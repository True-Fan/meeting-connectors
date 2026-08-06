"""Regression: a session's media legs must survive their own start.

**A real bug, found while wiring Teams and fixed in shared code.** ``MediaRouter.run``
starts four legs in one ``asyncio.TaskGroup``, two of which iterate
``decode.decoder.video()`` and ``.audio()``. But the decoder does not exist until the
avatar streams its first chunk — ``DecodePipeline.feed`` starts it lazily — and
``FfmpegDecoder.video()`` raises ``FfmpegDecoderError("video() called before start()")``
before that. So both legs raised immediately, the task group unwound, and every session
tore down its own media pipeline microseconds after starting it.

It reproduced identically through ``ZoomSessionFactory`` with no Teams code loaded, so it
predates this connector: it was invisible only because nothing exercised ``MediaRouter.run``
end to end. The fix is ``DecodePipeline.wait_started()``, awaited by both output legs.

Both connectors are covered here because the bug and the fix are in shared code, and a
regression in either direction should fail loudly.
"""

from __future__ import annotations

import asyncio

import pytest
from pydantic import SecretStr

from src.config.settings import Settings, TeamsSettings
from src.connectors.teams.config import TeamsConnectorConfig
from src.connectors.teams.session.teams_session import TeamsSessionFactory
from src.connectors.zoom.config import ZoomConnectorConfig
from src.connectors.zoom.session.zoom_session import ZoomSessionFactory
from src.domain.context import FrameContext
from src.domain.health import ComponentHealth
from src.domain.ids import CorrelationId, SessionId
from src.domain.media import AudioFormat, AudioFrame, MediaChunk, VideoFormat
from src.domain.meeting import MeetingContext, MeetingPlatform
from src.domain.session import SessionContext
from src.services.media.decode_pipeline import DecodePipeline
from src.services.media.sinks.null_sink import NullSink
from tests.fakes.decoder import FakeDecoder
from tests.fakes.teams_sidecar import FakeTeamsSidecar


class SilentSource:
    """An ``AudioSource`` that never yields — the meeting is quiet."""

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def frames(self):
        await asyncio.Event().wait()  # never completes
        yield  # pragma: no cover

    def health(self) -> ComponentHealth:
        return ComponentHealth.healthy("silent_source")


def _fake_decoder(ctx: FrameContext) -> FakeDecoder:
    return FakeDecoder(
        ctx=ctx,
        video_format=VideoFormat(width=64, height=48, fps=30),
        audio_format=AudioFormat(sample_rate_hz=16_000, channels=1),
    )


def _session(platform: MeetingPlatform) -> SessionContext:
    return SessionContext(
        session_id=SessionId("ses_regress0000000000000000000000"),
        correlation_id=CorrelationId("cor_regress0000000000000000000000"),
        meeting=MeetingContext(
            meeting_number="1234567890", display_name="AI Avatar", platform=platform
        ),
    )


# --------------------------------------------------------------------------- #
# The unit-level guarantee
# --------------------------------------------------------------------------- #


async def test_wait_started_blocks_until_the_decoder_starts(frame_ctx: FrameContext) -> None:
    pipeline = DecodePipeline(decoder=_fake_decoder(frame_ctx), ctx=frame_ctx)

    waiter = asyncio.create_task(pipeline.wait_started())
    await asyncio.sleep(0.01)
    assert not waiter.done()
    assert not pipeline.is_started

    await pipeline.start()

    await asyncio.wait_for(waiter, timeout=1.0)
    assert pipeline.is_started


async def test_wait_started_returns_immediately_once_started(
    frame_ctx: FrameContext,
) -> None:
    pipeline = DecodePipeline(decoder=_fake_decoder(frame_ctx), ctx=frame_ctx)
    await pipeline.start()

    await asyncio.wait_for(pipeline.wait_started(), timeout=1.0)


async def test_lazy_start_via_feed_also_releases_waiters(frame_ctx: FrameContext) -> None:
    """The production path: the decoder starts when the avatar's first chunk lands."""
    pipeline = DecodePipeline(decoder=_fake_decoder(frame_ctx), ctx=frame_ctx)
    waiter = asyncio.create_task(pipeline.wait_started())

    await pipeline.feed(
        MediaChunk(data=b"ftypmoov", seq=0, received_at_us=0, ctx=frame_ctx, is_init_segment=True)
    )

    await asyncio.wait_for(waiter, timeout=1.0)


async def test_stop_re_arms_the_gate(frame_ctx: FrameContext) -> None:
    """After a stop, a leg must wait again rather than iterating a dead decoder."""
    pipeline = DecodePipeline(decoder=_fake_decoder(frame_ctx), ctx=frame_ctx)
    await pipeline.start()
    await pipeline.stop()

    assert not pipeline.is_started
    waiter = asyncio.create_task(pipeline.wait_started())
    await asyncio.sleep(0.01)
    assert not waiter.done()

    waiter.cancel()


async def test_restart_releases_waiters_again(frame_ctx: FrameContext) -> None:
    pipeline = DecodePipeline(decoder=_fake_decoder(frame_ctx), ctx=frame_ctx)
    await pipeline.feed(
        MediaChunk(data=b"init", seq=0, received_at_us=0, ctx=frame_ctx, is_init_segment=True)
    )
    await pipeline.stop()

    assert await pipeline.restart()
    await asyncio.wait_for(pipeline.wait_started(), timeout=1.0)


# --------------------------------------------------------------------------- #
# Both connectors, end to end
# --------------------------------------------------------------------------- #


async def test_zoom_session_starts_and_stops_without_killing_its_task_group() -> None:
    """The original repro. No Teams code is involved."""
    factory = ZoomSessionFactory(
        config=ZoomConnectorConfig.from_settings(Settings(_env_file=None)),  # type: ignore[call-arg]
        sink_override=NullSink(),
        source_override=SilentSource(),  # type: ignore[arg-type]
    )
    built = factory.build(_session(MeetingPlatform.ZOOM))

    await built.start()
    await asyncio.sleep(0.05)
    await built.stop()  # must not raise an ExceptionGroup


async def test_teams_session_starts_and_stops_without_killing_its_task_group() -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        teams=TeamsSettings(
            tenant_id="72f988bf-86f1-41af-91ab-2d7cd011db47",
            client_id="8b081ef6-4792-4def-b2c9-c363a1bf41d5",
            client_secret=SecretStr("secret"),
            sidecar_host="teams-bot.internal",
        ),
    )
    fake = FakeTeamsSidecar()
    factory = TeamsSessionFactory(
        config=TeamsConnectorConfig.from_settings(settings),
        sink_override=NullSink(),
        source_override=SilentSource(),  # type: ignore[arg-type]
        client_factory=lambda: fake,  # type: ignore[arg-type,return-value]
    )
    built = factory.build(_session(MeetingPlatform.TEAMS))

    await built.start()
    await asyncio.sleep(0.05)
    await built.stop()


@pytest.mark.parametrize("platform", [MeetingPlatform.ZOOM, MeetingPlatform.TEAMS])
async def test_a_quiet_session_keeps_publishing_idle_media(
    platform: MeetingPlatform,
) -> None:
    """The consequence of the bug, stated as a behaviour: with the legs dead the pacer
    died too, so the avatar's camera froze the instant it joined. A silent meeting must
    still produce a continuous outbound cadence (doc 003 §1.4)."""
    sink = NullSink()

    if platform is MeetingPlatform.ZOOM:
        built = ZoomSessionFactory(
            config=ZoomConnectorConfig.from_settings(Settings(_env_file=None)),  # type: ignore[call-arg]
            sink_override=sink,
            source_override=SilentSource(),  # type: ignore[arg-type]
        ).build(_session(platform))
    else:
        settings = Settings(  # type: ignore[call-arg]
            _env_file=None,
            teams=TeamsSettings(
                tenant_id="72f988bf-86f1-41af-91ab-2d7cd011db47",
                client_id="8b081ef6-4792-4def-b2c9-c363a1bf41d5",
                client_secret=SecretStr("secret"),
                sidecar_host="teams-bot.internal",
            ),
        )
        built = TeamsSessionFactory(
            config=TeamsConnectorConfig.from_settings(settings),
            sink_override=sink,
            source_override=SilentSource(),  # type: ignore[arg-type]
            client_factory=lambda: FakeTeamsSidecar(),  # type: ignore[arg-type,return-value]
        ).build(_session(platform))

    await built.start()
    try:
        for _ in range(100):
            await asyncio.sleep(0.01)
            if built.router.stats.get("published_video", 0) > 0:
                break

        stats = built.router.stats
        assert stats.get("published_video", 0) > 0, (
            f"the pacer stopped publishing on {platform}: {stats}"
        )
    finally:
        await built.stop()


def test_audio_frame_sanity(frame_ctx: FrameContext) -> None:
    """Anchors the format assumption the legs above rely on."""
    from src.domain.avatar import AVATAR_INPUT_FORMAT

    frame = AudioFrame(
        pcm=b"\x00" * 640, pts_us=0, format=AVATAR_INPUT_FORMAT, ctx=frame_ctx
    )
    assert frame.duration_us == 20_000
