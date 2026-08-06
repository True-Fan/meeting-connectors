"""TeamsSidecarLink — join, demux, attribution, backpressure, recovery.

Every test here drives the **real** link against ``FakeTeamsSidecar``, which speaks the
real wire protocol. No Windows host, no Azure tenant, no Graph consent — the same way
``FakeRtmsTransport`` covers RTMS's protocol logic without a Zoom account.
"""

from __future__ import annotations

import asyncio

import pytest
from pydantic import SecretStr

from src.config.settings import Settings, TeamsSettings
from src.connectors.teams.config import TeamsConnectorConfig
from src.connectors.teams.exceptions import (
    SidecarFatalError,
    SidecarUnavailableError,
    TeamsConfigurationError,
)
from src.connectors.teams.ingest.audio_source import TeamsAudioSource
from src.connectors.teams.publisher.publisher import TeamsMediaSink
from src.connectors.teams.sidecar.link import VIDEO_BACKPRESSURE_BYTES, TeamsSidecarLink
from src.connectors.teams.sidecar.protocol import CallState, TeamsMessageType
from src.domain.context import FrameContext
from src.domain.health import ComponentState
from src.domain.media import AudioFormat, AudioFrame, VideoFormat, VideoFrame
from src.domain.meeting import MeetingContext, MeetingPlatform, ParticipantRef
from src.infrastructure.reconnect import ReconnectPolicy
from src.services.media.clock import MediaClock
from tests.fakes.teams_sidecar import DEFAULT_READY, FakeTeamsSidecar

TENANT = "72f988bf-86f1-41af-91ab-2d7cd011db47"
AUDIO_FORMAT = AudioFormat(sample_rate_hz=16_000, channels=1)


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


def _meeting(number: str = "123456789012") -> MeetingContext:
    return MeetingContext(
        meeting_number=number,
        display_name="AI Avatar",
        platform=MeetingPlatform.TEAMS,
    )


def _link(
    fake: FakeTeamsSidecar,
    *,
    config: TeamsConnectorConfig | None = None,
    ctx: FrameContext,
    policy: ReconnectPolicy | None = None,
) -> TeamsSidecarLink:
    return TeamsSidecarLink(
        config=config or _config(),
        ctx=ctx,
        clock=MediaClock(),
        policy=policy,
        client_factory=lambda: fake,  # type: ignore[arg-type,return-value]
    )


def _audio(ctx: FrameContext, *, pts_us: int = 0, pcm: bytes | None = None) -> AudioFrame:
    return AudioFrame(
        pcm=pcm if pcm is not None else b"\x11" * 640,
        pts_us=pts_us,
        format=AUDIO_FORMAT,
        ctx=ctx,
    )


def _video(ctx: FrameContext) -> VideoFrame:
    video_format = VideoFormat(width=1280, height=720, fps=30)
    return VideoFrame(
        planes=b"\x80" * video_format.frame_size_bytes,
        pts_us=0,
        format=video_format,
        ctx=ctx,
    )


# --------------------------------------------------------------------------- #
# Join
# --------------------------------------------------------------------------- #


async def test_join_sends_credentials_and_the_descriptor(frame_ctx: FrameContext) -> None:
    fake = FakeTeamsSidecar()
    link = _link(fake, ctx=frame_ctx)

    await link.start(_meeting())
    try:
        payload = fake.join_payload()

        assert payload["sessionId"] == frame_ctx.session_id
        assert payload["correlationId"] == frame_ctx.correlation_id
        assert payload["join"]["mode"] == "meeting_id"
        assert payload["join"]["joinMeetingId"] == "123456789012"
        assert payload["auth"]["tenantId"] == TENANT
        assert payload["auth"]["clientSecret"] == "secret"
        assert payload["audio"] == {"sampleRateHz": 16_000, "channels": 1, "unmixed": True}
        assert payload["video"] == {"width": 1280, "height": 720, "fps": 30}

        assert link.is_joined
        assert link.health().state is ComponentState.HEALTHY
        assert link.call_state is CallState.ESTABLISHED
    finally:
        await link.stop()


async def test_join_fails_fast_when_unconfigured(frame_ctx: FrameContext) -> None:
    """A missing credential must fail session creation, not a live meeting."""
    fake = FakeTeamsSidecar()
    link = _link(fake, config=_config(tenant_id=""), ctx=frame_ctx)

    with pytest.raises(TeamsConfigurationError, match=r"teams\.tenant_id"):
        await link.start(_meeting())

    assert fake.connect_calls == 0  # nothing was even dialled


async def test_fatal_error_during_join_propagates(frame_ctx: FrameContext) -> None:
    """A rejected credential is fatal: retrying it ten times ends in the same place."""
    fake = FakeTeamsSidecar(auto_ready=False)
    link = _link(fake, ctx=frame_ctx)

    async def _reject() -> None:
        await asyncio.sleep(0)
        fake.feed_error("GRAPH_403", "missing Calls.AccessMedia.All consent", fatal=True)

    task = asyncio.create_task(_reject())
    with pytest.raises(SidecarFatalError, match=r"Calls\.AccessMedia\.All"):
        await link.start(_meeting())
    await task


async def test_ready_timeout_is_surfaced(frame_ctx: FrameContext) -> None:
    fake = FakeTeamsSidecar(auto_ready=False)
    link = _link(fake, config=_config(sidecar_ready_timeout_s=0.05), ctx=frame_ctx)

    with pytest.raises(SidecarUnavailableError, match="did not send READY"):
        await link.start(_meeting())


async def test_negotiated_audio_mismatch_is_fatal(frame_ctx: FrameContext) -> None:
    """Silently accepting the wrong rate produces pitch-shifted speech that reads as an
    avatar bug. Refusing names the real cause."""
    fake = FakeTeamsSidecar(ready={**DEFAULT_READY, "audioSampleRateHz": 48_000})
    link = _link(fake, ctx=frame_ctx)

    with pytest.raises(SidecarFatalError, match="AUDIO_FORMAT_MISMATCH"):
        await link.start(_meeting())


async def test_negotiated_video_mismatch_is_fatal(frame_ctx: FrameContext) -> None:
    ready = dict(DEFAULT_READY)
    ready.update({"videoWidth": 640, "videoHeight": 360})
    link = _link(FakeTeamsSidecar(ready=ready), ctx=frame_ctx)

    with pytest.raises(SidecarFatalError, match="VIDEO_FORMAT_MISMATCH"):
        await link.start(_meeting())


async def test_unmixed_audio_downgrade_is_tolerated(frame_ctx: FrameContext) -> None:
    """Not fatal: the meeting still works, EchoGuard just leans on its speaking gate."""
    ready = dict(DEFAULT_READY)
    ready["unmixedAudio"] = False
    link = _link(FakeTeamsSidecar(ready=ready), ctx=frame_ctx)

    await link.start(_meeting())
    try:
        assert link.is_joined
        assert link.ready is not None
        assert link.ready.unmixed_audio is False
    finally:
        await link.stop()


async def test_self_msi_in_ready_arms_the_identity_filter(frame_ctx: FrameContext) -> None:
    ready = dict(DEFAULT_READY)
    ready["selfMsi"] = 4242
    link = _link(FakeTeamsSidecar(ready=ready), ctx=frame_ctx)

    await link.start(_meeting())
    try:
        assert link.own_participant() == ParticipantRef(user_id=4242, display_name="AI Avatar")
    finally:
        await link.stop()


# --------------------------------------------------------------------------- #
# Inbound audio
# --------------------------------------------------------------------------- #


async def test_inbound_audio_reaches_the_queue_as_domain_frames(
    frame_ctx: FrameContext,
) -> None:
    fake = FakeTeamsSidecar()
    link = _link(fake, ctx=frame_ctx)
    await link.start(_meeting())

    try:
        fake.feed_audio(b"\x05" * 640, ctx=frame_ctx)
        frame = await asyncio.wait_for(link.audio_queue().get(), timeout=1.0)

        assert isinstance(frame, AudioFrame)
        assert frame.pcm == b"\x05" * 640
        assert frame.format == AUDIO_FORMAT
        assert frame.ctx == frame_ctx
        assert frame.participant is None  # mixed stream
    finally:
        await link.stop()


async def test_unmixed_audio_is_attributed_to_a_participant(frame_ctx: FrameContext) -> None:
    fake = FakeTeamsSidecar()
    link = _link(fake, ctx=frame_ctx)
    await link.start(_meeting())

    try:
        fake.feed_roster([{"msi": 77, "displayName": "Dev", "isSelf": False}])
        await asyncio.sleep(0)  # let the roster land before the audio
        fake.feed_audio(b"\x06" * 640, ctx=frame_ctx, source_msi=77, unmixed=True)

        frame = await asyncio.wait_for(link.audio_queue().get(), timeout=1.0)
        assert frame.participant == ParticipantRef(user_id=77, display_name="Dev")
    finally:
        await link.stop()


async def test_unknown_source_id_still_yields_an_identity(frame_ctx: FrameContext) -> None:
    """Rosters and audio race. Dropping the id would look like a mixed stream and
    disable the own-participant filter for as long as the race lasts."""
    fake = FakeTeamsSidecar()
    link = _link(fake, ctx=frame_ctx)
    await link.start(_meeting())

    try:
        fake.feed_audio(b"\x07" * 640, ctx=frame_ctx, source_msi=999, unmixed=True)
        frame = await asyncio.wait_for(link.audio_queue().get(), timeout=1.0)
        assert frame.participant == ParticipantRef(user_id=999, display_name=None)
    finally:
        await link.stop()


async def test_wrong_ingest_format_is_dropped_not_forwarded(frame_ctx: FrameContext) -> None:
    """The zero-resample property is *checked*: a sidecar sending 48 kHz must not reach
    the avatar, which cannot use it."""
    fake = FakeTeamsSidecar()
    link = _link(fake, ctx=frame_ctx)
    await link.start(_meeting())

    try:
        fake.feed_audio(b"\x08" * 640, ctx=frame_ctx, sample_rate_hz=48_000)
        fake.feed_audio(b"\x09" * 640, ctx=frame_ctx)  # a good frame behind it

        frame = await asyncio.wait_for(link.audio_queue().get(), timeout=1.0)
        assert frame.pcm == b"\x09" * 640  # the 48 kHz frame never arrived
        assert link.stats["dropped_audio"] == 1
    finally:
        await link.stop()


async def test_roster_reports_the_bots_own_identity(frame_ctx: FrameContext) -> None:
    fake = FakeTeamsSidecar()
    link = _link(fake, ctx=frame_ctx)

    seen: list[ParticipantRef] = []
    link.add_participant_listener(seen.append)

    await link.start(_meeting())
    try:
        fake.feed_roster(
            [
                {"msi": 11, "displayName": "Human", "isSelf": False},
                {"msi": 22, "displayName": "AI Avatar", "isSelf": True},
            ]
        )
        await asyncio.sleep(0)

        assert link.own_participant() == ParticipantRef(user_id=22, display_name="AI Avatar")
        assert seen == [ParticipantRef(user_id=22, display_name="AI Avatar")]
        assert link.stats["roster"] == 2
    finally:
        await link.stop()


async def test_a_late_listener_is_told_immediately(frame_ctx: FrameContext) -> None:
    """Subscribing after the identity is known must not miss it — otherwise the echo
    filter stays disarmed depending on wiring order."""
    ready = dict(DEFAULT_READY)
    ready["selfMsi"] = 55
    link = _link(FakeTeamsSidecar(ready=ready), ctx=frame_ctx)
    await link.start(_meeting())

    try:
        seen: list[ParticipantRef] = []
        link.add_participant_listener(seen.append)
        assert [p.user_id for p in seen] == [55]
    finally:
        await link.stop()


async def test_terminated_call_degrades_the_link(frame_ctx: FrameContext) -> None:
    fake = FakeTeamsSidecar()
    link = _link(fake, ctx=frame_ctx)
    await link.start(_meeting())

    try:
        fake.feed_call_state(int(CallState.TERMINATED), reason="meeting ended")
        await asyncio.sleep(0)

        assert link.call_state is CallState.TERMINATED
        health = link.health()
        assert health.state is ComponentState.DEGRADED
        assert "meeting ended" in (health.detail or "")
    finally:
        await link.stop()


async def test_heartbeat_is_echoed(frame_ctx: FrameContext) -> None:
    """The sidecar measures the round trip across the host boundary from the echo."""
    fake = FakeTeamsSidecar()
    link = _link(fake, ctx=frame_ctx)
    await link.start(_meeting())

    try:
        fake.feed_heartbeat(sent_at_us=98_765)
        await asyncio.sleep(0.01)

        beats = fake.sent_of(TeamsMessageType.HEARTBEAT)
        assert beats
        assert beats[-1].json() == {"sent_at_us": 98_765}
    finally:
        await link.stop()


async def test_non_fatal_error_degrades_without_failing(frame_ctx: FrameContext) -> None:
    fake = FakeTeamsSidecar()
    link = _link(fake, ctx=frame_ctx)
    await link.start(_meeting())

    try:
        fake.feed_error("VIDEO_GEOMETRY_MISMATCH", "dropped a frame", fatal=False)
        await asyncio.sleep(0)
        assert link.health().state is ComponentState.DEGRADED
    finally:
        await link.stop()


async def test_fatal_error_mid_session_fails_the_leg(frame_ctx: FrameContext) -> None:
    fake = FakeTeamsSidecar()
    link = _link(fake, ctx=frame_ctx)
    await link.start(_meeting())

    try:
        fake.feed_error("MEDIA_PLATFORM_INIT", "certificate expired", fatal=True)
        await asyncio.sleep(0)
        assert link.health().state is ComponentState.UNHEALTHY
        assert "certificate expired" in (link.health().detail or "")
    finally:
        await link.stop()


# --------------------------------------------------------------------------- #
# Outbound media
# --------------------------------------------------------------------------- #


async def test_publishing_audio_and_video_reaches_the_sidecar(
    frame_ctx: FrameContext,
) -> None:
    fake = FakeTeamsSidecar()
    link = _link(fake, ctx=frame_ctx)
    await link.start(_meeting())

    try:
        await link.publish_audio(_audio(frame_ctx, pts_us=1_000))
        await link.publish_video(_video(frame_ctx))

        audio = fake.sent_of(TeamsMessageType.AUDIO_PCM)
        video = fake.sent_of(TeamsMessageType.VIDEO_I420)

        assert len(audio) == 1
        assert audio[0].pts_us == 1_000
        assert audio[0].audio()[1] == b"\x11" * 640
        assert len(video) == 1
        assert video[0].video()[0].width == 1280
    finally:
        await link.stop()


async def test_video_is_dropped_under_backpressure_but_audio_is_not(
    frame_ctx: FrameContext,
) -> None:
    """The same policy as Zoom's publisher, for the same reason: a lost video frame costs
    one frame of smoothness, a lost audio frame is an audible gap."""
    fake = FakeTeamsSidecar()
    link = _link(fake, ctx=frame_ctx)
    await link.start(_meeting())

    try:
        fake.set_write_buffer_size(VIDEO_BACKPRESSURE_BYTES + 1)

        await link.publish_video(_video(frame_ctx))
        await link.publish_audio(_audio(frame_ctx))

        assert fake.sent_of(TeamsMessageType.VIDEO_I420) == []
        assert len(fake.sent_of(TeamsMessageType.AUDIO_PCM)) == 1
        assert link.stats["dropped_video"] == 1
        assert link.health().state is ComponentState.DEGRADED
    finally:
        await link.stop()


async def test_publishing_while_the_link_is_down_is_absorbed(
    frame_ctx: FrameContext,
) -> None:
    """The pacer is shared with Zoom and runs a continuous cadence. Letting an error
    escape would tear down its task group mid-reconnect, when the link is seconds from
    healing itself."""
    fake = FakeTeamsSidecar()
    link = _link(fake, ctx=frame_ctx)
    await link.start(_meeting())
    await link.stop()

    # Must not raise.
    await link.publish_audio(_audio(frame_ctx))
    await link.publish_video(_video(frame_ctx))
    assert link.stats["dropped_audio"] >= 1


async def test_sequence_numbers_advance_independently(frame_ctx: FrameContext) -> None:
    fake = FakeTeamsSidecar()
    link = _link(fake, ctx=frame_ctx)
    await link.start(_meeting())

    try:
        for _ in range(3):
            await link.publish_audio(_audio(frame_ctx))
            await link.publish_video(_video(frame_ctx))

        assert [m.seq for m in fake.sent_of(TeamsMessageType.AUDIO_PCM)] == [0, 1, 2]
        assert [m.seq for m in fake.sent_of(TeamsMessageType.VIDEO_I420)] == [0, 1, 2]
    finally:
        await link.stop()


# --------------------------------------------------------------------------- #
# Teardown and recovery
# --------------------------------------------------------------------------- #


async def test_stop_leaves_the_call_then_closes(frame_ctx: FrameContext) -> None:
    fake = FakeTeamsSidecar()
    link = _link(fake, ctx=frame_ctx)
    await link.start(_meeting())
    await link.stop()

    leaves = fake.sent_of(TeamsMessageType.CONTROL_LEAVE)
    assert len(leaves) == 1
    assert leaves[0].json() == {"reason": "session_stop"}
    assert fake.close_calls >= 1
    assert link.health().state is ComponentState.UNKNOWN


async def test_stop_is_idempotent(frame_ctx: FrameContext) -> None:
    fake = FakeTeamsSidecar()
    link = _link(fake, ctx=frame_ctx)
    await link.start(_meeting())

    await link.stop()
    await link.stop()  # must not raise
    assert len(fake.sent_of(TeamsMessageType.CONTROL_LEAVE)) == 1


async def test_start_is_idempotent(frame_ctx: FrameContext) -> None:
    fake = FakeTeamsSidecar()
    link = _link(fake, ctx=frame_ctx)
    await link.start(_meeting())
    try:
        await link.start(_meeting())
        assert len(fake.sent_of(TeamsMessageType.CONTROL_JOIN)) == 1
    finally:
        await link.stop()


async def test_link_loss_triggers_a_full_rejoin(frame_ctx: FrameContext) -> None:
    """Teams recovers as one unit: a media session cannot be re-attached to a call whose
    signalling has gone, so reconnect re-creates the call. Doc 002 called this
    ``ReconnectScope.FULL``; the enum was cut, the behaviour is what matters."""
    fake = FakeTeamsSidecar()
    link = _link(
        fake,
        ctx=frame_ctx,
        policy=ReconnectPolicy(initial_delay_s=0.01, max_delay_s=0.02, max_attempts=3),
    )
    await link.start(_meeting())

    try:
        assert len(fake.sent_of(TeamsMessageType.CONTROL_JOIN)) == 1

        fake.feed_eof()  # the sidecar crashed or the call ended

        for _ in range(200):
            await asyncio.sleep(0.01)
            if link.reconnects >= 1:
                break

        assert link.reconnects == 1
        assert len(fake.sent_of(TeamsMessageType.CONTROL_JOIN)) == 2
        assert link.health().state is ComponentState.HEALTHY
    finally:
        await link.stop()


async def test_reconnect_budget_exhaustion_fails_the_leg(frame_ctx: FrameContext) -> None:
    fake = FakeTeamsSidecar(auto_ready=False)
    link = _link(
        fake,
        config=_config(sidecar_ready_timeout_s=0.02),
        ctx=frame_ctx,
        policy=ReconnectPolicy(initial_delay_s=0.01, max_delay_s=0.01, max_attempts=1),
    )

    # First join succeeds only because we feed READY by hand.
    async def _ready_once() -> None:
        await asyncio.sleep(0)
        fake.feed_ready()

    task = asyncio.create_task(_ready_once())
    await link.start(_meeting())
    await task

    try:
        fake.feed_eof()

        # The leg goes UNHEALTHY as soon as recovery starts, so waiting on the state
        # alone would observe it mid-retry. The budget being spent is a distinct,
        # later event and is what this test is about.
        for _ in range(200):
            await asyncio.sleep(0.01)
            if "exhausted" in (link.health().detail or ""):
                break

        assert link.health().state is ComponentState.UNHEALTHY
        assert "exhausted" in (link.health().detail or "")
    finally:
        await link.stop()


# --------------------------------------------------------------------------- #
# The port adapters
# --------------------------------------------------------------------------- #


async def test_both_legs_report_the_same_health(frame_ctx: FrameContext) -> None:
    """One media session means there is no state where Teams ingest is healthy and Teams
    egress is not. Reporting them independently would be a lie."""
    fake = FakeTeamsSidecar()
    link = _link(fake, ctx=frame_ctx)
    source = TeamsAudioSource(link=link)
    sink = TeamsMediaSink(link=link)

    await link.start(_meeting())
    try:
        assert source.health().state is sink.health().state is ComponentState.HEALTHY
        assert source.health().name == "teams_ingest"
        assert sink.health().name == "teams_publisher"

        fake.feed_error("X", "degraded", fatal=False)
        await asyncio.sleep(0)
        assert source.health().state is sink.health().state is ComponentState.DEGRADED
    finally:
        await link.stop()


async def test_audio_source_yields_frames_and_ends_on_close(frame_ctx: FrameContext) -> None:
    fake = FakeTeamsSidecar()
    link = _link(fake, ctx=frame_ctx)
    source = TeamsAudioSource(link=link)

    await link.start(_meeting())
    await source.start()

    received: list[AudioFrame] = []

    async def _drain() -> None:
        async for frame in source.frames():
            received.append(frame)

    task = asyncio.create_task(_drain())
    fake.feed_audio(b"\x0a" * 640, ctx=frame_ctx)
    await asyncio.sleep(0.02)

    await link.stop()
    await asyncio.wait_for(task, timeout=1.0)  # the iterator must end, not hang

    assert len(received) == 1
    assert source.audio_format == AUDIO_FORMAT


async def test_audio_source_start_stop_do_not_touch_the_link(frame_ctx: FrameContext) -> None:
    """Stopping ingest must not tear down egress as a side effect."""
    fake = FakeTeamsSidecar()
    link = _link(fake, ctx=frame_ctx)
    source = TeamsAudioSource(link=link)

    await link.start(_meeting())
    try:
        await source.start()
        await source.stop()
        assert link.is_joined
        assert fake.sent_of(TeamsMessageType.CONTROL_LEAVE) == []
    finally:
        await link.stop()


async def test_sink_exposes_the_identity_for_echo_suppression(frame_ctx: FrameContext) -> None:
    fake = FakeTeamsSidecar()
    link = _link(fake, ctx=frame_ctx)
    sink = TeamsMediaSink(link=link)

    await link.start(_meeting())
    try:
        assert sink.own_participant() is None  # roster has not landed yet

        fake.feed_roster([{"msi": 33, "displayName": "AI Avatar", "isSelf": True}])
        await asyncio.sleep(0)

        assert sink.own_participant() == ParticipantRef(user_id=33, display_name="AI Avatar")
    finally:
        await link.stop()
