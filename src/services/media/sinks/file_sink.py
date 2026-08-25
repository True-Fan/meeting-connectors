"""FileSink — writes the avatar's paced output to a playable file.

**This is why the ``MediaSink`` port exists at all.** It lets M4 prove that decoding,
the shared media clock, A/V sync, and idle continuity are all correct — with output a
human can watch — before any Zoom SDK build, C++ toolchain, or account entitlement is
involved. Four of six milestones become verifiable on a laptop.

Muxing is done by an ffmpeg subprocess taking two raw inputs, because the point is a
file you can double-click. When ffmpeg is unavailable it degrades to writing the raw
I420 and PCM streams side by side, which is still inspectable and keeps the sink usable
in a bare CI container.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from src.domain.health import ComponentHealth, ComponentState
from src.domain.media import AudioFormat, AudioFrame, VideoFormat, VideoFrame
from src.domain.meeting import MeetingContext, ParticipantRef
from src.infrastructure.logging import get_logger

logger = get_logger(__name__)

COMPONENT_NAME = "file_sink"


class FileSink:
    """``MediaSink`` that writes to disk."""

    __slots__ = (
        "_audio_format",
        "_audio_frames",
        "_audio_path",
        "_audio_stream",
        "_detail",
        "_ffmpeg_path",
        "_mux",
        "_output_path",
        "_process",
        "_state",
        "_video_format",
        "_video_frames",
        "_video_path",
        "_video_stream",
    )

    def __init__(
        self,
        *,
        output_path: Path,
        video_format: VideoFormat,
        audio_format: AudioFormat,
        mux: bool = True,
        ffmpeg_path: str = "ffmpeg",
    ) -> None:
        self._output_path = output_path
        self._video_format = video_format
        self._audio_format = audio_format
        self._mux = mux
        self._ffmpeg_path = ffmpeg_path

        self._video_path = output_path.with_suffix(".i420")
        self._audio_path = output_path.with_suffix(".pcm")
        self._video_stream: object | None = None
        self._audio_stream: object | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._state = ComponentState.UNKNOWN
        self._detail: str | None = None
        self._video_frames = 0
        self._audio_frames = 0

    @property
    def output_path(self) -> Path:
        return self._output_path

    @property
    def stats(self) -> tuple[int, int]:
        return self._video_frames, self._audio_frames

    # -- MediaSink ---------------------------------------------------------

    async def start(self, meeting: MeetingContext) -> None:
        """Open the output files."""
        self._output_path.parent.mkdir(parents=True, exist_ok=True)
        self._video_stream = self._video_path.open("wb")
        self._audio_stream = self._audio_path.open("wb")
        self._state = ComponentState.HEALTHY
        self._detail = None
        logger.info(
            "file_sink.started",
            output=str(self._output_path),
            meeting_number=meeting.meeting_number,
            video=str(self._video_format),
            audio=str(self._audio_format),
        )

    async def publish_video(self, frame: VideoFrame) -> None:
        if self._video_stream is None:
            raise RuntimeError("publish_video() before start()")
        self._video_stream.write(frame.planes)  # type: ignore[attr-defined]
        self._video_frames += 1

    async def publish_audio(self, frame: AudioFrame) -> None:
        if self._audio_stream is None:
            raise RuntimeError("publish_audio() before start()")
        self._audio_stream.write(frame.pcm)  # type: ignore[attr-defined]
        self._audio_frames += 1

    async def stop(self) -> None:
        """Close the files and mux them. Idempotent."""
        for stream_attr in ("_video_stream", "_audio_stream"):
            stream = getattr(self, stream_attr)
            if stream is not None:
                stream.close()
                setattr(self, stream_attr, None)

        if self._mux and self._video_frames:
            await self._run_mux()

        self._state = ComponentState.UNKNOWN
        self._detail = "stopped"
        logger.info(
            "file_sink.stopped",
            video_frames=self._video_frames,
            audio_frames=self._audio_frames,
            output=str(self._output_path),
        )

    def health(self) -> ComponentHealth:
        return ComponentHealth(name=COMPONENT_NAME, state=self._state, detail=self._detail)

    def own_participant(self) -> ParticipantRef | None:
        """A file has no meeting identity, so echo suppression is moot here."""
        return None

    # -- muxing ------------------------------------------------------------

    async def _run_mux(self) -> None:
        fmt = self._video_format
        command = [
            self._ffmpeg_path,
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            "-f", "rawvideo",
            "-pix_fmt", "yuv420p",
            "-s", f"{fmt.width}x{fmt.height}",
            "-r", str(fmt.fps),
            "-i", str(self._video_path),
            "-f", "s16le",
            "-ar", str(self._audio_format.sample_rate_hz),
            "-ac", str(self._audio_format.channels),
            "-i", str(self._audio_path),
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-shortest",
            str(self._output_path),
        ]
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except (OSError, ValueError) as exc:
            # Raw streams are still on disk and inspectable, so this is a degraded
            # outcome rather than a failure.
            logger.warning(
                "file_sink.mux_unavailable",
                error=str(exc),
                note=f"raw streams kept at {self._video_path} and {self._audio_path}",
            )
            self._state = ComponentState.DEGRADED
            self._detail = "mux unavailable; raw streams retained"
            return

        _, stderr = await process.communicate()
        if process.returncode:
            logger.warning(
                "file_sink.mux_failed",
                returncode=process.returncode,
                error=stderr.decode("utf-8", errors="replace")[:500],
            )
            self._state = ComponentState.DEGRADED
            self._detail = "mux failed; raw streams retained"
            return

        # Only discard the raw streams once a playable file definitely exists.
        self._video_path.unlink(missing_ok=True)
        self._audio_path.unlink(missing_ok=True)
        logger.info("file_sink.muxed", output=str(self._output_path))
