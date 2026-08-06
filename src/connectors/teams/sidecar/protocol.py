"""Teams sidecar IPC codec — wire version 1.

Reference implementation of ``docs/design/006-teams-sidecar-ipc-protocol.md``. The
.NET sidecar under ``dotnet/`` must match it byte for byte;
``tests/unit/test_teams_sidecar_protocol.py`` holds the conformance vector.

Pure: no I/O, no sockets, no asyncio. Encoding is a function of its arguments and
decoding is an incremental state machine over a byte stream — so the whole boundary is
exhaustively testable on Linux or macOS with no Windows host, no Azure tenant, and no
Graph consent. That is the point of writing it down before the sidecar exists, and it
is what makes the Teams connector developable on the machines we actually have.

**Why this is not Zoom's protocol.** ``connectors/zoom/publisher/protocol.py`` is a
frozen contract (doc 004) already deployed against a C++ binary, and it is shaped for
the job it does: unidirectional media, over a Unix socket, on one host. Teams differs
in every one of those dimensions —

* **media flows both ways.** The Teams media platform owns receive *and* send in one
  session, so participant audio arrives *up* this link rather than over a separate
  WebSocket as RTMS does. Zoom's ``AUDIO_PCM`` is bridge→sidecar only.
* **it crosses a host boundary.** TCP with TLS between a Linux container and a
  Windows host, not a UDS on a shared volume.
* **the control payloads have nothing in common.** A Graph join descriptor and an
  Azure AD credential versus a Zoom SDK JWT and a meeting number.
* **audio carries a source identity.** Teams unmixed buffers are tagged with a media
  source id; Zoom attributes per-participant audio on the RTMS leg instead.

Sharing one codec would mean unfreezing a contract that is running in production to
add fields only Teams uses, so the two stay separate. The cost is a few hundred lines
of framing that look similar; the alternative is coupling Zoom's release cycle to
Teams' — which is the thing this repository is explicitly organised to avoid.
"""

from __future__ import annotations

import json
import struct
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import IntEnum, IntFlag
from typing import Any, Final

from src.connectors.teams.exceptions import SidecarProtocolError
from src.domain.media import AudioFormat, AudioFrame, PixelFormat, SampleFormat, VideoFrame

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

MAGIC: Final[int] = 0x544D4331
"""ASCII ``'TMC1'`` — Teams Meeting Connector, wire 1. Distinct from Zoom's
``'ZMC1'`` so that pointing a bridge at the wrong sidecar fails on the first frame
with a named error instead of decoding garbage."""

WIRE_VERSION: Final[int] = 1

HEADER_STRUCT: Final = struct.Struct(">IBBBBIqI")
HEADER_SIZE: Final[int] = HEADER_STRUCT.size  # 24

VIDEO_HEADER_STRUCT: Final = struct.Struct(">HHHHHH")
VIDEO_HEADER_SIZE: Final[int] = VIDEO_HEADER_STRUCT.size  # 12

AUDIO_HEADER_STRUCT: Final = struct.Struct(">IBBHI")
AUDIO_HEADER_SIZE: Final[int] = AUDIO_HEADER_STRUCT.size  # 12

MAX_PAYLOAD_BYTES: Final[int] = 8 * 1024 * 1024
"""Hard ceiling on a single message. A 1080p I420 frame is ~3.1 MB, so this leaves
headroom without letting a corrupt length field make us allocate a gigabyte."""

MIXED_SOURCE: Final[int] = 0
"""``source_msi`` sentinel for "mixed, or not attributable". Always used for
bridge→sidecar audio, which has exactly one source: us."""

_SAMPLE_FORMAT_S16LE: Final[int] = 1
_SAMPLE_FORMAT_TO_WIRE: Final[dict[SampleFormat, int]] = {
    SampleFormat.S16LE: _SAMPLE_FORMAT_S16LE,
}
_WIRE_TO_SAMPLE_FORMAT: Final[dict[int, SampleFormat]] = {
    _SAMPLE_FORMAT_S16LE: SampleFormat.S16LE,
}


class TeamsMessageType(IntEnum):
    """Message discriminator."""

    VIDEO_I420 = 0x01
    """Bridge → sidecar. Packed I420 planes. The sidecar converts to NV12 while
    copying into the media platform's send buffer — it has to make that copy anyway,
    so the interleave is close to free there, and doing it in C# keeps a per-frame
    1.4 MB byte shuffle out of the Python event loop (doc 005 §4.1)."""

    AUDIO_PCM = 0x02
    """**Bidirectional.** Bridge → sidecar is the avatar speaking; sidecar → bridge is
    participant audio. One type in both directions because the payload is identical
    and the direction is implied by which end received it."""

    CONTROL_JOIN = 0x03
    """Bridge → sidecar. Credentials and the Graph join descriptor."""

    CONTROL_LEAVE = 0x04
    HEARTBEAT = 0x05
    READY = 0x06
    ERROR = 0x07

    ROSTER = 0x08
    """Sidecar → bridge. Participants, including the bot's own entry — which is how
    ``EchoGuard`` learns the identity to filter."""

    CALL_STATE = 0x09
    """Sidecar → bridge. Graph call state transitions, so a service-side teardown
    surfaces as a health change rather than as silence."""

    @property
    def is_media(self) -> bool:
        return self in (TeamsMessageType.VIDEO_I420, TeamsMessageType.AUDIO_PCM)

    @property
    def is_json(self) -> bool:
        return not self.is_media


class TeamsFlags(IntFlag):
    """Advisory per-message flags. Never load-bearing for decoding."""

    NONE = 0x00
    KEYFRAME = 0x01
    UNMIXED = 0x02
    """Audio from the sidecar came from an unmixed (per-participant) buffer, so
    ``source_msi`` is meaningful."""
    SILENCE = 0x04
    """Payload is digital silence. Diagnostics only — the sidecar still sends it, since
    the media platform needs a continuous cadence."""


class CallState(IntEnum):
    """Graph call lifecycle, as reported in ``CALL_STATE``."""

    ESTABLISHING = 1
    ESTABLISHED = 2
    TERMINATING = 3
    TERMINATED = 4


# --------------------------------------------------------------------------- #
# Decoded messages
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class AudioWireHeader:
    """The 12-byte prologue on an ``AUDIO_PCM`` payload."""

    sample_rate_hz: int
    channels: int
    sample_format: SampleFormat
    frame_ms: int
    source_msi: int = MIXED_SOURCE

    def to_format(self) -> AudioFormat:
        return AudioFormat(
            sample_rate_hz=self.sample_rate_hz,
            channels=self.channels,
            sample_format=self.sample_format,
        )


@dataclass(frozen=True, slots=True)
class VideoWireHeader:
    """The 12-byte prologue on a ``VIDEO_I420`` payload."""

    width: int
    height: int
    stride_y: int
    stride_uv: int
    fps: int


@dataclass(frozen=True, slots=True)
class TeamsMessage:
    """One decoded frame off the wire."""

    msg_type: TeamsMessageType
    flags: TeamsFlags
    seq: int
    pts_us: int
    payload: bytes = field(repr=False)

    def json(self) -> dict[str, Any]:
        """Decode a control payload.

        Raises:
            SidecarProtocolError: the payload is not a JSON object.
        """
        if not self.payload:
            return {}
        try:
            decoded = json.loads(self.payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise SidecarProtocolError(
                f"{self.msg_type.name} payload is not valid UTF-8 JSON: {exc}"
            ) from exc
        if not isinstance(decoded, dict):
            raise SidecarProtocolError(f"{self.msg_type.name} payload is not a JSON object")
        return decoded

    def audio(self) -> tuple[AudioWireHeader, bytes]:
        """Split an ``AUDIO_PCM`` payload into its header and PCM bytes.

        Raises:
            SidecarProtocolError: wrong message type, or a truncated header.
        """
        if self.msg_type is not TeamsMessageType.AUDIO_PCM:
            raise SidecarProtocolError(f"expected AUDIO_PCM, got {self.msg_type.name}")
        return decode_audio_payload(self.payload)

    def video(self) -> tuple[VideoWireHeader, bytes]:
        """Split a ``VIDEO_I420`` payload into its header and plane bytes.

        Raises:
            SidecarProtocolError: wrong message type, or a truncated header.
        """
        if self.msg_type is not TeamsMessageType.VIDEO_I420:
            raise SidecarProtocolError(f"expected VIDEO_I420, got {self.msg_type.name}")
        return decode_video_payload(self.payload)


# --------------------------------------------------------------------------- #
# Encoding
# --------------------------------------------------------------------------- #


def encode_header(
    msg_type: TeamsMessageType,
    *,
    payload_len: int,
    seq: int = 0,
    pts_us: int = 0,
    flags: TeamsFlags = TeamsFlags.NONE,
) -> bytes:
    """Build the 24-byte frame header."""
    if payload_len > MAX_PAYLOAD_BYTES:
        raise SidecarProtocolError(f"payload of {payload_len} bytes exceeds {MAX_PAYLOAD_BYTES}")
    return HEADER_STRUCT.pack(
        MAGIC,
        WIRE_VERSION,
        int(msg_type),
        int(flags),
        0,  # reserved
        seq & 0xFFFFFFFF,
        pts_us,
        payload_len,
    )


def encode_json(
    msg_type: TeamsMessageType, body: dict[str, Any], *, seq: int = 0, pts_us: int = 0
) -> bytes:
    """Encode a control message.

    ``separators`` and ``sort_keys`` are pinned so the same body always produces the
    same bytes — which is what lets the conformance vector be a literal.
    """
    payload = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return encode_header(msg_type, payload_len=len(payload), seq=seq, pts_us=pts_us) + payload


def encode_audio(
    frame: AudioFrame,
    *,
    seq: int = 0,
    source_msi: int = MIXED_SOURCE,
    flags: TeamsFlags = TeamsFlags.NONE,
) -> bytes:
    """Encode one PCM frame.

    Raises:
        SidecarProtocolError: the frame's sample format has no wire encoding.
    """
    wire_format = _SAMPLE_FORMAT_TO_WIRE.get(frame.format.sample_format)
    if wire_format is None:
        raise SidecarProtocolError(f"unsupported sample format {frame.format.sample_format}")

    if frame.is_silence:
        flags |= TeamsFlags.SILENCE

    header = AUDIO_HEADER_STRUCT.pack(
        frame.format.sample_rate_hz,
        frame.format.channels,
        wire_format,
        min(frame.duration_us // 1000, 0xFFFF),
        source_msi & 0xFFFFFFFF,
    )
    payload_len = AUDIO_HEADER_SIZE + len(frame.pcm)
    return (
        encode_header(
            TeamsMessageType.AUDIO_PCM,
            payload_len=payload_len,
            seq=seq,
            pts_us=frame.pts_us,
            flags=flags,
        )
        + header
        + frame.pcm
    )


def encode_video(frame: VideoFrame, *, seq: int = 0) -> bytes:
    """Encode one packed-I420 frame.

    Raises:
        SidecarProtocolError: the frame is not I420.
    """
    if frame.format.pixel_format is not PixelFormat.I420:
        raise SidecarProtocolError(
            f"the Teams video socket path carries I420, got {frame.format.pixel_format}"
        )

    flags = TeamsFlags.KEYFRAME if frame.is_keyframe else TeamsFlags.NONE
    header = VIDEO_HEADER_STRUCT.pack(
        frame.format.width,
        frame.format.height,
        frame.format.width,  # stride_y — planes are packed, so stride == width
        frame.format.width // 2,  # stride_uv
        frame.format.fps,
        0,  # reserved
    )
    payload_len = VIDEO_HEADER_SIZE + len(frame.planes)
    return (
        encode_header(
            TeamsMessageType.VIDEO_I420,
            payload_len=payload_len,
            seq=seq,
            pts_us=frame.pts_us,
            flags=flags,
        )
        + header
        + frame.planes
    )


# --------------------------------------------------------------------------- #
# Payload decoding
# --------------------------------------------------------------------------- #


def decode_audio_payload(payload: bytes) -> tuple[AudioWireHeader, bytes]:
    """Split an ``AUDIO_PCM`` payload.

    Raises:
        SidecarProtocolError: truncated header, or an unknown sample format.
    """
    if len(payload) < AUDIO_HEADER_SIZE:
        raise SidecarProtocolError(
            f"audio payload of {len(payload)} bytes is shorter than its {AUDIO_HEADER_SIZE}-byte "
            "header"
        )
    rate, channels, wire_format, frame_ms, source_msi = AUDIO_HEADER_STRUCT.unpack_from(payload)
    sample_format = _WIRE_TO_SAMPLE_FORMAT.get(wire_format)
    if sample_format is None:
        raise SidecarProtocolError(f"unknown wire sample format {wire_format}")
    if channels < 1:
        raise SidecarProtocolError(f"audio header declares {channels} channels")
    if rate < 1:
        raise SidecarProtocolError(f"audio header declares {rate} Hz")
    return (
        AudioWireHeader(
            sample_rate_hz=rate,
            channels=channels,
            sample_format=sample_format,
            frame_ms=frame_ms,
            source_msi=source_msi,
        ),
        payload[AUDIO_HEADER_SIZE:],
    )


def decode_video_payload(payload: bytes) -> tuple[VideoWireHeader, bytes]:
    """Split a ``VIDEO_I420`` payload.

    Raises:
        SidecarProtocolError: truncated header.
    """
    if len(payload) < VIDEO_HEADER_SIZE:
        raise SidecarProtocolError(
            f"video payload of {len(payload)} bytes is shorter than its {VIDEO_HEADER_SIZE}-byte "
            "header"
        )
    width, height, stride_y, stride_uv, fps, _reserved = VIDEO_HEADER_STRUCT.unpack_from(payload)
    return (
        VideoWireHeader(
            width=width, height=height, stride_y=stride_y, stride_uv=stride_uv, fps=fps
        ),
        payload[VIDEO_HEADER_SIZE:],
    )


# --------------------------------------------------------------------------- #
# Stream decoding
# --------------------------------------------------------------------------- #


class TeamsFrameDecoder:
    """Incremental framing decoder over a byte stream.

    TCP gives no message boundaries, so this accumulates and yields whole messages.
    It never resynchronises on a bad magic or version: a desynced binary stream cannot
    be realigned with confidence, and guessing would surface as corrupt audio rather
    than as an error. The link tears down and rebuilds instead (doc 005 §6).
    """

    __slots__ = ("_buffer",)

    def __init__(self) -> None:
        self._buffer = bytearray()

    def reset(self) -> None:
        """Discard buffered bytes. Called on (re)connect."""
        self._buffer.clear()

    @property
    def buffered(self) -> int:
        return len(self._buffer)

    def feed(self, data: bytes) -> Iterator[TeamsMessage]:
        """Add bytes and yield every message they complete.

        Raises:
            SidecarProtocolError: bad magic, unknown version or type, or a payload
                length past the ceiling.
        """
        self._buffer.extend(data)

        while True:
            if len(self._buffer) < HEADER_SIZE:
                return

            magic, version, raw_type, raw_flags, _reserved, seq, pts_us, payload_len = (
                HEADER_STRUCT.unpack_from(self._buffer)
            )

            if magic != MAGIC:
                raise SidecarProtocolError(
                    f"bad magic 0x{magic:08X} (expected 0x{MAGIC:08X}); "
                    "the stream is desynced or this is not a Teams sidecar"
                )
            if version != WIRE_VERSION:
                raise SidecarProtocolError(
                    f"wire version {version} is not supported (expected {WIRE_VERSION})"
                )
            if payload_len > MAX_PAYLOAD_BYTES:
                raise SidecarProtocolError(
                    f"declared payload of {payload_len} bytes exceeds {MAX_PAYLOAD_BYTES}"
                )

            total = HEADER_SIZE + payload_len
            if len(self._buffer) < total:
                return  # incomplete — wait for more bytes

            payload = bytes(self._buffer[HEADER_SIZE:total])
            del self._buffer[:total]

            try:
                msg_type = TeamsMessageType(raw_type)
            except ValueError as exc:
                raise SidecarProtocolError(f"unknown message type 0x{raw_type:02X}") from exc

            yield TeamsMessage(
                msg_type=msg_type,
                flags=TeamsFlags(raw_flags),
                seq=seq,
                pts_us=pts_us,
                payload=payload,
            )
