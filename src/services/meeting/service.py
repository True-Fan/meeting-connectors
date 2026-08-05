"""MeetingService — session lifecycle orchestration.

The single entry point for "start an avatar in a meeting" and "stop it". It allocates
and supervises; it never touches a media frame.

Also resolves the **join/RTMS race** (doc 003 §3.1): a session can be created before or
after the ``meeting.rtms_started`` webhook arrives, and whichever lands second completes
the binding. Handling only one order would make attachment depend on timing we do not
control.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.domain.exceptions import DomainError
from src.domain.ids import CorrelationId, SessionId, new_correlation_id, new_session_id
from src.domain.meeting import MeetingContext
from src.domain.session import SessionContext, SessionState
from src.infrastructure.context import bind_context
from src.infrastructure.logging import get_logger
from src.infrastructure.metrics import MetricName, MetricsCollector
from src.services.session.lifecycle import SessionLifecycle
from src.services.session.registry import PendingRtmsBinding, SessionRegistry
from src.services.session.supervisor import SessionSupervisor

logger = get_logger(__name__)


class MeetingServiceError(DomainError):
    """A session could not be created or stopped."""


class SessionNotFoundError(MeetingServiceError):
    def __init__(self, session_id: str) -> None:
        super().__init__(f"session {session_id} not found")


@dataclass(frozen=True, slots=True)
class CreateSessionCommand:
    """Request to put an avatar into a meeting."""

    meeting_number: str
    passcode: str | None = None
    display_name: str | None = None
    meeting_uuid: str | None = None
    correlation_id: CorrelationId | None = None


class MeetingService:
    """Creates, stops, and looks up avatar meeting sessions."""

    __slots__ = (
        "_default_display_name",
        "_factory",
        "_lifecycle",
        "_metrics",
        "_registry",
        "_supervisor",
    )

    def __init__(
        self,
        *,
        registry: SessionRegistry,
        lifecycle: SessionLifecycle,
        supervisor: SessionSupervisor,
        session_factory: object,
        default_display_name: str = "AI Avatar",
        metrics: MetricsCollector | None = None,
    ) -> None:
        self._registry = registry
        self._lifecycle = lifecycle
        self._supervisor = supervisor
        self._factory = session_factory
        self._default_display_name = default_display_name
        self._metrics = metrics

    # -- creation ----------------------------------------------------------

    async def create_session(self, command: CreateSessionCommand) -> SessionContext:
        """Create and start a session.

        If an ``rtms_started`` webhook is already parked, it is claimed here and ingest
        attaches immediately. Otherwise the publisher still starts — the avatar joins
        and idles until the webhook arrives (doc 003 §3.1).

        Raises:
            MeetingServiceError: a session for this meeting already exists, or start
                failed.
        """
        if self._registry.by_meeting_number(command.meeting_number) is not None:
            raise MeetingServiceError(
                f"a session for meeting {command.meeting_number} already exists"
            )

        session = SessionContext(
            session_id=new_session_id(),
            correlation_id=command.correlation_id or new_correlation_id(),
            meeting=MeetingContext(
                meeting_number=command.meeting_number,
                display_name=command.display_name or self._default_display_name,
                passcode=command.passcode,
                meeting_uuid=command.meeting_uuid,
            ),
        )

        with bind_context(
            session_id=session.session_id, correlation_id=session.correlation_id
        ):
            self._claim_pending_rtms(session)
            self._registry.register(session)

            try:
                zoom_session = self._factory.build(session)  # type: ignore[attr-defined]
                self._lifecycle.transition(session, SessionState.JOINING)
                await zoom_session.start()
            except Exception as exc:
                self._lifecycle.fail(session, "meeting_service", str(exc))
                self._registry.remove(session.session_id)
                logger.error("session.start_failed", error=str(exc))
                raise MeetingServiceError(f"cannot start session: {exc}") from exc

            self._supervisor.supervise(zoom_session)
            if self._metrics is not None:
                self._metrics.increment(MetricName.SESSIONS_STARTED_TOTAL)

            logger.info(
                "session.created",
                meeting_number=command.meeting_number,
                rtms_bound=session.meeting.meeting_uuid is not None,
            )
            return session

    def _claim_pending_rtms(self, session: SessionContext) -> None:
        """Bind a parked ``rtms_started`` payload to a new session, if one is waiting."""
        binding = None
        if session.meeting.meeting_uuid:
            binding = self._registry.take_pending_rtms(session.meeting.meeting_uuid)
        if binding is None:
            binding = self._registry.take_any_pending_rtms()
        if binding is None:
            return
        self._apply_binding(session, binding)
        logger.info("session.rtms_binding_claimed", meeting_uuid=binding.meeting_uuid)

    @staticmethod
    def _apply_binding(session: SessionContext, binding: PendingRtmsBinding) -> None:
        session.meeting = session.meeting.with_uuid(binding.meeting_uuid)
        session.meeting.platform_data["rtms_stream_id"] = binding.rtms_stream_id
        session.meeting.platform_data["signaling_url"] = binding.signaling_url

    # -- webhook binding ---------------------------------------------------

    async def bind_rtms(self, binding: PendingRtmsBinding) -> SessionContext | None:
        """Bind an ``rtms_started`` payload to a session, parking it if none exists yet.

        Returns the session when one was found, otherwise ``None``.
        """
        session = self._registry.by_meeting_uuid(binding.meeting_uuid)
        if session is None:
            # Webhook arrived first. Park it; ``create_session`` will claim it.
            self._registry.park_pending_rtms(binding)
            return None

        with bind_context(
            session_id=session.session_id, correlation_id=session.correlation_id
        ):
            self._apply_binding(session, binding)
            self._registry.bind_uuid(session.session_id, binding.meeting_uuid)
            logger.info("session.rtms_bound", meeting_uuid=binding.meeting_uuid)
            return session

    async def handle_rtms_stopped(self, meeting_uuid: str) -> SessionContext | None:
        """Tear a session down because Zoom stopped its RTMS stream."""
        session = self._registry.by_meeting_uuid(meeting_uuid)
        if session is None:
            return None
        await self.stop_session(session.session_id)
        return session

    # -- lookup and teardown ----------------------------------------------

    def get_session(self, session_id: SessionId) -> SessionContext | None:
        return self._registry.by_id(session_id)

    def list_sessions(self) -> tuple[SessionContext, ...]:
        return self._registry.all_sessions()

    async def stop_session(self, session_id: SessionId) -> SessionContext:
        """Stop and evict a session.

        Raises:
            SessionNotFoundError: no such session.
        """
        session = self._registry.by_id(session_id)
        if session is None:
            raise SessionNotFoundError(session_id)

        with bind_context(
            session_id=session.session_id, correlation_id=session.correlation_id
        ):
            await self._supervisor.shutdown(session_id)
            self._registry.remove(session_id)
            logger.info("session.stopped", state=session.state)
            return session

    async def stop_all(self) -> None:
        """Drain every session, so a redeploy leaves no bot behind in a meeting."""
        for session in self._registry.all_sessions():
            try:
                await self.stop_session(session.session_id)
            except MeetingServiceError:
                continue

    def health_report(self, session_id: SessionId) -> object | None:
        """Component-level health for one session, if it is supervised."""
        zoom_session = self._supervisor.get(session_id)
        return zoom_session.health() if zoom_session is not None else None
