"""MeetingService — session lifecycle orchestration.

The single entry point for "start an avatar in a meeting" and "stop it". It allocates
and supervises; it never touches a media frame.

Also resolves the **join/RTMS race** (doc 003 §3.1): a Zoom session can be created
before or after the ``meeting.rtms_started`` webhook arrives, and whichever lands
second completes the binding. Handling only one order would make attachment depend on
timing we do not control. That race is Zoom's alone — Teams initiates its own join and
has nothing to park — so the claim step is gated on the platform (doc 005 §3.4).
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
    platform: MeetingPlatform = MeetingPlatform.ZOOM
    """Which connector serves this session. Defaults to ``ZOOM``, so every existing
    caller keeps its exact behaviour."""
    meeting_url: str | None = None
    """Optional platform join URL. Zoom ignores it (it joins by number); the Teams
    connector parses it for the Graph join descriptor when no numeric meeting id and
    passcode are supplied (doc 005 §3.2)."""


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
            session_factory: A single factory, registered as the ``ZOOM`` connector.
                Retained so the pre-Teams constructor signature keeps working
                unchanged; supply one or the other, not both.

        Raises:
            ValueError: neither or both of ``connectors`` and ``session_factory``.
        """
        if (connectors is None) == (session_factory is None):
            raise ValueError("supply exactly one of connectors= or session_factory=")

        if connectors is None:
            assert session_factory is not None
            connectors = ConnectorRegistry().register(MeetingPlatform.ZOOM, session_factory)

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

        If an ``rtms_started`` webhook is already parked, it is claimed here and ingest
        attaches immediately. Otherwise the publisher still starts — the avatar joins
        and idles until the webhook arrives (doc 003 §3.1).

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
                meeting_uuid=command.meeting_uuid,
                platform_data=platform_data,
                platform=command.platform,
            ),
        )

        with bind_context(session_id=session.session_id, correlation_id=session.correlation_id):
            self._claim_pending_rtms(session)
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
                rtms_bound=session.meeting.meeting_uuid is not None,
            )
            return session

    def _claim_pending_rtms(self, session: SessionContext) -> None:
        """Bind a parked ``rtms_started`` payload to a new session, if one is waiting.

        **Zoom only.** ``take_any_pending_rtms`` claims by arrival order rather than
        identity, so without this guard a Teams session created while a Zoom webhook
        sat parked would swallow that binding — leaving the Zoom session that the
        payload belongs to waiting forever for a webhook that had already been
        consumed. The guard is what keeps the two connectors' races from touching.
        """
        if session.meeting.platform not in (
            MeetingPlatform.ZOOM,
            MeetingPlatform.ZOOM_WEB,
        ):
            return

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
            # Created before the webhook, so its UUID is still unset and the exact
            # match above cannot find it. ``meeting.rtms_started`` carries no meeting
            # number to match on instead, so fall back to the sole unbound session.
            session = self._registry.sole_session_awaiting_rtms()
        if session is None:
            # Webhook arrived first. Park it; ``create_session`` will claim it.
            self._registry.park_pending_rtms(binding)
            return None

        with bind_context(session_id=session.session_id, correlation_id=session.correlation_id):
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
