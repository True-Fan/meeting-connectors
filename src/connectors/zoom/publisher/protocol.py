"""Sidecar IPC codec — wire version 1. **FROZEN.**

Reference implementation of ``docs/design/004-sidecar-ipc-protocol.md``. The C++
sidecar (M5) must match this byte for byte; ``tests/unit/test_sidecar_protocol.py``
holds the conformance vector.

This module is pure: no I/O, no sockets, no asyncio. Encoding is a function of its
arguments and decoding is an incremental state machine over a byte stream. That
makes the boundary exhaustively testable without a sidecar process — which is the
point of freezing it before the publisher exists.

Do not change the layout. Additive changes only, per the change policy in §1 of the
spec; anything else needs a new ``WIRE_VERSION`` and a new document.
"""

from __future__ import annotations

import json
import struct
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import IntEnum, IntFlag
from typing import Any, Final

from src.domain.media import AudioFrame, PixelFormat, SampleFormat, VideoFrame

# --------------------------------------------------------------------------- #
# Frozen constants
# --------------------------------------------------------------------------- #

MAGIC: Final[int] = 0x5A4D4331
"""ASCII 'ZMC1'."""

WIRE_VERSION: Final[int] = 1

HEADER_STRUCT: Final = struct.Struct(">IBBBBIqI")
HEADER_SIZE: Final[int] = HEADER_STRUCT.size  # 24

VIDEO_HEADER_STRUCT: Final = struct.Struct(">HHHHHH")
VIDEO_HEADER_SIZE: Final[int] = VIDEO_HEADER_STRUCT.size  # 12

AUDIO_HEADER_STRUCT: Final = struct.Struct(">IBBH")
AUDIO_HEADER_SIZE: Final[int] = AUDIO_HEADER_STRUCT.size  # 8

MAX_PAYLOAD_BYTES: Final[int] = 8 * 1024 * 1024

_SAMPLE_FORMAT_S16LE: Final[int] = 1
_SAMPLE_FORMAT_TO_WIRE: Final[dict[SampleFormat, int]] = {
    SampleFormat.S16LE: _SAMPLE_FORMAT_S16LE,
}
_WIRE_TO_SAMPLE_FORMAT: Final[dict[int, SampleFormat]] = {
    _SAMPLE_FORMAT_S16LE: SampleFormat.S16LE,
}


class SidecarMessageType(IntEnum):
    """Message discriminator. Values are frozen (spec §3)."""

    VIDEO_I420 = 0x01
    AUDIO_PCM = 0x02
    CONTROL_JOIN = 0x03
    CONTROL_LEAVE = 0x04
    HEARTBEAT = 0x05
    READY = 0x06
    ERROR = 0x07

    @property
    def is_media(self) -> bool:
        return self in (SidecarMessageType.VIDEO_I420, SidecarMessageType.AUDIO_PCM)

    @property
    def is_json(self) -> bool:
        return not self.is_media


class SidecarFlags(IntFlag):
    """Advisory per-message flags (spec §4)."""

    NONE = 0x00
    KEYFRAME = 0x01
    IDLE = 0x02
    END_OF_STREAM = 0x04


class SidecarProtocolError(Exception):
    """The byte stream violates the wire contract.

    Always fatal for the connection. A desynced binary stream cannot be re-aligned
    with confidence, and a heuristic resync would publish garbage while reporting
    success (spec §6).
    """


# --------------------------------------------------------------------------- #
# Messages
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SidecarMessage:
    """A decoded IPC message: header fields plus its raw payload."""

    msg_type: SidecarMessageType
    payload: bytes = b""
    seq: int = 0
    pts_us: int = 0
    flags: SidecarFlags = SidecarFlags.NONE

    def json(self) -> dict[str, Any]:
        """Parse the payload as a JSON object.

        Raises:
            SidecarProtocolError: on a media message, malformed JSON, or a
                non-object top level.
        """
        if self.msg_type.is_media:
            raise SidecarProtocolError(f"{self.msg_type.name} payload is binary, not JSON")
        if not self.payload:
            return {}
        try:
            parsed = json.loads(self.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SidecarProtocolError(f"{self.msg_type.name} payload is not valid JSON") from exc
        if not isinstance(parsed, dict):
            raise SidecarProtocolError(
                f"{self.msg_type.name} payload must be a JSON object, got {type(parsed).__name__}"
            )
        return parsed


@dataclass(frozen=True, slots=True)
class VideoPayload:
    """Decoded ``VIDEO_I420`` payload (spec §5.1)."""

    width: int
    height: int
    planes: bytes
    stride_y: int = 0
    stride_u: int = 0
    stride_v: int = 0

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise SidecarProtocolError(f"invalid video geometry {self.width}x{self.height}")

    @property
    def is_packed(self) -> bool:
        """True when strides imply a tightly packed I420 buffer."""
        return (
            self.stride_y == self.width
            and self.stride_u == self.width // 2
            and self.stride_v == self.width // 2
        )


@dataclass(frozen=True, slots=True)
class AudioPayload:
    """Decoded ``AUDIO_PCM`` payload (spec §5.2)."""

    sample_rate_hz: int
    channels: int
    pcm: bytes
    sample_format: SampleFormat = SampleFormat.S16LE


# --------------------------------------------------------------------------- #
# Encoding
# --------------------------------------------------------------------------- #


def encode(message: SidecarMessage) -> bytes:
    """Serialise one message to its wire representation."""
    if len(message.payload) > MAX_PAYLOAD_BYTES:
        raise SidecarProtocolError(
            f"payload {len(message.payload)} exceeds {MAX_PAYLOAD_BYTES} byte cap"
        )
    if not 0 <= message.seq <= 0xFFFFFFFF:
        raise SidecarProtocolError(f"seq {message.seq} out of u32 range")

    header = HEADER_STRUCT.pack(
        MAGIC,
        WIRE_VERSION,
        int(message.msg_type),
        int(message.flags) & 0xFF,
        0,  # reserved
        message.seq,
        message.pts_us,
        len(message.payload),
    )
    return header + message.payload


def encode_video(
    frame: VideoFrame,
    *,
    seq: int,
    idle: bool = False,
) -> bytes:
    """Encode a domain ``VideoFrame`` as a ``VIDEO_I420`` message.

    Geometry travels with every frame, so a mid-session resolution change cannot
    desync the sidecar (spec §5.1).
    """
    if frame.format.pixel_format is not PixelFormat.I420:
        raise SidecarProtocolError(f"wire v1 carries I420 only, got {frame.format.pixel_format}")

    width = frame.format.width
    height = frame.format.height
    sub = VIDEO_HEADER_STRUCT.pack(width, height, width, width // 2, width // 2, 0)

    flags = SidecarFlags.NONE
    if frame.is_keyframe:
        flags |= SidecarFlags.KEYFRAME
    if idle:
        flags |= SidecarFlags.IDLE

    return encode(
        SidecarMessage(
            msg_type=SidecarMessageType.VIDEO_I420,
            payload=sub + frame.planes,
            seq=seq,
            pts_us=frame.pts_us,
            flags=flags,
        )
    )


def encode_audio(
    frame: AudioFrame,
    *,
    seq: int,
    idle: bool = False,
) -> bytes:
    """Encode a domain ``AudioFrame`` as an ``AUDIO_PCM`` message."""
    wire_format = _SAMPLE_FORMAT_TO_WIRE.get(frame.format.sample_format)
    if wire_format is None:
        raise SidecarProtocolError(f"unsupported sample format {frame.format.sample_format}")

    sub = AUDIO_HEADER_STRUCT.pack(
        frame.format.sample_rate_hz, frame.format.channels, wire_format, 0
    )
    return encode(
        SidecarMessage(
            msg_type=SidecarMessageType.AUDIO_PCM,
            payload=sub + frame.pcm,
            seq=seq,
            pts_us=frame.pts_us,
            flags=SidecarFlags.IDLE if idle else SidecarFlags.NONE,
        )
    )


def encode_json(
    msg_type: SidecarMessageType,
    body: dict[str, Any],
    *,
    seq: int = 0,
    pts_us: int = 0,
) -> bytes:
    """Encode a control message with a JSON body."""
    if msg_type.is_media:
        raise SidecarProtocolError(f"{msg_type.name} is a media type, not a control type")
    payload = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return encode(SidecarMessage(msg_type=msg_type, payload=payload, seq=seq, pts_us=pts_us))


# --------------------------------------------------------------------------- #
# Payload decoding
# --------------------------------------------------------------------------- #


def decode_video_payload(payload: bytes) -> VideoPayload:
    """Parse a ``VIDEO_I420`` payload."""
    if len(payload) < VIDEO_HEADER_SIZE:
        raise SidecarProtocolError(
            f"video payload {len(payload)} shorter than {VIDEO_HEADER_SIZE} byte sub-header"
        )
    width, height, stride_y, stride_u, stride_v, _ = VIDEO_HEADER_STRUCT.unpack_from(payload)
    planes = payload[VIDEO_HEADER_SIZE:]
    expected = width * height * 3 // 2
    if len(planes) != expected:
        raise SidecarProtocolError(
            f"video planes {len(planes)} != {expected} expected for {width}x{height} I420"
        )
    return VideoPayload(
        width=width,
        height=height,
        planes=planes,
        stride_y=stride_y,
        stride_u=stride_u,
        stride_v=stride_v,
    )


def decode_audio_payload(payload: bytes) -> AudioPayload:
    """Parse an ``AUDIO_PCM`` payload."""
    if len(payload) < AUDIO_HEADER_SIZE:
        raise SidecarProtocolError(
            f"audio payload {len(payload)} shorter than {AUDIO_HEADER_SIZE} byte sub-header"
        )
    sample_rate, channels, wire_format, _ = AUDIO_HEADER_STRUCT.unpack_from(payload)
    sample_format = _WIRE_TO_SAMPLE_FORMAT.get(wire_format)
    if sample_format is None:
        raise SidecarProtocolError(f"unknown sample format code {wire_format}")
    if channels == 0:
        raise SidecarProtocolError("channels must be non-zero")
    return AudioPayload(
        sample_rate_hz=sample_rate,
        channels=channels,
        pcm=payload[AUDIO_HEADER_SIZE:],
        sample_format=sample_format,
    )


# --------------------------------------------------------------------------- #
# Incremental stream decoding
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class SidecarFrameDecoder:
    """Incremental decoder for a ``SOCK_STREAM`` byte stream.

    A stream socket delivers arbitrary fragments, so ``feed`` accepts any number of
    bytes and yields whole messages as they complete. Partial headers and partial
    payloads are retained across calls.

    Usage::

        decoder = SidecarFrameDecoder()
        while chunk := await reader.read(65536):
            for message in decoder.feed(chunk):
                handle(message)
    """

    _buffer: bytearray = field(default_factory=bytearray)

    def feed(self, data: bytes) -> Iterator[SidecarMessage]:
        """Consume bytes and yield every message that is now complete.

        Raises:
            SidecarProtocolError: bad magic, unsupported version, unknown message
                type, or an over-cap length. Always fatal (spec §6).
        """
        self._buffer.extend(data)
        while True:
            if len(self._buffer) < HEADER_SIZE:
                return

            magic, version, raw_type, raw_flags, _reserved, seq, pts_us, length = (
                HEADER_STRUCT.unpack_from(self._buffer)
            )

            if magic != MAGIC:
                raise SidecarProtocolError(
                    f"framing desync: expected magic 0x{MAGIC:08X}, got 0x{magic:08X}"
                )
            if version != WIRE_VERSION:
                raise SidecarProtocolError(
                    f"unsupported wire version {version}, this build speaks {WIRE_VERSION}"
                )
            if length > MAX_PAYLOAD_BYTES:
                raise SidecarProtocolError(
                    f"declared payload {length} exceeds {MAX_PAYLOAD_BYTES} byte cap"
                )
            try:
                msg_type = SidecarMessageType(raw_type)
            except ValueError as exc:
                raise SidecarProtocolError(f"unknown message type 0x{raw_type:02X}") from exc

            total = HEADER_SIZE + length
            if len(self._buffer) < total:
                return  # payload still in flight

            payload = bytes(self._buffer[HEADER_SIZE:total])
            del self._buffer[:total]

            yield SidecarMessage(
                msg_type=msg_type,
                payload=payload,
                seq=seq,
                pts_us=pts_us,
                # Unknown flag bits are preserved rather than rejected, so a newer
                # peer setting a reserved bit stays interoperable (spec §4).
                flags=SidecarFlags(raw_flags),
            )

    @property
    def pending_bytes(self) -> int:
        """Buffered bytes not yet forming a complete message."""
        return len(self._buffer)

    def reset(self) -> None:
        """Discard buffered bytes. Call after a reconnect."""
        self._buffer.clear()
