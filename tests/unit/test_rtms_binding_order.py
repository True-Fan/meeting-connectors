"""Binding an ``rtms_started`` payload works in either arrival order.

The two orders are not symmetric in the data available, which is why this exists:

* **Webhook first** — no session yet, so the payload parks and ``create_session``
  claims it. Long covered.
* **Session first** — the session exists but its ``meeting_uuid`` is still ``None``,
  because an operator creates a session by *meeting number* and Zoom's
  ``meeting.rtms_started`` carries no meeting number to match against. Only the UUID
  is on the wire, so the exact lookup misses and — without a fallback — the payload
  parks with nobody left to claim it. Ingest then waits out its full timeout for a
  webhook that already arrived.

**The browser connector makes the second order the normal case**: it joins the
meeting first and RTMS starts afterwards, so the session always exists before the
webhook.
"""

from __future__ import annotations

import pytest

from src.domain.health import ComponentState, HealthReport
from src.domain.meeting import MeetingPlatform
from src.domain.session import SessionContext
from src.services.meeting.connector_registry import ConnectorRegistry
from src.services.meeting.service import CreateSessionCommand, MeetingService
from src.services.session.lifecycle import SessionLifecycle
from src.services.session.registry import PendingRtmsBinding, SessionRegistry
from src.services.session.supervisor import SessionSupervisor


class StubSession:
    def __init__(self, session: SessionContext) -> None:
        self._session = session

    @property
    def session(self) -> SessionContext:
        return self._session

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    def health(self) -> HealthReport:
        return HealthReport()

    def leg_states(self) -> tuple[ComponentState, ComponentState]:
        return ComponentState.HEALTHY, ComponentState.HEALTHY


class StubFactory:
    def build(self, session: SessionContext) -> StubSession:
        return StubSession(session)


@pytest.fixture
def registry() -> SessionRegistry:
    return SessionRegistry()


@pytest.fixture
def service(registry: SessionRegistry) -> MeetingService:
    lifecycle = SessionLifecycle()
    return MeetingService(
        registry=registry,
        lifecycle=lifecycle,
        supervisor=SessionSupervisor(registry=registry, lifecycle=lifecycle),
        connectors=(
            ConnectorRegistry()
            .register(MeetingPlatform.ZOOM, StubFactory())
            .register(MeetingPlatform.ZOOM_WEB, StubFactory())
            .register(MeetingPlatform.TEAMS, StubFactory())
        ),
    )


def binding(meeting_uuid: str = "uuid-abc==") -> PendingRtmsBinding:
    return PendingRtmsBinding(
        meeting_uuid=meeting_uuid,
        rtms_stream_id="stream-1",
        signaling_url="wss://rtms.example/signal",
    )


@pytest.mark.parametrize(
    "platform", [MeetingPlatform.ZOOM, MeetingPlatform.ZOOM_WEB]
)
async def test_webhook_binds_to_a_session_created_before_it(
    service: MeetingService, platform: MeetingPlatform
) -> None:
    """The regression this file exists for: it used to park and strand."""
    created = await service.create_session(
        CreateSessionCommand(meeting_number="9414944", platform=platform)
    )
    assert created.meeting.meeting_uuid is None

    bound = await service.bind_rtms(binding())

    assert bound is not None
    assert bound.session_id == created.session_id
    assert bound.meeting.platform_data["signaling_url"] == "wss://rtms.example/signal"


async def test_two_unbound_zoom_sessions_park_rather_than_guess(
    service: MeetingService, registry: SessionRegistry
) -> None:
    """With no way to tell which session a UUID belongs to, binding either is wrong."""
    await service.create_session(CreateSessionCommand(meeting_number="1111111"))
    await service.create_session(
        CreateSessionCommand(meeting_number="2222222", platform=MeetingPlatform.ZOOM_WEB)
    )

    assert await service.bind_rtms(binding()) is None
    assert registry.pending_count() == 1


async def test_a_teams_session_is_not_a_binding_candidate(
    service: MeetingService, registry: SessionRegistry
) -> None:
    """The fallback is Zoom-only, like the parked-binding claim it mirrors."""
    await service.create_session(
        CreateSessionCommand(meeting_number="123456789012", platform=MeetingPlatform.TEAMS)
    )

    assert registry.sole_session_awaiting_rtms() is None
    assert await service.bind_rtms(binding()) is None


async def test_binding_twice_finds_the_session_by_uuid(
    service: MeetingService, registry: SessionRegistry
) -> None:
    """Once bound, the exact match takes over and the fallback is not consulted."""
    await service.create_session(
        CreateSessionCommand(meeting_number="9414944", platform=MeetingPlatform.ZOOM_WEB)
    )
    await service.bind_rtms(binding())

    assert registry.sole_session_awaiting_rtms() is None
    assert registry.by_meeting_uuid("uuid-abc==") is not None


async def test_webhook_first_still_parks_and_is_claimed_on_create(
    service: MeetingService, registry: SessionRegistry
) -> None:
    """The pre-existing order must be untouched."""
    assert await service.bind_rtms(binding()) is None
    assert registry.pending_count() == 1

    created = await service.create_session(
        CreateSessionCommand(meeting_number="9414944", platform=MeetingPlatform.ZOOM_WEB)
    )

    assert created.meeting.meeting_uuid == "uuid-abc=="
    assert registry.pending_count() == 0
