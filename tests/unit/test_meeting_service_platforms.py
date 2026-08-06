"""MeetingService with more than one connector.

Two things are under test:

1. **Routing** — a session goes to the connector its platform names.
2. **Isolation** — the Zoom-specific RTMS binding race cannot be disturbed by a Teams
   session, and every pre-Teams behaviour is unchanged.

The second is the important one: Zoom is in production.
"""

from __future__ import annotations

import pytest

from src.domain.health import ComponentState, HealthReport
from src.domain.meeting import MeetingPlatform
from src.domain.session import SessionContext, SessionState
from src.services.meeting.connector_registry import ConnectorRegistry
from src.services.meeting.service import (
    CreateSessionCommand,
    MeetingService,
    MeetingServiceError,
)
from src.services.session.lifecycle import SessionLifecycle
from src.services.session.registry import PendingRtmsBinding, SessionRegistry
from src.services.session.supervisor import SessionSupervisor


class RecordingSession:
    """A ``ConnectorSession`` that records what happened to it."""

    def __init__(self, session: SessionContext, platform: str) -> None:
        self._session = session
        self.platform = platform
        self.started = False
        self.stopped = False

    @property
    def session(self) -> SessionContext:
        return self._session

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    def health(self) -> HealthReport:
        return HealthReport()

    def leg_states(self) -> tuple[ComponentState, ComponentState]:
        return ComponentState.HEALTHY, ComponentState.HEALTHY


class RecordingFactory:
    def __init__(self, platform: str, *, fail: Exception | None = None) -> None:
        self.platform = platform
        self.fail = fail
        self.built: list[RecordingSession] = []

    def build(self, session: SessionContext) -> RecordingSession:
        if self.fail is not None:
            raise self.fail
        built = RecordingSession(session, self.platform)
        self.built.append(built)
        return built


@pytest.fixture
def zoom_factory() -> RecordingFactory:
    return RecordingFactory("zoom")


@pytest.fixture
def teams_factory() -> RecordingFactory:
    return RecordingFactory("teams")


@pytest.fixture
def service(
    zoom_factory: RecordingFactory, teams_factory: RecordingFactory
) -> MeetingService:
    registry = SessionRegistry()
    lifecycle = SessionLifecycle()
    return MeetingService(
        registry=registry,
        lifecycle=lifecycle,
        supervisor=SessionSupervisor(registry=registry, lifecycle=lifecycle),
        connectors=(
            ConnectorRegistry()
            .register(MeetingPlatform.ZOOM, zoom_factory)
            .register(MeetingPlatform.TEAMS, teams_factory)
        ),
    )


# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #


async def test_default_platform_is_zoom(
    service: MeetingService, zoom_factory: RecordingFactory, teams_factory: RecordingFactory
) -> None:
    """A command written before Teams existed must still go to Zoom."""
    session = await service.create_session(CreateSessionCommand(meeting_number="1234567890"))

    assert session.meeting.platform is MeetingPlatform.ZOOM
    assert len(zoom_factory.built) == 1
    assert teams_factory.built == []


async def test_teams_command_routes_to_the_teams_connector(
    service: MeetingService, zoom_factory: RecordingFactory, teams_factory: RecordingFactory
) -> None:
    session = await service.create_session(
        CreateSessionCommand(
            meeting_number="123456789012", platform=MeetingPlatform.TEAMS
        )
    )

    assert session.meeting.platform is MeetingPlatform.TEAMS
    assert len(teams_factory.built) == 1
    assert zoom_factory.built == []
    assert teams_factory.built[0].started


async def test_meeting_url_is_carried_into_platform_data(service: MeetingService) -> None:
    url = "https://teams.microsoft.com/l/meetup-join/19%3ameeting_x%40thread.v2/0"
    session = await service.create_session(
        CreateSessionCommand(
            meeting_number="123456789012",
            platform=MeetingPlatform.TEAMS,
            meeting_url=url,
        )
    )

    assert session.meeting.platform_data["meeting_url"] == url


async def test_absent_meeting_url_leaves_platform_data_empty(service: MeetingService) -> None:
    session = await service.create_session(CreateSessionCommand(meeting_number="1234567890"))
    assert session.meeting.platform_data == {}


async def test_unregistered_platform_is_rejected_before_anything_is_allocated(
    zoom_factory: RecordingFactory,
) -> None:
    registry = SessionRegistry()
    lifecycle = SessionLifecycle()
    service = MeetingService(
        registry=registry,
        lifecycle=lifecycle,
        supervisor=SessionSupervisor(registry=registry, lifecycle=lifecycle),
        connectors=ConnectorRegistry().register(MeetingPlatform.ZOOM, zoom_factory),
    )

    with pytest.raises(MeetingServiceError, match="no connector registered"):
        await service.create_session(
            CreateSessionCommand(meeting_number="1", platform=MeetingPlatform.TEAMS)
        )

    # Nothing was allocated: no session row, no started connector.
    assert len(registry) == 0
    assert zoom_factory.built == []


# --------------------------------------------------------------------------- #
# Zoom isolation
# --------------------------------------------------------------------------- #


async def test_a_teams_session_does_not_claim_a_parked_zoom_binding(
    service: MeetingService, zoom_factory: RecordingFactory, teams_factory: RecordingFactory
) -> None:
    """The bug this guards against: ``take_any_pending_rtms`` claims by arrival order, not
    identity. Without the platform guard, a Teams session created while a Zoom webhook sat
    parked would swallow that binding — and the Zoom session it belongs to would wait
    forever for a webhook that had already been consumed."""
    binding = PendingRtmsBinding(
        meeting_uuid="uuid-zoom-1",
        rtms_stream_id="stream-1",
        signaling_url="wss://rtms.example/signal",
    )
    assert await service.bind_rtms(binding) is None  # parked, no session yet

    teams = await service.create_session(
        CreateSessionCommand(meeting_number="123456789012", platform=MeetingPlatform.TEAMS)
    )
    assert teams.meeting.meeting_uuid is None
    assert "rtms_stream_id" not in teams.meeting.platform_data

    # The binding is still there for the Zoom session it belongs to.
    zoom = await service.create_session(CreateSessionCommand(meeting_number="1234567890"))
    assert zoom.meeting.meeting_uuid == "uuid-zoom-1"
    assert zoom.meeting.platform_data["rtms_stream_id"] == "stream-1"


async def test_zoom_still_claims_its_own_parked_binding(service: MeetingService) -> None:
    """The pre-Teams behaviour, unchanged."""
    binding = PendingRtmsBinding(
        meeting_uuid="uuid-zoom-2",
        rtms_stream_id="stream-2",
        signaling_url="wss://rtms.example/signal",
    )
    await service.bind_rtms(binding)

    session = await service.create_session(CreateSessionCommand(meeting_number="1234567890"))

    assert session.meeting.meeting_uuid == "uuid-zoom-2"
    assert session.meeting.platform_data["signaling_url"] == "wss://rtms.example/signal"


async def test_with_uuid_preserves_the_platform() -> None:
    """``_apply_binding`` rebuilds the meeting context through ``with_uuid``. Dropping the
    platform there would silently turn a session back into a Zoom one."""
    from src.domain.meeting import MeetingContext

    meeting = MeetingContext(
        meeting_number="1",
        display_name="AI Avatar",
        platform=MeetingPlatform.TEAMS,
        platform_data={"meeting_url": "https://example"},
    )
    rebound = meeting.with_uuid("uuid-x")

    assert rebound.platform is MeetingPlatform.TEAMS
    assert rebound.meeting_uuid == "uuid-x"
    assert rebound.platform_data == {"meeting_url": "https://example"}


# --------------------------------------------------------------------------- #
# Constructor compatibility
# --------------------------------------------------------------------------- #


async def test_legacy_session_factory_argument_still_works(
    zoom_factory: RecordingFactory,
) -> None:
    """The pre-Teams constructor signature. Kept working so the change to
    ``MeetingService`` is additive rather than a breaking API change."""
    registry = SessionRegistry()
    lifecycle = SessionLifecycle()
    service = MeetingService(
        registry=registry,
        lifecycle=lifecycle,
        supervisor=SessionSupervisor(registry=registry, lifecycle=lifecycle),
        session_factory=zoom_factory,
    )

    assert service.supported_platforms == frozenset({MeetingPlatform.ZOOM})

    session = await service.create_session(CreateSessionCommand(meeting_number="1234567890"))
    assert session.state is not SessionState.FAILED
    assert len(zoom_factory.built) == 1


def test_supplying_both_or_neither_is_refused(zoom_factory: RecordingFactory) -> None:
    registry = SessionRegistry()
    lifecycle = SessionLifecycle()
    supervisor = SessionSupervisor(registry=registry, lifecycle=lifecycle)

    with pytest.raises(ValueError, match="exactly one"):
        MeetingService(
            registry=registry,
            lifecycle=lifecycle,
            supervisor=supervisor,
            connectors=ConnectorRegistry(),
            session_factory=zoom_factory,
        )

    with pytest.raises(ValueError, match="exactly one"):
        MeetingService(registry=registry, lifecycle=lifecycle, supervisor=supervisor)


# --------------------------------------------------------------------------- #
# Teardown
# --------------------------------------------------------------------------- #


async def test_stopping_a_teams_session_stops_its_connector(
    service: MeetingService, teams_factory: RecordingFactory
) -> None:
    session = await service.create_session(
        CreateSessionCommand(meeting_number="123456789012", platform=MeetingPlatform.TEAMS)
    )
    await service.stop_session(session.session_id)

    assert teams_factory.built[0].stopped


async def test_stop_all_drains_both_platforms(
    service: MeetingService, zoom_factory: RecordingFactory, teams_factory: RecordingFactory
) -> None:
    """A redeploy must not leave a bot behind in a meeting on either platform."""
    await service.create_session(CreateSessionCommand(meeting_number="1234567890"))
    await service.create_session(
        CreateSessionCommand(meeting_number="123456789012", platform=MeetingPlatform.TEAMS)
    )

    await service.stop_all()

    assert zoom_factory.built[0].stopped
    assert teams_factory.built[0].stopped
    assert service.list_sessions() == ()
