"""MeetingService — session lifecycle orchestration.

The single entry point for "start an avatar in a meeting" and "stop it". It allocates
and supervises; it never touches a media frame.

**It used to resolve a race here too** (doc 003 §3.1), and the reason it no longer has to is
worth recording. The Meeting-SDK Zoom connector did not open its own ingest leg: *Zoom* did,
through a ``meeting.rtms_started`` webhook that could arrive before or after the session was
created, so this service had to claim or park a binding and gate that step on the platform.
Every connector now joins with a browser and opens ingest synchronously inside
``start()``, so there is no second initiator and no arrival order to reconcile.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.domain.exceptions import DomainError
from src.domain.ids import CorrelationId, SessionId, new_correlation_id, new_session_id
from src.domain.meeting import MeetingContext, MeetingPlatform
from src.domain.session import SessionContext, SessionState
from src.infrastructure.context import bind_context
from src.infrastructure.logging import get_logger
from src.infrastructure.metrics import MetricName, MetricsCollector
from src.protocols.connector import ConnectorSession, ConnectorSessionFactory
from src.services.meeting.connector_registry import ConnectorRegistry
from src.services.session.lifecycle import SessionLifecycle
from src.services.session.registry import SessionRegistry
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
    correlation_id: CorrelationId | None = None
    platform: MeetingPlatform = MeetingPlatform.ZOOM_WEB
    """Which connector serves this session. Defaults to ``ZOOM_WEB``, which is what the
    removed ``ZOOM`` default resolved to in practice once the Meeting-SDK connector went
    away — a Zoom join with no platform stated still joins Zoom."""
    meeting_url: str | None = None
    """Optional platform join URL, used where a numeric meeting id is not to hand. The
    ``teams_web`` connector resolves one; ``zoom_web`` and ``google_meet`` join by number
    and code respectively."""


class MeetingService:
    """Creates, stops, and looks up avatar meeting sessions."""

    __slots__ = (
        "_connectors",
        "_default_display_name",
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
        connectors: ConnectorRegistry | None = None,
        session_factory: ConnectorSessionFactory | None = None,
        default_display_name: str = "AI Avatar",
        metrics: MetricsCollector | None = None,
    ) -> None:
        """Wire the service.

        Args:
            connectors: Platform → factory lookup. The path used in production.
            session_factory: A single factory, registered as the ``ZOOM_WEB`` connector.
                A convenience for tests that exercise one connector; supply one or the
                other, not both.

        Raises:
            ValueError: neither or both of ``connectors`` and ``session_factory``.
        """
        if (connectors is None) == (session_factory is None):
            raise ValueError("supply exactly one of connectors= or session_factory=")

        if connectors is None:
            assert session_factory is not None
            connectors = ConnectorRegistry().register(
                MeetingPlatform.ZOOM_WEB, session_factory
            )

        self._registry = registry
        self._lifecycle = lifecycle
        self._supervisor = supervisor
        self._connectors = connectors
        self._default_display_name = default_display_name
        self._metrics = metrics

    @property
    def supported_platforms(self) -> frozenset[MeetingPlatform]:
        """Platforms this deployment has a connector registered for."""
        return self._connectors.supported()

    # -- creation ----------------------------------------------------------

    async def create_session(self, command: CreateSessionCommand) -> SessionContext:
        """Create and start a session.

        Raises:
            MeetingServiceError: a session for this meeting already exists, no
                connector is registered for the requested platform, or start failed.
        """
        if self._registry.by_meeting_number(command.meeting_number) is not None:
            raise MeetingServiceError(
                f"a session for meeting {command.meeting_number} already exists"
            )

        # Resolved before anything is allocated: an unsupported platform must fail
        # before a session id, a registry entry, or a socket exists.
        try:
            factory = self._connectors.get(command.platform)
        except DomainError as exc:
            raise MeetingServiceError(str(exc)) from exc

        platform_data: dict[str, Any] = {}
        if command.meeting_url:
            platform_data["meeting_url"] = command.meeting_url

        session = SessionContext(
            session_id=new_session_id(),
            correlation_id=command.correlation_id or new_correlation_id(),
            meeting=MeetingContext(
                meeting_number=command.meeting_number,
                display_name=command.display_name or self._default_display_name,
                passcode=command.passcode,
                platform_data=platform_data,
                platform=command.platform,
            ),
        )

        with bind_context(session_id=session.session_id, correlation_id=session.correlation_id):
            self._registry.register(session)

            try:
                connector_session = factory.build(session)
                self._lifecycle.transition(session, SessionState.JOINING)
                await connector_session.start()
            except Exception as exc:
                self._lifecycle.fail(session, "meeting_service", str(exc))
                self._registry.remove(session.session_id)
                logger.error("session.start_failed", error=str(exc))
                raise MeetingServiceError(f"cannot start session: {exc}") from exc

            self._supervisor.supervise(connector_session)
            if self._metrics is not None:
                self._metrics.increment(
                    MetricName.SESSIONS_STARTED_TOTAL, platform=command.platform
                )

            logger.info(
                "session.created",
                platform=command.platform,
                meeting_number=command.meeting_number,
            )
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

        with bind_context(session_id=session.session_id, correlation_id=session.correlation_id):
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
        connector_session: ConnectorSession | None = self._supervisor.get(session_id)
        return connector_session.health() if connector_session is not None else None

    # -- attendance --------------------------------------------------------
    #
    # Duck-typed rather than added to ``ConnectorSession``, and deliberately so. Doc 003 §0
    # sets the rule this file already follows elsewhere: *a protocol earns its place only if a
    # second implementation exists in this repository today.* One connector keeps an
    # attendance ledger, so widening the port would oblige Zoom and Teams to answer a question
    # neither can, and put a Meet-shaped concept in the one place that is meant to be
    # platform-blind. When a second connector grows one, this becomes a protocol method and
    # these two ``getattr`` calls go away.

    def attendance_snapshot(self, session_id: SessionId) -> object | None:
        """Who has been in this session's meeting, if its connector tracks that.

        ``None`` covers three genuinely different cases the caller has to distinguish — no
        such session, a connector with no ledger, and a ledger switched off — so the API layer
        maps it to a 404 with a message rather than to an empty answer, which would read as
        "nobody attended".
        """
        ledger = self._ledger(session_id)
        return ledger.snapshot() if ledger is not None else None

    def seed_invitees(self, session_id: SessionId, names: tuple[str, ...]) -> int | None:
        """Tell a session who was invited. Returns how many names were newly recorded.

        Separate from session creation because the invite list comes from a different place
        than the join does: the calendar event, which the orchestrator holds and the bridge has
        no access to. Accepting it as a later call keeps ``CreateSessionRequest`` platform-blind
        and lets the list arrive late — a meeting whose invitees are posted ten minutes in still
        gets a correct "who never joined" answer, because the ledger marks existing entries
        rather than replacing them.
        """
        ledger = self._ledger(session_id)
        return ledger.seed_invitees(names) if ledger is not None else None

    def _ledger(self, session_id: SessionId) -> Any | None:
        connector_session = self._supervisor.get(session_id)
        if connector_session is None:
            return None
        return getattr(connector_session, "attendance", None)

    # -- who is speaking ---------------------------------------------------
    #
    # Duck-typed for exactly the reason attendance is, and the rule is worth restating rather
    # than assumed: one connector can identify a speaker, because one connector has a browser to
    # observe. Widening ``ConnectorSession`` would oblige Zoom and Teams to answer a question
    # neither can — their audio arrives mixed too, and neither has a DOM to attribute it from.

    def speaker_snapshot(self, session_id: SessionId) -> object | None:
        """Who is speaking in this session's meeting, and who has, if its connector knows.

        ``None`` covers the same three cases attendance's does — no such session, a connector
        that does not track speakers, and tracking switched off — so the API maps it to a 404
        rather than to an empty answer, which would read as "nobody has spoken".
        """
        tracker = self._speakers(session_id)
        return tracker.snapshot() if tracker is not None else None

    def _speakers(self, session_id: SessionId) -> Any | None:
        connector_session = self._supervisor.get(session_id)
        if connector_session is None:
            return None
        return getattr(connector_session, "speakers", None)

    def transcript_snapshot(self, session_id: SessionId) -> object | None:
        """What each participant said, if this session's connector records it.

        Duck-typed like the two above, and ``None`` for the same three distinguishable cases — no
        such session, a connector that cannot transcribe, and captions switched off — so the API
        can say which rather than returning an empty conversation.
        """
        transcript = self._transcript(session_id)
        return transcript.snapshot() if transcript is not None else None

    def _transcript(self, session_id: SessionId) -> Any | None:
        connector_session = self._supervisor.get(session_id)
        if connector_session is None:
            return None
        return getattr(connector_session, "transcript", None)
