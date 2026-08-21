"""The two media legs — what they assert, and what they refuse to claim.

Small on purpose: both are thin adapters onto the page channel. What is worth guarding is the
handful of invariants whose violation is silent — a frame stamped from the wrong clock, a format
converted instead of asserted, and a health report that editorialises about a silent meeting.
"""

from __future__ import annotations

import asyncio

import pytest

from src.connectors.teams_web.egress.media_sink import TeamsWebMediaSink
from src.connectors.teams_web.ingest.page_audio_source import PageAudioSource
from src.connectors.teams_web.page.protocol import KIND_AUDIO_PCM, decode_audio
from src.connectors.teams_web.page.server import PageAudioServer
from src.domain.avatar import AVATAR_INPUT_FORMAT
from src.domain.context import FrameContext
from src.domain.health import ComponentState
from src.domain.ids import CorrelationId, SessionId
from src.domain.media import AudioFrame, VideoFormat, VideoFrame
from src.domain.meeting import MeetingContext
from src.services.media.clock import MediaClock

PCM = b"\x01\x02" * 320


@pytest.fixture
def ctx() -> FrameContext:
    return FrameContext(
        session_id=SessionId("ses_teamsweb00000000000000000000"),
        correlation_id=CorrelationId("cor_teamsweb00000000000000000000"),
    )


class _FixedClock:
    def __init__(self, now_us: int = 999_000) -> None:
        self._now_us = now_us

    def now_us(self) -> int:
        return self._now_us


class TestIngest:
    @pytest.mark.asyncio
    async def test_a_tapped_frame_is_stamped_from_our_media_clock(
        self, ctx: FrameContext
    ) -> None:
        """**Not the page's clock.** ``AudioContext.currentTime`` runs on the audio device's
        timeline — an arbitrary origin that drifts against the monotonic clock the pipeline is
        paced on — so mixing the two would corrupt the single-clock invariant A/V sync depends
        on. Every transport in this repository arrived at the same rule."""
        server = PageAudioServer()
        source = PageAudioSource(
            server=server, ctx=ctx, clock=_FixedClock(777_000), queue_size=4  # type: ignore[arg-type]
        )
        await source.start()
        source._on_pcm(PCM)

        frame = await anext(source.frames())
        assert frame.pts_us == 777_000
        assert frame.pcm == PCM

    @pytest.mark.asyncio
    async def test_the_format_is_asserted_rather_than_converted(
        self, ctx: FrameContext
    ) -> None:
        """The capture context is constructed at 16 kHz so Web Audio resamples in native code.
        A page running a stale script that built a 48 kHz context would produce a chipmunk voice
        three services downstream; the assertion is where that is caught."""
        server = PageAudioServer()
        source = PageAudioSource(
            server=server, ctx=ctx, clock=MediaClock(), queue_size=4
        )
        await source.start()
        source._on_pcm(PCM)
        frame = await anext(source.frames())
        assert frame.format == AVATAR_INPUT_FORMAT
        assert source.audio_format == AVATAR_INPUT_FORMAT

    @pytest.mark.asyncio
    async def test_a_frame_that_is_not_whole_samples_is_dropped_rather_than_raising(
        self, ctx: FrameContext
    ) -> None:
        """A skew check rather than an expected path — the worklet only posts complete frames —
        and it must not raise, because this runs on the loop carrying the avatar's voice."""
        server = PageAudioServer()
        source = PageAudioSource(
            server=server, ctx=ctx, clock=MediaClock(), queue_size=4
        )
        await source.start()
        source._on_pcm(b"\x01")
        assert source.frames_received == 0

    def test_a_tapped_frame_carries_no_attribution(self, ctx: FrameContext) -> None:
        """The tap sits at playout, where the meeting has already been mixed down to what a
        human would hear. Not recoverable at any price: the individual streams do not exist by
        that point in the graph, which is why who-is-talking comes from the DOM instead."""
        server = PageAudioServer()
        source = PageAudioSource(
            server=server, ctx=ctx, clock=MediaClock(), queue_size=4
        )
        source._on_pcm(PCM)
        assert source.health().state is ComponentState.UNKNOWN  # not started

    @pytest.mark.asyncio
    async def test_silence_is_degraded_and_says_which_two_things_it_could_be(
        self, ctx: FrameContext
    ) -> None:
        """A silent meeting and a tap that never attached are indistinguishable from here, so
        the report states the fact and refuses to editorialise."""
        server = PageAudioServer()
        source = PageAudioSource(
            server=server, ctx=ctx, clock=MediaClock(), queue_size=4
        )
        await source.start()
        health = source.health()
        assert health.state is ComponentState.DEGRADED
        assert health.detail is not None
        assert "silent meeting" in health.detail
        assert "tapped=0" in health.detail

    @pytest.mark.asyncio
    async def test_stopping_ingest_does_not_take_the_page_server_with_it(
        self, ctx: FrameContext
    ) -> None:
        """It is shared with the publisher: stopping it here would take the avatar's voice down
        as a side effect of ingest shutting up. The session stops it once, for both."""
        server = PageAudioServer()
        await server.start()
        source = PageAudioSource(
            server=server, ctx=ctx, clock=MediaClock(), queue_size=4
        )
        await source.start()
        await source.stop()
        try:
            assert server.endpoint.startswith("ws://127.0.0.1:")
            await server.send(b"still open")
        finally:
            await server.stop()


class TestEgress:
    @pytest.mark.asyncio
    async def test_published_audio_reaches_the_page_framed(self, ctx: FrameContext) -> None:
        # A real socket rather than a patched ``send``: ``PageAudioServer`` uses ``__slots__``,
        # and the broadcast path is worth exercising for real anyway.
        server = PageAudioServer()
        sink = TeamsWebMediaSink(server=server)
        await server.start()
        try:
            from websockets.asyncio.client import connect

            async with connect(server.endpoint) as page:
                await _until(lambda: server.attached_pages == 1)
                await sink.start(
                    MeetingContext(meeting_number="1", display_name="AI Avatar")
                )
                await sink.publish_audio(
                    AudioFrame(pcm=PCM, pts_us=42, format=AVATAR_INPUT_FORMAT, ctx=ctx)
                )
                framed = await asyncio.wait_for(page.recv(), timeout=1)
        finally:
            await server.stop()

        assert isinstance(framed, bytes)
        assert framed[5] == KIND_AUDIO_PCM
        # The inbound decoder must refuse it: it is the other direction's kind.
        assert decode_audio(framed) is None
        assert sink.published == 1

    @pytest.mark.asyncio
    async def test_video_is_counted_rather_than_silently_discarded(
        self, ctx: FrameContext
    ) -> None:
        """An operator sees ``video_dropped`` climbing rather than assuming a camera that was
        never wired."""
        sink = TeamsWebMediaSink(server=PageAudioServer())
        fmt = VideoFormat(width=2, height=2, fps=25)
        await sink.publish_video(
            VideoFrame(planes=b"\x00" * fmt.frame_size_bytes, pts_us=0, format=fmt, ctx=ctx)
        )
        assert sink.video_dropped == 1
        assert sink.published == 0

    def test_no_attached_page_is_unhealthy(self) -> None:
        """The failure worth surfacing: the session keeps hearing the meeting and looking fine
        everywhere else, while nothing it says is audible."""
        sink = TeamsWebMediaSink(server=PageAudioServer())
        health = sink.health()
        assert health.state is ComponentState.UNHEALTHY
        assert health.detail is not None
        assert "not attached" in health.detail

    def test_our_own_participant_id_is_unknown(self) -> None:
        """Structurally so: the browser does not tell us. The Graph connector's sidecar does,
        which is why ``EchoGuard`` can filter by identity there and not here."""
        assert TeamsWebMediaSink(server=PageAudioServer()).own_participant() is None


async def _until(predicate: object, *, timeout_s: float = 1.0) -> None:
    """Poll until ``predicate`` holds, so a test never sleeps for a fixed interval."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while not predicate():  # type: ignore[operator]
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition never became true")
        await asyncio.sleep(0.005)
