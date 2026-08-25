"""Session state transitions.

The single place a session's state changes. State is *derived* from component health
by ``domain.session.derive_state``, so "what does healthy mean" is decided once
rather than at each call site.
"""

from __future__ import annotations

from datetime import UTC, datetime

from src.domain.exceptions import IllegalStateTransitionError
from src.domain.health import ComponentState
from src.domain.session import SessionContext, SessionState, can_transition, derive_state
from src.infrastructure.logging import get_logger

logger = get_logger(__name__)


class SessionLifecycle:
    """Applies state transitions to a ``SessionContext``."""

    def transition(self, session: SessionContext, target: SessionState) -> SessionState:
        """Move a session to ``target``.

        Returns the resulting state. Re-entering the current state is a no-op rather
        than an error: health polling naturally re-derives the same state, and making
        that raise would force every caller to compare first.

        Raises:
            IllegalStateTransitionError: the move is not in the transition table.
        """
        if session.state is target:
            return session.state
        if not can_transition(session.state, target):
            raise IllegalStateTransitionError(session.state, target)

        previous = session.state
        session.state = target
        self._stamp(session, target)

        logger.info("session.transition", from_state=previous, to_state=target)
        return target

    def apply_health(
        self,
        session: SessionContext,
        *,
        ingest: ComponentState,
        publish: ComponentState,
    ) -> SessionState:
        """Derive and apply the state implied by component health.

        Terminal and stopping sessions are left alone — a component reporting
        unhealthy during teardown is expected, not a reason to resurrect a session.
        """
        if session.state.is_terminal or session.state is SessionState.STOPPING:
            return session.state

        derived = derive_state(ingest, publish)
        if derived is SessionState.JOINING and session.state is not SessionState.JOINING:
            # A component dropping back to UNKNOWN after we were running is a
            # restart in progress, which is DEGRADED rather than a return to
            # JOINING — JOINING is not reachable from ACTIVE.
            derived = SessionState.DEGRADED

        return self.transition(session, derived)

    def fail(self, session: SessionContext, component: str, message: str) -> SessionState:
        """Record a fatal error and move the session to ``FAILED``."""
        session.record_error(component, message, fatal=True)
        if session.state.is_terminal:
            return session.state
        return self.transition(session, SessionState.FAILED)

    def _stamp(self, session: SessionContext, target: SessionState) -> None:
        now = datetime.now(UTC)
        if target is SessionState.ACTIVE and session.started_at is None:
            session.started_at = now
        if target.is_terminal:
            session.ended_at = now

    def heartbeat(self, session: SessionContext) -> None:
        """Record liveness.

        Distinct from RTMS's ``msg_type 12``/``13``, which is a Zoom wire detail owned
        by ``connectors/zoom/rtms/keepalive.py``. This is session liveness — a domain
        concern (doc 002 §1.2 D6).
        """
        session.last_heartbeat_at = datetime.now(UTC)
