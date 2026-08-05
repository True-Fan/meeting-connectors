"""Decoder double.

``FakeDecoder`` is the second implementation that justifies the ``MediaDecoder`` port
(doc 003 §0): deterministic frames, no ffmpeg binary, and it records how many times it
was started so init-segment replay on restart can be asserted.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from src.domain.context import FrameContext
from src.domain.health import ComponentHealth, ComponentState
from src.domain.media import (
    AudioFormat,
    AudioFrame,
    MediaChunk,
    VideoFormat,
    VideoFrame,
)


class FakeDecoder:
    """Emits a fixed number of frames per chunk fed."""

    def __init__(
        self,
        *,
        ctx: FrameContext,
        video_format: VideoFormat,
        audio_format: AudioFormat,
        video_per_chunk: int = 2,
        audio_per_chunk: int = 2,
        audio_chunk_ms: int = 20,
        fail_on_feed: Exception | None = None,
    ) -> None:
        self._ctx = ctx
        self._video_format = video_format
        self._audio_format = audio_format
        self._video_per_chunk = video_per_chunk
        self._audio_per_chunk = audio_per_chunk
        self._audio_bytes = audio_format.bytes_for_duration(audio_chunk_ms * 1_000)
        self._fail_on_feed = fail_on_feed

        self.start_calls: list[MediaChunk | None] = []
        self.fed: list[MediaChunk] = []
        self.stopped = 0

        self._video: asyncio.Queue[VideoFrame | None] = asyncio.Queue()
        self._audio: asyncio.Queue[AudioFrame | None] = asyncio.Queue()
        self._pts = 0
        self._state = ComponentState.UNKNOWN

    @property
    def init_segments_replayed(self) -> int:
        """How many starts were given an init segment.

        The assertion that matters for restart: fMP4 cannot resume from a mid-stream
        ``moof``, so a restart without one produces permanently black video.
        """
        return sum(1 for c in self.start_calls if c is not None and c.is_init_segment)

    async def start(self, init_segment: MediaChunk | None = None) -> None:
        self.start_calls.append(init_segment)
        self._state = ComponentState.HEALTHY

    async def stop(self) -> None:
        self.stopped += 1
        self._state = ComponentState.UNKNOWN
        await self._video.put(None)
        await self._audio.put(None)

    async def feed(self, chunk: MediaChunk) -> None:
        if self._fail_on_feed is not None:
            raise self._fail_on_feed
        self.fed.append(chunk)
        if chunk.is_init_segment:
            return  # an init segment carries no samples

        for _ in range(self._video_per_chunk):
            self._video.put_nowait(
                VideoFrame(
                    planes=b"\x10" * self._video_format.frame_size_bytes,
                    pts_us=self._pts,
                    format=self._video_format,
                    ctx=self._ctx,
                    is_keyframe=True,
                )
            )
        for _ in range(self._audio_per_chunk):
            self._audio.put_nowait(
                AudioFrame(
                    pcm=b"\x01\x02" * (self._audio_bytes // 2),
                    pts_us=self._pts,
                    format=self._audio_format,
                    ctx=self._ctx,
                )
            )
        self._pts += 20_000

    async def video(self) -> AsyncIterator[VideoFrame]:
        while True:
            frame = await self._video.get()
            if frame is None:
                return
            yield frame

    async def audio(self) -> AsyncIterator[AudioFrame]:
        while True:
            frame = await self._audio.get()
            if frame is None:
                return
            yield frame

    def health(self) -> ComponentHealth:
        return ComponentHealth(name="fake_decoder", state=self._state)
