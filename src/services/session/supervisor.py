"""SessionSupervisor — health, recovery, and cleanup.

Watches the two legs and derives session state from their health. Recovery itself lives
in the components (each owns its own retry loop); the supervisor decides *when a
session is beyond recovery* — which needs the retry budget, and is therefore a decision
health alone cannot make (``domain.session.derive_state`` deliberately never returns
``FAILED``).

One ``asyncio.Task`` per session. A session's failure cannot touch another's, because
they share no task and no state.

Platform-blind: it drives ``ConnectorSession`` and never learns which platform is
underneath. Zoom's two legs recover independently and Teams' single media session
recovers as a unit — both surface through ``leg_states()`` as a health pair, so the
difference is data rather than a second supervisor.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress

from src.domain.health import ComponentState
from src.domain.ids import SessionId
from src.domain.session import SessionState
from src.infrastructure.context import bind_context
from src.infrastructure.logging import get_logger
from src.infrastructure.metrics import MetricName, MetricsCollector
from src.protocols.connector import ConnectorSession
from src.services.session.lifecycle import SessionLifecycle
from src.services.session.registry import SessionRegistry

logger = get_logger(__name__)

DEFAULT_POLL_INTERVAL_S = 1.0
DEFAULT_UNHEALTHY_GRACE_S = 60.0
"""How long a leg may stay unhealthy before the session is declared FAILED. Generous
because component-level reconnect is already running underneath — this is the outer
backstop for when that has quietly given up."""


class SessionSupervisor:
    """Supervises live sessions."""

    __slots__ = (
        "_grace_s",
        "_lifecycle",
        "_metrics",
        "_poll_s",
        "_registry",
        "_sessions",
        "_tasks",
        "_unhealthy_since",
    )

    def __init__(
        self,
        *,
        registry: SessionRegistry,
        lifecycle: SessionLifecycle,
        metrics: MetricsCollector | None = None,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        unhealthy_grace_s: float = DEFAULT_UNHEALTHY_GRACE_S,
    ) -> None:
        self._registry = registry
        self._lifecycle = lifecycle
        self._metrics = metrics
        self._poll_s = poll_interval_s
        self._grace_s = unhealthy_grace_s
        self._sessions: dict[SessionId, ConnectorSession] = {}
        self._tasks: dict[SessionId, asyncio.Task[None]] = {}
        self._unhealthy_since: dict[SessionId, float] = {}

    def supervise(self, connector_session: ConnectorSession) -> None:
        """Begin supervising a started session."""
        session = connector_session.session
        session_id: SessionId = session.session_id
        self._sessions[session_id] = connector_session
        self._tasks[session_id] = asyncio.create_task(
            self._watch(session_id), name=f"supervise-{session_id}"
        )

    async def _watch(self, session_id: SessionId) -> None:
        connector_session = self._sessions.get(session_id)
        if connector_session is None:
            return
        session = connector_session.session

        with bind_context(
            session_id=session.session_id, correlation_id=session.correlation_id
        ):
            while True:
                await asyncio.sleep(self._poll_s)

                if session.state.is_terminal:
                    return

                ingest, publish = connector_session.leg_states()
                self._lifecycle.heartbeat(session)
                self._lifecycle.apply_health(session, ingest=ingest, publish=publish)

                if self._should_fail(session_id, ingest, publish, session_state=session.state):
                    detail = f"ingest={ingest} publish={publish} for over {self._grace_s}s"
                    self._lifecycle.fail(session, "supervisor", detail)
                    logger.error("session.failed", detail=detail)
                    if self._metrics is not None:
                        self._metrics.increment(MetricName.SESSIONS_FAILED_TOTAL)
                    return

    def _should_fail(
        self,
        session_id: SessionId,
        ingest: ComponentState,
        publish: ComponentState,
        *,
        session_state: SessionState,
    ) -> bool:
        """True once a leg has been unhealthy past the grace window.

        Exempt while still ``JOINING``: a leg reporting ``UNKNOWN`` there means it
        has not attempted its first attach yet — for RTMS ingest that is normal
        while a session waits on ``meeting.rtms_started`` (doc 003 §3.1), and that
        wait has its own, longer timeout (``RtmsAudioSource``'s
        ``attach_wait_timeout_s``). This grace window is for a leg that *was* up
        and went bad, not for the ordinary time a first attach takes. Once a leg
        actually fails to attach it reports ``UNHEALTHY``, not ``UNKNOWN``, which
        moves the session out of ``JOINING`` (``domain.session.derive_state``) and
        back under this clock.
        """
        if session_state is SessionState.JOINING:
            self._unhealthy_since.pop(session_id, None)
            return False

        loop = asyncio.get_running_loop()
        now = loop.time()
        impaired = not ingest.is_serving or not publish.is_serving

        if not impaired:
            self._unhealthy_since.pop(session_id, None)
            return False

        started = self._unhealthy_since.setdefault(session_id, now)
        return now - started > self._grace_s

    async def shutdown(self, session_id: SessionId) -> None:
        """Stop supervising and tear the session down. Idempotent."""
        task = self._tasks.pop(session_id, None)
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

        connector_session = self._sessions.pop(session_id, None)
        self._unhealthy_since.pop(session_id, None)
        if connector_session is None:
            return

        session = connector_session.session
        if not session.state.is_terminal:
            self._lifecycle.transition(session, SessionState.STOPPING)
        await connector_session.stop()
        if session.state is SessionState.STOPPING:
            self._lifecycle.transition(session, SessionState.STOPPED)

        if self._metrics is not None:
            self._metrics.drop_session(session_id)

    async def shutdown_all(self) -> None:
        """Drain every session.

        Called on application shutdown so a redeploy never leaves a bot sitting in a
        meeting talking to nobody.
        """
        for session_id in list(self._tasks):
            await self.shutdown(session_id)

    def get(self, session_id: SessionId) -> ConnectorSession | None:
        return self._sessions.get(session_id)

    def __len__(self) -> int:
        return len(self._sessions)
