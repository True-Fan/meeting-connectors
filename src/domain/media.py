"""Canonical media model.

This is the anti-corruption boundary made concrete. Each connector's wire types — a
page frame header, a bridge envelope, a base64 payload — are translated into these
models inside that connector's own mapping code and never travel further. Every
consumer downstream — router, decoder, pacer, publisher — speaks only this module,
which is what makes them testable without RTMS.

Frames validate their own payload against their declared format on construction.
The checks are O(1) integer arithmetic; at ~75 frames/s per session the cost is
irrelevant next to catching a malformed frame at the boundary that produced it
rather than three hops later as garbled video.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from src.domain.context import FrameContext
from src.domain.exceptions import InvalidFrameError
from src.domain.meeting import ParticipantRef


class SampleFormat(StrEnum):
    """Audio sample encodings the bridge handles."""

    S16LE = "s16le"

    @property
    def bytes_per_sample(self) -> int:
        return 2


class PixelFormat(StrEnum):
    """Video pixel formats the bridge handles.

    I420 is the format the Zoom Meeting SDK's external video source requires
    (doc 001 §2), so it is the pipeline's native currency — no conversion happens
    between the decoder and the publisher.
    """

    I420 = "i420"


class ContainerFormat(StrEnum):
    """Container formats the avatar agent may stream."""

    FRAGMENTED_MP4 = "fmp4"


@dataclass(frozen=True, slots=True)
class AudioFormat:
    """An immutable PCM audio format description."""

    sample_rate_hz: int
    channels: int
    sample_format: SampleFormat = SampleFormat.S16LE

    def __post_init__(self) -> None:
        if self.sample_rate_hz <= 0:
            raise InvalidFrameError(f"sample_rate_hz must be positive, got {self.sample_rate_hz}")
        if self.channels <= 0:
            raise InvalidFrameError(f"channels must be positive, got {self.channels}")

    @property
    def bytes_per_frame(self) -> int:
        """Bytes for one sample across all channels."""
        return self.channels * self.sample_format.bytes_per_sample

    def bytes_for_duration(self, duration_us: int) -> int:
        """Bytes needed to hold ``duration_us`` of audio in this format."""
        return (self.sample_rate_hz * duration_us) // 1_000_000 * self.bytes_per_frame

    def __str__(self) -> str:
        return f"{self.sample_rate_hz}Hz/{self.channels}ch/{self.sample_format}"


@dataclass(frozen=True, slots=True)
class VideoFormat:
    """An immutable raw video format description."""

    width: int
    height: int
    fps: int
    pixel_format: PixelFormat = PixelFormat.I420

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise InvalidFrameError(f"invalid dimensions {self.width}x{self.height}")
        if self.width % 2 or self.height % 2:
            # I420 subsamples chroma 2x2; odd dimensions have no valid plane layout.
            raise InvalidFrameError(
                f"I420 requires even dimensions, got {self.width}x{self.height}"
            )
        if self.fps <= 0:
            raise InvalidFrameError(f"fps must be positive, got {self.fps}")

    @property
    def frame_size_bytes(self) -> int:
        """Size of one packed I420 frame: Y plane plus two half-resolution planes."""
        return self.width * self.height * 3 // 2

    @property
    def frame_duration_us(self) -> int:
        """Nominal presentation interval for one frame."""
        return 1_000_000 // self.fps

    def __str__(self) -> str:
        return f"{self.width}x{self.height}@{self.fps}/{self.pixel_format}"


@dataclass(frozen=True, slots=True)
class AudioFrame:
    """A single PCM audio frame on the session media clock."""

    pcm: bytes
    pts_us: int
    format: AudioFormat
    ctx: FrameContext
    participant: ParticipantRef | None = None

    def __post_init__(self) -> None:
        if self.pts_us < 0:
            raise InvalidFrameError(f"pts_us must be non-negative, got {self.pts_us}")
        if len(self.pcm) % self.format.bytes_per_frame:
            raise InvalidFrameError(
                f"pcm length {len(self.pcm)} is not a whole number of samples for {self.format}"
            )

    @property
    def sample_count(self) -> int:
        """Samples per channel in this frame."""
        return len(self.pcm) // self.format.bytes_per_frame

    @property
    def duration_us(self) -> int:
        """Presentation duration of this frame."""
        return self.sample_count * 1_000_000 // self.format.sample_rate_hz

    @property
    def is_silence(self) -> bool:
        """True when every byte is zero.

        Used by the pacer to account for idle publishing; not an energy-based
        speech detector, and deliberately not one — the bridge runs no VAD.
        """
        return not any(self.pcm)


@dataclass(frozen=True, slots=True)
class VideoFrame:
    """A single raw video frame on the session media clock."""

    planes: bytes
    pts_us: int
    format: VideoFormat
    ctx: FrameContext
    is_keyframe: bool = False

    def __post_init__(self) -> None:
        if self.pts_us < 0:
            raise InvalidFrameError(f"pts_us must be non-negative, got {self.pts_us}")
        expected = self.format.frame_size_bytes
        if len(self.planes) != expected:
            raise InvalidFrameError(
                f"planes length {len(self.planes)} != {expected} expected for {self.format}"
            )


@dataclass(frozen=True, slots=True)
class MediaChunk:
    """An opaque container-format chunk streamed from the avatar agent.

    ``is_init_segment`` marks the ``ftyp``+``moov`` prologue. It is a first-class
    field because a fragmented-MP4 decoder cannot resume from a mid-stream
    ``moof``: on decoder restart the cached init segment must be replayed first,
    or recovery silently produces permanent black video (doc 003 §0.2).
    """

    data: bytes
    seq: int
    received_at_us: int
    ctx: FrameContext
    is_init_segment: bool = False

    def __post_init__(self) -> None:
        if self.seq < 0:
            raise InvalidFrameError(f"seq must be non-negative, got {self.seq}")

    @property
    def size_bytes(self) -> int:
        return len(self.data)
