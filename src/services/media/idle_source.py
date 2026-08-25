"""IdleFrameSource — what the avatar shows when it is not speaking.

**A gap in docs 001 and 002, surfaced in doc 003 §1.4.** Zoom's external video source
must be fed continuously at the negotiated frame rate. If frames only flow while the
avatar speaks, then between utterances the camera freezes on the last frame or drops
out — which reads as a broken connection, not a person. The end goal is "looks like
another human", so the publisher never stops sending.

Video strategy, in order of preference:

1. a looping idle clip of pre-decoded I420 frames (configurable);
2. hold the last real frame the avatar produced;
3. a neutral grey field, so a session that has not yet spoken still shows something
   rather than black.

Audio while idle is digital silence at the same cadence as real audio, so the virtual
microphone sees a continuous stream and the publish clock never stalls.
"""

from __future__ import annotations

from pathlib import Path

from src.domain.context import FrameContext
from src.domain.media import AudioFormat, AudioFrame, VideoFormat, VideoFrame
from src.infrastructure.logging import get_logger

logger = get_logger(__name__)

_GREY_LUMA = 0x80
_NEUTRAL_CHROMA = 0x80


def make_solid_i420(video_format: VideoFormat, *, luma: int = _GREY_LUMA) -> bytes:
    """Build a single flat I420 frame.

    Y plane at ``luma``, chroma planes at neutral 0x80 (no colour cast).
    """
    width, height = video_format.width, video_format.height
    y_size = width * height
    chroma_size = y_size // 4
    return bytes([luma]) * y_size + bytes([_NEUTRAL_CHROMA]) * (chroma_size * 2)


class IdleFrameSource:
    """Supplies video and audio for the gaps between utterances."""

    __slots__ = (
        "_audio_format",
        "_clip",
        "_clip_index",
        "_ctx",
        "_fallback",
        "_last_real_frame",
        "_silence",
        "_video_format",
    )

    def __init__(
        self,
        *,
        ctx: FrameContext,
        video_format: VideoFormat,
        audio_format: AudioFormat,
        audio_chunk_ms: int = 20,
        clip: list[bytes] | None = None,
    ) -> None:
        self._ctx = ctx
        self._video_format = video_format
        self._audio_format = audio_format
        self._clip = clip or []
        self._clip_index = 0
        self._last_real_frame: bytes | None = None
        self._fallback = make_solid_i420(video_format)
        self._silence = bytes(audio_format.bytes_for_duration(audio_chunk_ms * 1_000))

        if self._clip:
            expected = video_format.frame_size_bytes
            bad = [i for i, f in enumerate(self._clip) if len(f) != expected]
            if bad:
                raise ValueError(
                    f"idle clip frames {bad[:5]} do not match {video_format} "
                    f"({expected} bytes per frame)"
                )

    @property
    def has_clip(self) -> bool:
        return bool(self._clip)

    def note_real_frame(self, frame: VideoFrame) -> None:
        """Remember the last frame the avatar actually produced.

        Held only when there is no idle clip — a clip is a deliberate choice and
        should not be overridden by whatever the avatar happened to end on.
        """
        if not self._clip and frame.format == self._video_format:
            self._last_real_frame = frame.planes

    def next_video(self, pts_us: int) -> VideoFrame:
        """The next idle video frame."""
        if self._clip:
            planes = self._clip[self._clip_index]
            self._clip_index = (self._clip_index + 1) % len(self._clip)
        else:
            planes = self._last_real_frame or self._fallback

        return VideoFrame(
            planes=planes,
            pts_us=pts_us,
            format=self._video_format,
            ctx=self._ctx,
            is_keyframe=True,
        )

    def next_audio(self, pts_us: int) -> AudioFrame:
        """A frame of digital silence."""
        return AudioFrame(
            pcm=self._silence, pts_us=pts_us, format=self._audio_format, ctx=self._ctx
        )

    @classmethod
    def from_raw_clip(
        cls,
        path: Path,
        *,
        ctx: FrameContext,
        video_format: VideoFormat,
        audio_format: AudioFormat,
        audio_chunk_ms: int = 20,
    ) -> IdleFrameSource:
        """Load an idle clip from a packed raw I420 file.

        Produce one with::

            ffmpeg -i idle.mp4 -vf scale=W:H,fps=F -pix_fmt yuv420p -f rawvideo idle.i420

        A malformed clip degrades to the built-in fallback rather than failing the
        session: an idle animation is cosmetic, and losing it must not stop the avatar
        from joining.
        """
        frame_bytes = video_format.frame_size_bytes
        frames: list[bytes] = []
        try:
            data = path.read_bytes()
        except OSError as exc:
            logger.warning("idle_source.clip_unreadable", path=str(path), error=str(exc))
            data = b""

        if data and len(data) % frame_bytes:
            logger.warning(
                "idle_source.clip_size_mismatch",
                path=str(path),
                bytes=len(data),
                frame_bytes=frame_bytes,
                note="ignoring clip; falling back to held/neutral frame",
            )
            data = b""

        for offset in range(0, len(data), frame_bytes):
            frames.append(data[offset : offset + frame_bytes])

        if frames:
            logger.info("idle_source.clip_loaded", path=str(path), frames=len(frames))

        return cls(
            ctx=ctx,
            video_format=video_format,
            audio_format=audio_format,
            audio_chunk_ms=audio_chunk_ms,
            clip=frames,
        )
