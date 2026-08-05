"""FfmpegDecoder — fMP4 → raw I420 video + PCM audio.

One ``ffmpeg`` subprocess per session: fMP4 on stdin, two raw outputs.

**Why a subprocess and not PyAV in-process?** Isolation. A malformed fragment kills a
child process we can restart, rather than raising inside the event loop that is also
running a live meeting. The cost is roughly one frame of latency and one process.
PyAV is documented as the optimisation path (doc 003 §0.1) and the ``MediaDecoder``
port keeps it a config change.

**Why two output pipes rather than one muxed stream?** Because the publisher needs
audio and video separately, on separate Zoom SDK calls. Muxing here only to demux
again would add work and a synchronisation point.

Video goes to stdout; audio to file descriptor 3. Both are raw, so frame boundaries
are computed from geometry rather than parsed — the reader knows exactly how many
bytes make a frame.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import suppress

from src.domain.context import FrameContext
from src.domain.health import ComponentHealth, ComponentState
from src.domain.media import (
    AudioFormat,
    AudioFrame,
    MediaChunk,
    VideoFormat,
    VideoFrame,
)
from src.infrastructure.logging import get_logger
from src.infrastructure.metrics import MetricName, MetricsCollector
from src.services.media.clock import MediaClock

logger = get_logger(__name__)

COMPONENT_NAME = "ffmpeg_decoder"

_AUDIO_FD = 3
"""Audio egress fd. stdout carries video, stderr carries logs, so audio needs its own."""

AUDIO_CHUNK_MS = 20
"""Audio read granularity. Matches the RTMS ingest cadence, so both directions of the
pipeline speak in the same size unit."""


class FfmpegDecoderError(Exception):
    """The decoder could not be started or died unexpectedly."""


class FfmpegDecoder:
    """``MediaDecoder`` backed by an ffmpeg subprocess."""

    __slots__ = (
        "_audio_format",
        "_audio_frames",
        "_audio_reader",
        "_clock",
        "_ctx",
        "_detail",
        "_ffmpeg_path",
        "_metrics",
        "_process",
        "_state",
        "_stderr_task",
        "_video_format",
        "_video_frames",
    )

    def __init__(
        self,
        *,
        ctx: FrameContext,
        clock: MediaClock,
        video_format: VideoFormat,
        audio_format: AudioFormat,
        ffmpeg_path: str = "ffmpeg",
        metrics: MetricsCollector | None = None,
    ) -> None:
        self._ctx = ctx
        self._clock = clock
        self._video_format = video_format
        self._audio_format = audio_format
        self._ffmpeg_path = ffmpeg_path
        self._metrics = metrics

        self._process: asyncio.subprocess.Process | None = None
        self._audio_reader: asyncio.StreamReader | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._state = ComponentState.UNKNOWN
        self._detail: str | None = None
        self._video_frames = 0
        self._audio_frames = 0

    # -- MediaDecoder ------------------------------------------------------

    async def start(self, init_segment: MediaChunk | None = None) -> None:
        """Spawn ffmpeg, replaying ``init_segment`` first if supplied.

        The replay is not optional in practice: an fMP4 decoder cannot resume from a
        mid-stream ``moof``, so a restart without the cached ``ftyp``+``moov`` produces
        permanently black video that *looks* like successful recovery.
        """
        if self._process is not None:
            return

        audio_read_fd, audio_write_fd = os.pipe()
        try:
            process = await asyncio.create_subprocess_exec(
                *self._build_command(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                pass_fds=(audio_write_fd,),
            )
        except (OSError, ValueError) as exc:
            os.close(audio_read_fd)
            os.close(audio_write_fd)
            self._state = ComponentState.UNHEALTHY
            self._detail = f"spawn failed: {exc}"
            raise FfmpegDecoderError(f"cannot start {self._ffmpeg_path}: {exc}") from exc

        # The child owns the write end now; holding it open here would mean the
        # audio reader never sees EOF when ffmpeg exits.
        os.close(audio_write_fd)

        self._process = process
        self._audio_reader = await self._wrap_fd(audio_read_fd)
        self._stderr_task = asyncio.create_task(self._drain_stderr(), name="ffmpeg-stderr")
        self._state = ComponentState.HEALTHY
        self._detail = None

        if init_segment is not None:
            await self.feed(init_segment)

        logger.info(
            "decoder.started",
            video=str(self._video_format),
            audio=str(self._audio_format),
            init_segment_bytes=init_segment.size_bytes if init_segment else 0,
        )

    def _build_command(self) -> list[str]:
        fmt = self._video_format
        return [
            self._ffmpeg_path,
            "-hide_banner",
            "-loglevel", "warning",
            "-fflags", "+nobuffer+discardcorrupt",
            "-flags", "low_delay",
            "-probesize", "32",
            "-analyzeduration", "0",
            "-i", "pipe:0",
            # video → stdout, scaled to the negotiated publish geometry
            "-map", "0:v:0?",
            "-vf", f"scale={fmt.width}:{fmt.height},fps={fmt.fps}",
            "-pix_fmt", "yuv420p",
            "-f", "rawvideo",
            "pipe:1",
            # audio → fd 3, resampled to the publish rate
            "-map", "0:a:0?",
            "-ar", str(self._audio_format.sample_rate_hz),
            "-ac", str(self._audio_format.channels),
            "-f", "s16le",
            f"pipe:{_AUDIO_FD}",
        ]

    @staticmethod
    async def _wrap_fd(fd: int) -> asyncio.StreamReader:
        """Wrap a raw fd as an asyncio reader."""
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), os.fdopen(fd, "rb", 0)
        )
        return reader

    async def feed(self, chunk: MediaChunk) -> None:
        """Write one container chunk to ffmpeg's stdin."""
        process = self._process
        if process is None or process.stdin is None:
            raise FfmpegDecoderError("feed() called before start()")
        try:
            process.stdin.write(chunk.data)
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            self._state = ComponentState.UNHEALTHY
            self._detail = f"stdin closed: {exc}"
            raise FfmpegDecoderError(f"ffmpeg stdin closed: {exc}") from exc

    async def video(self) -> AsyncIterator[VideoFrame]:
        """Yield decoded video frames.

        Frame size is fixed by geometry, so an exact read is a frame — no parsing and
        no partial-frame ambiguity.
        """
        process = self._process
        if process is None or process.stdout is None:
            raise FfmpegDecoderError("video() called before start()")

        frame_bytes = self._video_format.frame_size_bytes
        while True:
            try:
                planes = await process.stdout.readexactly(frame_bytes)
            except asyncio.IncompleteReadError:
                return  # ffmpeg exited; a partial frame is not publishable

            started = self._clock.now_us()
            frame = VideoFrame(
                planes=planes,
                pts_us=self._clock.now_us(),
                format=self._video_format,
                ctx=self._ctx,
                # ffmpeg emits raw frames with no keyframe concept; every raw frame
                # is independently displayable, so this is true by construction.
                is_keyframe=True,
            )
            self._video_frames += 1
            if self._metrics is not None:
                self._metrics.observe(
                    MetricName.DECODE_US,
                    self._clock.now_us() - started,
                    ctx=self._ctx,
                    kind="video",
                )
            yield frame

    async def audio(self) -> AsyncIterator[AudioFrame]:
        """Yield decoded audio frames in ``AUDIO_CHUNK_MS`` slices."""
        reader = self._audio_reader
        if reader is None:
            raise FfmpegDecoderError("audio() called before start()")

        chunk_bytes = self._audio_format.bytes_for_duration(AUDIO_CHUNK_MS * 1_000)
        while True:
            try:
                pcm = await reader.readexactly(chunk_bytes)
            except asyncio.IncompleteReadError as exc:
                # Emit a final short frame if it is a whole number of samples;
                # dropping real audio at end-of-utterance would clip the last word.
                tail = exc.partial
                if tail and not len(tail) % self._audio_format.bytes_per_frame:
                    yield self._audio_frame(tail)
                return

            yield self._audio_frame(pcm)

    def _audio_frame(self, pcm: bytes) -> AudioFrame:
        self._audio_frames += 1
        return AudioFrame(
            pcm=pcm, pts_us=self._clock.now_us(), format=self._audio_format, ctx=self._ctx
        )

    async def stop(self) -> None:
        """Terminate ffmpeg and release resources. Idempotent."""
        stderr_task, self._stderr_task = self._stderr_task, None
        if stderr_task is not None:
            stderr_task.cancel()
            with suppress(asyncio.CancelledError):
                await stderr_task

        process, self._process = self._process, None
        self._audio_reader = None
        if process is None:
            return

        if process.stdin is not None and not process.stdin.is_closing():
            with suppress(BrokenPipeError, ConnectionResetError, RuntimeError):
                process.stdin.close()

        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except TimeoutError:
                process.kill()
                await process.wait()

        self._state = ComponentState.UNKNOWN
        self._detail = "stopped"

    def health(self) -> ComponentHealth:
        process = self._process
        if process is not None and process.returncode is not None:
            return ComponentHealth.unhealthy(
                COMPONENT_NAME, f"ffmpeg exited with code {process.returncode}"
            )
        return ComponentHealth(name=COMPONENT_NAME, state=self._state, detail=self._detail)

    @property
    def stats(self) -> tuple[int, int]:
        """``(video_frames, audio_frames)`` emitted so far."""
        return self._video_frames, self._audio_frames

    async def _drain_stderr(self) -> None:
        """Surface ffmpeg's diagnostics as structured logs.

        Not draining stderr would eventually fill the pipe buffer and block ffmpeg —
        a deadlock that presents as "video mysteriously stopped".
        """
        process = self._process
        if process is None or process.stderr is None:
            return
        async for line in process.stderr:
            text = line.decode("utf-8", errors="replace").strip()
            if text:
                logger.warning("decoder.ffmpeg", message=text)
