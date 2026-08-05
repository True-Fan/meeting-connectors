"""Pacer — releases frames on the shared media clock, and never stops.

Two responsibilities that must live together because they share one timeline:

**1. Pacing.** Frames are released when their PTS arrives, not when they finish
decoding. Zoom's send-audio and send-video are separate paths with documented desync
risk (doc 001 §7.1); publishing "as decoded" guarantees drift because decode
completion time is unrelated to presentation time. One clock, two streams.

**2. Idle continuity.** The video loop runs for the session's whole life. When the
avatar has nothing to say, idle frames and silence go out instead — otherwise the
camera freezes and the avatar stops looking like a person (doc 003 §1.4).

Late frames are **dropped, not burst**. Bursting is what turns a brief hiccup into
visible desync that never recovers, because every subsequent frame inherits the
backlog. Audio gaps are filled with silence rather than by shifting later audio
earlier, which would corrupt the timeline permanently.
"""

from __future__ import annotations

import asyncio

from src.domain.context import FrameContext
from src.domain.media import AudioFormat, AudioFrame, VideoFormat, VideoFrame
from src.infrastructure.logging import get_logger
from src.infrastructure.metrics import MetricName, MetricsCollector
from src.protocols.sink import MediaSink
from src.services.media.clock import MediaClock
from src.services.media.echo_guard import EchoGuard
from src.services.media.idle_source import IdleFrameSource
from src.services.media.queues import BoundedFrameQueue, OverflowPolicy

logger = get_logger(__name__)

VIDEO_LATE_TOLERANCE_US = 40_000
"""One frame period at 25 fps. Beyond this a frame is stale and dropped."""

AUDIO_LATE_TOLERANCE_US = 100_000
"""Audio is more tolerant than video: a gap is audible, a slightly late frame is not."""


class Pacer:
    """Paces decoded and idle media into a ``MediaSink``."""

    __slots__ = (
        "_audio_chunk_us",
        "_audio_format",
        "_audio_queue",
        "_clock",
        "_ctx",
        "_echo_guard",
        "_idle",
        "_idle_audio",
        "_idle_video",
        "_metrics",
        "_published_audio",
        "_published_video",
        "_sink",
        "_speaking",
        "_video_format",
        "_video_queue",
    )

    def __init__(
        self,
        *,
        ctx: FrameContext,
        clock: MediaClock,
        sink: MediaSink,
        idle: IdleFrameSource,
        video_format: VideoFormat,
        audio_format: AudioFormat,
        echo_guard: EchoGuard | None = None,
        video_queue_size: int = 3,
        audio_queue_size: int = 10,
        audio_chunk_ms: int = 20,
        metrics: MetricsCollector | None = None,
    ) -> None:
        self._ctx = ctx
        self._clock = clock
        self._sink = sink
        self._idle = idle
        self._video_format = video_format
        self._audio_format = audio_format
        self._echo_guard = echo_guard
        self._metrics = metrics
        self._audio_chunk_us = audio_chunk_ms * 1_000

        self._video_queue: BoundedFrameQueue[VideoFrame] = BoundedFrameQueue(
            name="pacer_video",
            maxsize=video_queue_size,
            policy=OverflowPolicy.DROP_OLDEST,
            metrics=metrics,
        )
        self._audio_queue: BoundedFrameQueue[AudioFrame] = BoundedFrameQueue(
            name="pacer_audio",
            maxsize=audio_queue_size,
            policy=OverflowPolicy.DROP_OLDEST,
            metrics=metrics,
        )
        self._speaking = False
        self._published_video = 0
        self._published_audio = 0
        self._idle_video = 0
        self._idle_audio = 0

    # -- submission --------------------------------------------------------

    def submit_video(self, frame: VideoFrame) -> None:
        """Offer a decoded video frame. Never blocks."""
        self._video_queue.put(frame, ctx=frame.ctx, reason="pacer_video_overflow")

    def submit_audio(self, frame: AudioFrame) -> None:
        """Offer a decoded audio frame. Never blocks."""
        self._audio_queue.put(frame, ctx=frame.ctx, reason="pacer_audio_overflow")

    # -- stats -------------------------------------------------------------

    @property
    def stats(self) -> dict[str, int]:
        return {
            "published_video": self._published_video,
            "published_audio": self._published_audio,
            "idle_video": self._idle_video,
            "idle_audio": self._idle_audio,
            "dropped_video": self._video_queue.dropped,
            "dropped_audio": self._audio_queue.dropped,
        }

    @property
    def is_speaking(self) -> bool:
        """True when real avatar media was published recently."""
        return self._speaking

    # -- run ---------------------------------------------------------------

    async def run(self) -> None:
        """Pace video and audio until cancelled.

        Both loops run for the session's whole life; neither exits when the avatar
        falls silent.
        """
        async with asyncio.TaskGroup() as group:
            group.create_task(self._run_video(), name="pacer-video")
            group.create_task(self._run_audio(), name="pacer-audio")

    async def _run_video(self) -> None:
        period_us = self._video_format.frame_duration_us
        next_pts = self._clock.now_us()

        while True:
            delay = self._clock.deadline_delay_s(next_pts)
            if delay > 0:
                await asyncio.sleep(delay)

            frame = self._take_video()
            if frame is None:
                frame = self._idle.next_video(next_pts)
                self._idle_video += 1
                is_idle = True
            else:
                self._idle.note_real_frame(frame)
                is_idle = False

            await self._publish_video(frame, is_idle=is_idle)
            next_pts += period_us

            # If we have fallen more than a frame behind, resynchronise to now
            # rather than trying to catch up — catching up is bursting.
            if self._clock.is_late(next_pts, tolerance_us=period_us):
                next_pts = self._clock.now_us()

    async def _run_audio(self) -> None:
        next_pts = self._clock.now_us()

        while True:
            delay = self._clock.deadline_delay_s(next_pts)
            if delay > 0:
                await asyncio.sleep(delay)

            frame = self._take_audio()
            if frame is None:
                # Fill with silence rather than shifting later audio earlier, which
                # would corrupt the timeline for the rest of the session.
                frame = self._idle.next_audio(next_pts)
                self._idle_audio += 1
                is_idle = True
            else:
                is_idle = False

            await self._publish_audio(frame, is_idle=is_idle)
            next_pts += self._audio_chunk_us

            if self._clock.is_late(next_pts, tolerance_us=AUDIO_LATE_TOLERANCE_US):
                next_pts = self._clock.now_us()

    # -- queue draining ----------------------------------------------------

    def _take_video(self) -> VideoFrame | None:
        """Take the freshest video frame that is not already stale."""
        while (frame := self._video_queue.get_nowait()) is not None:
            if self._clock.is_late(frame.pts_us, tolerance_us=VIDEO_LATE_TOLERANCE_US):
                self._count_drop("video", "stale")
                continue
            return frame
        return None

    def _take_audio(self) -> AudioFrame | None:
        while (frame := self._audio_queue.get_nowait()) is not None:
            if self._clock.is_late(frame.pts_us, tolerance_us=AUDIO_LATE_TOLERANCE_US):
                self._count_drop("audio", "stale")
                continue
            return frame
        return None

    def _count_drop(self, kind: str, reason: str) -> None:
        if self._metrics is not None:
            self._metrics.increment(
                MetricName.FRAMES_DROPPED_TOTAL, ctx=self._ctx, stage=f"pacer_{kind}", reason=reason
            )

    # -- publishing --------------------------------------------------------

    async def _publish_video(self, frame: VideoFrame, *, is_idle: bool) -> None:
        started = self._clock.now_us()
        await self._sink.publish_video(frame)
        self._published_video += 1
        self._record_publish(frame.pts_us, started, kind="video", is_idle=is_idle)

    async def _publish_audio(self, frame: AudioFrame, *, is_idle: bool) -> None:
        started = self._clock.now_us()
        await self._sink.publish_audio(frame)
        self._published_audio += 1
        self._speaking = not is_idle

        # Close the echo loop: publishing real avatar audio arms the gate so the
        # mixed-back copy arriving through RTMS is suppressed (doc 003 §3.3).
        if not is_idle and self._echo_guard is not None:
            self._echo_guard.note_publishing(self._clock.now_us())

        self._record_publish(frame.pts_us, started, kind="audio", is_idle=is_idle)

    def _record_publish(self, pts_us: int, started_us: int, *, kind: str, is_idle: bool) -> None:
        if self._metrics is None:
            return
        now = self._clock.now_us()
        self._metrics.observe(MetricName.PUBLISH_US, now - started_us, ctx=self._ctx, kind=kind)
        self._metrics.observe(
            MetricName.PACE_WAIT_US, max(started_us - pts_us, 0), ctx=self._ctx, kind=kind
        )
        self._metrics.increment(
            MetricName.IDLE_FRAMES_PUBLISHED_TOTAL
            if is_idle
            else MetricName.FRAMES_PUBLISHED_TOTAL,
            ctx=self._ctx,
            kind=kind,
        )

    def close(self) -> None:
        """Close both queues."""
        self._video_queue.close()
        self._audio_queue.close()
