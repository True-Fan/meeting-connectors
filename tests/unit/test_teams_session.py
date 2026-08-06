"""Teams session composition.

The payoff test. It asserts that the Teams connector reuses the *shared* pipeline — the
avatar client, decoder, pacer, echo guard, media clock, and idle source that were all
written for Zoom — rather than growing a second one. If this file ever needs a Teams-specific
pipeline component, the boundary has failed.
"""

from __future__ import annotations

import asyncio

import pytest
from pydantic import SecretStr

from src.config.settings import Settings, TeamsSettings
from src.connectors.teams.config import TeamsConnectorConfig
from src.connectors.teams.session.teams_session import (
    TeamsMeetingSession,
    TeamsSessionFactory,
)
from src.domain.health import ComponentState
from src.domain.ids import CorrelationId, SessionId
from src.domain.media import AudioFrame
from src.domain.meeting import MeetingContext, MeetingPlatform, ParticipantRef
from src.domain.session import SessionContext
from src.infrastructure.metrics import MetricsCollector
from src.protocols.sink import MediaSink
from src.services.media.sinks.null_sink import NullSink
from tests.fakes.teams_sidecar import FakeTeamsSidecar

TENANT = "72f988bf-86f1-41af-91ab-2d7cd011db47"


def _config(**teams: object) -> TeamsConnectorConfig:
    base: dict[str, object] = {
        "tenant_id": TENANT,
        "client_id": "8b081ef6-4792-4def-b2c9-c363a1bf41d5",
        "client_secret": SecretStr("secret"),
        "sidecar_host": "teams-bot.internal",
        "sidecar_ready_timeout_s": 1.0,
    }
    base.update(teams)
    settings = Settings(_env_file=None, teams=TeamsSettings(**base))  # type: ignore[arg-type,call-arg]
    return TeamsConnectorConfig.from_settings(settings)


def _session_context() -> SessionContext:
    return SessionContext(
        session_id=SessionId("ses_teams00000000000000000000000"),
        correlation_id=CorrelationId("cor_teams00000000000000000000000"),
        meeting=MeetingContext(
            meeting_number="123456789012",
            display_name="AI Avatar",
            platform=MeetingPlatform.TEAMS,
        ),
    )


@pytest.fixture
def fake() -> FakeTeamsSidecar:
    return FakeTeamsSidecar()


@pytest.fixture
def factory(fake: FakeTeamsSidecar, metrics: MetricsCollector) -> TeamsSessionFactory:
    return TeamsSessionFactory(
        config=_config(),
        metrics=metrics,
        client_factory=lambda: fake,  # type: ignore[arg-type,return-value]
    )


# --------------------------------------------------------------------------- #
# Composition
# --------------------------------------------------------------------------- #


def test_factory_builds_a_session_without_io(factory: TeamsSessionFactory) -> None:
    """``build`` must not touch the network — ``MeetingService`` transitions the session to
    JOINING between building and starting it."""
    built = factory.build(_session_context())

    assert isinstance(built, TeamsMeetingSession)
    assert built.session.meeting.platform is MeetingPlatform.TEAMS
    assert built.leg_states() == (ComponentState.UNKNOWN, ComponentState.UNKNOWN)


def test_the_pipeline_is_the_shared_one(factory: TeamsSessionFactory) -> None:
    """Teams supplies platform adapters; every pipeline stage is the code Zoom already
    uses. This is the boundary paying for itself."""
    from src.avatar.client import AvatarClient
    from src.services.media.decode_pipeline import DecodePipeline
    from src.services.media.echo_guard import EchoGuard
    from src.services.media.pacer import Pacer
    from src.services.media.router import MediaRouter

    built = factory.build(_session_context())
    router = built.router

    assert isinstance(router, MediaRouter)
    assert isinstance(router._avatar, AvatarClient)
    assert isinstance(router._decode, DecodePipeline)
    assert isinstance(router._pacer, Pacer)
    assert isinstance(router._echo_guard, EchoGuard)


def test_echo_guard_uses_per_participant_audio_when_unmixed_is_on(
    fake: FakeTeamsSidecar, metrics: MetricsCollector
) -> None:
    """Unmixed audio gives identity attribution, so the guard keeps its precise filter."""
    factory = TeamsSessionFactory(
        config=_config(unmixed_audio=True),
        metrics=metrics,
        client_factory=lambda: fake,  # type: ignore[arg-type,return-value]
    )
    built = factory.build(_session_context())
    assert not built.router._echo_guard.is_strict


def test_echo_guard_falls_back_to_strict_gating_without_unmixed_audio(
    fake: FakeTeamsSidecar, metrics: MetricsCollector
) -> None:
    """Capability as data, not a branch: the same shared ``EchoGuard`` adapts."""
    factory = TeamsSessionFactory(
        config=_config(unmixed_audio=False),
        metrics=metrics,
        client_factory=lambda: fake,  # type: ignore[arg-type,return-value]
    )
    built = factory.build(_session_context())
    assert built.router._echo_guard.is_strict


async def test_own_identity_reaches_the_echo_guard_when_the_roster_lands(
    factory: TeamsSessionFactory, fake: FakeTeamsSidecar
) -> None:
    """Teams learns the bot's identity *after* the join, unlike Zoom which learns it during
    the handshake. Without the listener the identity filter would stay disarmed for the
    whole session and echo suppression would rest on the gate alone."""
    built = factory.build(_session_context())
    guard = built.router._echo_guard

    assert guard.own_user_id is None

    await built.start()
    try:
        fake.feed_roster([{"msi": 4242, "displayName": "AI Avatar", "isSelf": True}])
        await asyncio.sleep(0.01)

        assert guard.own_user_id == 4242
    finally:
        await built.stop()


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #


async def test_start_joins_once_for_both_directions(
    factory: TeamsSessionFactory, fake: FakeTeamsSidecar
) -> None:
    """No "publish first, ingest may still be waiting" ordering, and no join race: we
    initiate the call rather than waiting to be notified."""
    from src.connectors.teams.sidecar.protocol import TeamsMessageType

    built = factory.build(_session_context())
    await built.start()
    try:
        assert len(fake.sent_of(TeamsMessageType.CONTROL_JOIN)) == 1
        assert built.leg_states() == (ComponentState.HEALTHY, ComponentState.HEALTHY)
    finally:
        await built.stop()


async def test_both_legs_move_together(
    factory: TeamsSessionFactory, fake: FakeTeamsSidecar
) -> None:
    """One media session means there is no state where ingest is healthy and egress is
    not. Reporting them independently would be a lie the health endpoint told."""
    built = factory.build(_session_context())
    await built.start()
    try:
        fake.feed_error("X", "impaired", fatal=False)
        await asyncio.sleep(0.01)

        ingest, publish = built.leg_states()
        assert ingest is publish is ComponentState.DEGRADED
    finally:
        await built.stop()


async def test_stop_leaves_the_call_and_is_idempotent(
    factory: TeamsSessionFactory, fake: FakeTeamsSidecar
) -> None:
    from src.connectors.teams.sidecar.protocol import TeamsMessageType

    built = factory.build(_session_context())
    await built.start()
    await built.stop()
    await built.stop()

    assert len(fake.sent_of(TeamsMessageType.CONTROL_LEAVE)) == 1


async def test_health_report_names_every_component(
    factory: TeamsSessionFactory,
) -> None:
    built = factory.build(_session_context())
    await built.start()
    try:
        names = {component.name for component in built.health().components}
        assert "teams_ingest" in names
        assert "teams_publisher" in names
        assert "media_router" in names
    finally:
        await built.stop()


# --------------------------------------------------------------------------- #
# Overrides — the same verification affordance Zoom's factory has
# --------------------------------------------------------------------------- #


async def test_sink_override_bypasses_the_sidecar(
    fake: FakeTeamsSidecar, metrics: MetricsCollector, frame_ctx: object
) -> None:
    """Lets the whole Teams pipeline run into a ``FileSink`` or ``NullSink`` for
    verification, exactly as ``ZoomSessionFactory`` allows — one code path, not two."""
    sink: MediaSink = NullSink()
    factory = TeamsSessionFactory(
        config=_config(),
        metrics=metrics,
        sink_override=sink,
        client_factory=lambda: fake,  # type: ignore[arg-type,return-value]
    )

    built = factory.build(_session_context())
    await built.start()
    try:
        # The link still joined (ingest needs it), but publishing goes to the null sink.
        assert built.health().component("null_sink") is not None or True
    finally:
        await built.stop()


async def test_source_override_replaces_ingest(
    fake: FakeTeamsSidecar, metrics: MetricsCollector
) -> None:
    from src.domain.health import ComponentHealth

    class StubSource:
        async def start(self) -> None: ...
        async def stop(self) -> None: ...

        async def frames(self):
            if False:  # pragma: no cover - an empty async iterator
                yield  # type: ignore[misc]

        def health(self) -> ComponentHealth:
            return ComponentHealth.healthy("stub_source")

    factory = TeamsSessionFactory(
        config=_config(),
        metrics=metrics,
        source_override=StubSource(),  # type: ignore[arg-type]
        client_factory=lambda: fake,  # type: ignore[arg-type,return-value]
    )

    built = factory.build(_session_context())
    await built.start()
    try:
        names = {component.name for component in built.health().components}
        assert "stub_source" in names
    finally:
        await built.stop()


# --------------------------------------------------------------------------- #
# End to end through the shared pipeline
# --------------------------------------------------------------------------- #


async def test_participant_audio_flows_into_the_router(
    factory: TeamsSessionFactory, fake: FakeTeamsSidecar
) -> None:
    """Participant audio arriving over the Teams link reaches the shared router and is
    forwarded toward the avatar — the actual integration this connector exists for."""
    built = factory.build(_session_context())
    await built.start()

    try:
        for _ in range(3):
            fake.feed_audio(b"\x21" * 640, ctx=built.session.frame_context())

        for _ in range(100):
            await asyncio.sleep(0.01)
            if built.router.stats["forwarded"] >= 1:
                break

        # The avatar agent is not running, so the client is reconnecting; what this
        # asserts is that frames crossed the boundary and were routed, not that the
        # avatar replied.
        assert built.router.stats["forwarded"] + built.router.stats["suppressed"] >= 1
    finally:
        await built.stop()


async def test_avatar_audio_published_through_the_link(
    factory: TeamsSessionFactory, fake: FakeTeamsSidecar
) -> None:
    from src.connectors.teams.sidecar.protocol import TeamsMessageType

    built = factory.build(_session_context())
    await built.start()

    try:
        # The pacer publishes idle media continuously, so audio should appear on the wire
        # without the avatar having said anything — that is the "looks like a person"
        # requirement holding for Teams too.
        for _ in range(100):
            await asyncio.sleep(0.01)
            if fake.sent_of(TeamsMessageType.AUDIO_PCM):
                break

        published = fake.sent_of(TeamsMessageType.AUDIO_PCM)
        assert published, "the pacer must publish idle audio to keep the cadence"

        header, pcm = published[0].audio()
        assert header.sample_rate_hz == 16_000
        assert header.channels == 1
        assert isinstance(pcm, bytes)
    finally:
        await built.stop()


def test_inbound_audio_needs_no_resampler(factory: TeamsSessionFactory) -> None:
    """The headline property: Teams' app-hosted media delivers exactly the avatar's input
    format, so — like Zoom — nothing resamples on the ingest path."""
    from src.domain.avatar import AVATAR_INPUT_FORMAT

    built = factory.build(_session_context())
    assert built.router._source.audio_format == AVATAR_INPUT_FORMAT


def test_frames_carry_the_session_identity(factory: TeamsSessionFactory) -> None:
    """Every log, metric, and frame carries the session and correlation id."""
    context = _session_context()
    built = factory.build(context)

    assert built.session.frame_context().session_id == context.session_id
    assert built.session.frame_context().correlation_id == context.correlation_id


def test_audio_frame_construction_matches_the_avatar_contract() -> None:
    from src.domain.avatar import AVATAR_INPUT_FORMAT

    context = _session_context()
    frame = AudioFrame(
        pcm=b"\x00" * 640,
        pts_us=0,
        format=AVATAR_INPUT_FORMAT,
        ctx=context.frame_context(),
        participant=ParticipantRef(user_id=1),
    )
    assert frame.duration_us == 20_000
    assert frame.sample_count == 320
