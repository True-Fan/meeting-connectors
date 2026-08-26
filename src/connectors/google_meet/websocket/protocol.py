"""Page bridge wire codec — wire version 1.

Reference implementation of ``docs/design/007-google-meet-connector-architecture.md``
§5. The browser-side encoder in ``js/bridge.js`` must match it byte for byte;
``tests/unit/test_google_meet_protocol.py`` holds the conformance vector, and
``tests/unit/test_google_meet_js_assets.py`` checks the two constant sets have not
drifted apart.

Pure: no sockets, no asyncio, no Playwright. Encoding is a function of its arguments and
decoding is a function of a single frame — so the entire boundary is testable with no
Chromium, no Google account, and no meeting. That is the same argument
the removed Teams sidecar's protocol made for writing the codec before the sidecar
exists, and it is what makes this connector developable on a laptop.

**Why there is no incremental decoder here, unlike Teams.** ``TeamsFrameDecoder``
accumulates bytes and hunts for message boundaries because TCP is a byte stream that
provides none. WebSocket is message-oriented: the transport already delivers exactly the
frames the sender wrote. So ``decode`` takes one complete message and the whole
resynchronisation problem — and the class of desync bug that comes with it — does not
exist on this link. The length field is kept anyway, as a cheap self-consistency check
against a page running a mismatched script.

**Why this is not Zoom's or Teams' protocol.** Zoom's (doc 004) is a frozen contract
already deployed against a C++ binary, and Teams' carries Graph credentials and media
source ids across a host boundary. This one carries neither: it is loopback-only, it has
no credentials on it at all — the Google session lives in the browser profile, never on
this wire — and its peer is JavaScript, which is why every integer is big-endian and
every payload is either raw PCM, raw I420, or UTF-8 JSON. ``DataView`` reads those three
without a library.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass, field
from enum import IntEnum, IntFlag, StrEnum
from typing import Any, Final

from src.connectors.google_meet.exceptions import BridgeProtocolError
from src.domain.media import AudioFormat, AudioFrame, PixelFormat, SampleFormat, VideoFrame

# --------------------------------------------------------------------------- #
# Constants — mirrored in js/bridge.js
# --------------------------------------------------------------------------- #

MAGIC: Final[int] = 0x474D4331
"""ASCII ``'GMC1'`` — Google Meet Connector, wire 1. Distinct from Zoom's ``'ZMC1'``
and Teams' ``'TMC1'`` so that a bridge pointed at the wrong peer fails on the first
frame with a named error instead of decoding garbage."""

WIRE_VERSION: Final[int] = 1

HEADER_STRUCT: Final = struct.Struct(">IBBBBIqI")
HEADER_SIZE: Final[int] = HEADER_STRUCT.size  # 24

VIDEO_HEADER_STRUCT: Final = struct.Struct(">HHHHHH")
VIDEO_HEADER_SIZE: Final[int] = VIDEO_HEADER_STRUCT.size  # 12

AUDIO_HEADER_STRUCT: Final = struct.Struct(">IBBHI")
AUDIO_HEADER_SIZE: Final[int] = AUDIO_HEADER_STRUCT.size  # 12

MAX_PAYLOAD_BYTES: Final[int] = 8 * 1024 * 1024
"""Hard ceiling on one message. A 1080p I420 frame is ~3.1 MB, so this leaves headroom
without letting a corrupt length field make us allocate a gigabyte."""

MIXED_SOURCE: Final[int] = 0
"""``source_id`` sentinel for "mixed, or not attributable".

**Always** used for page→bridge audio, and that is a real property of this connector
rather than a placeholder: the capture graph in ``js/bridge.js`` sums every remote
participant's track into one mono node before the worklet sees it, so inbound audio is
mixed by construction. ``EchoGuard`` is told as much and runs its speaking gate in
strict mode — see ``audio_capture/mapping.py``."""

_SAMPLE_FORMAT_S16LE: Final[int] = 1
_SAMPLE_FORMAT_TO_WIRE: Final[dict[SampleFormat, int]] = {
    SampleFormat.S16LE: _SAMPLE_FORMAT_S16LE,
}
_WIRE_TO_SAMPLE_FORMAT: Final[dict[int, SampleFormat]] = {
    _SAMPLE_FORMAT_S16LE: SampleFormat.S16LE,
}


class MeetMessageType(IntEnum):
    """Message discriminator."""

    VIDEO_I420 = 0x01
    """Bridge → page. Packed I420 planes for the synthetic camera track.

    I420 rather than RGBA because it is what the shared decoder already produces and
    what ``WebCodecs.VideoFrame`` accepts natively — so the frame crosses this link and
    reaches a real ``MediaStreamTrack`` with no colour conversion anywhere, in either
    language. RGBA would also be twice the bytes."""

    AUDIO_PCM = 0x02
    """**Bidirectional.** Page → bridge is the conference; bridge → page is the avatar
    speaking. One type in both directions because the payload is identical and the
    direction is implied by which end received it."""

    HELLO = 0x03
    """Page → bridge. The session token and what the page's Chromium can do."""

    CONFIG = 0x04
    """Bridge → page. Media formats and behaviour, sent in reply to ``HELLO``.

    Formats are pushed rather than hardcoded in the JavaScript so that changing the
    publish geometry is a settings change, not an asset edit."""

    READY = 0x05
    """Page → bridge. Worklets are running and the synthetic tracks exist."""

    LEAVE = 0x06
    """Bridge → page. Leave the call cleanly, so the avatar disappears from the roster
    instead of hanging as a frozen tile until Meet times it out."""

    HEARTBEAT = 0x07
    ERROR = 0x08

    PARTICIPANTS = 0x09
    """Page → bridge. The roster as the page observes it, including our own entry."""

    MEET_STATE = 0x0A
    """Page → bridge. Admission and call-state transitions, so being denied or ejected
    surfaces as a health change rather than as silence."""

    CHAT_MESSAGE = 0x0C
    """Page → bridge. One message observed in the meeting's chat panel.

    Page-observed rather than API-fetched because Meet exposes no chat API to a participant;
    the panel's DOM is the only source. The page reports ``{id, text, sender, isSelf}`` and
    makes no decision about whether the avatar should answer — that is the bridge's call, and
    it is where the self-message filter lives."""

    HAND_RAISE = 0x0D
    """Page → bridge. A participant just raised their hand.

    **Edge-triggered, and that is the whole contract.** Meet renders a hand as a *state* — an
    indicator that stays on the tile until it is lowered — and the page re-renders it on
    nearly every mutation. What the avatar has to react to is the moment it goes up, so the
    page reports transitions and holds the current set itself; a level signal would arrive
    dozens of times per raised hand and the bridge would have to reconstruct the edge anyway.

    The page reports ``{id, name, isSelf}`` and makes no decision about whether to interrupt.
    Like ``CHAT_MESSAGE``, that judgement is the bridge's — see ``meeting/hand_raise.py``."""

    ACTIVE_SPEAKER = 0x0E
    """Page → bridge. One participant started or stopped speaking.

    **Edge-triggered, like ``HAND_RAISE``, and for a stronger reason.** Speech is continuous:
    a level signal would arrive several times a second per participant, on the same socket that
    carries the meeting's audio. The page holds the current set and reports its transitions.

    ``{trackId, id, name, speaking, source, level, heldMs}``. ``trackId`` is stable for the life
    of the remote track and is the only field guaranteed to be present — ``id`` and ``name``
    are filled in once the page can see which tile the stream is rendered on, which may be
    *after* somebody has already started talking. A repeated ``speaking: true`` for the same
    ``trackId`` is therefore an identity refresh rather than a new turn, so an open turn is
    renamed instead of split (see ``meeting/active_speaker.py``).

    ``source`` distinguishes the two independent observations: ``audio`` is per-track energy,
    measured on an ``AnalyserNode`` branched off the source node that already feeds the mix, and
    ``dom`` is Meet's own speaking indicator. The first says *when* and the second says *who*,
    and neither is on the media path — **this message exists precisely so that attribution does
    not have to be**. ``MIXED_SOURCE`` still holds for every audio frame: the capture graph
    mixes before it samples, and adding a name to a frame would mean unmixing it."""

    CAPTION = 0x0F
    """Page → bridge. One settled line of Meet's own captions, with the name beside it.

    **The only place in the page where a name and the words that person said appear together.**
    Attribution here is built from audio levels and DOM observation, which say *who* is talking
    but never *what*; the agent's transcription hears the words but receives one mixed stream and
    so cannot attribute them. Meet's captions have both, because Meet transcribes per participant.

    ``{speaker, text, isSelf}``. Sent when a caption has stopped changing — Meet extends a line
    word by word as somebody keeps talking, so forwarding on sight would deliver a dozen fragments
    of one sentence. The page decides only when a line is *final*; whether it is worth keeping,
    whose it is, and what the agent is told are Python's (``meeting/transcript.py``).

    Not on the media path in any sense: this is a DOM read of a small panel, rate limited like
    chat, and it changes nothing about the audio the avatar sends or receives."""

    PAGE_EVENT = 0x0B
    """Page → bridge. Raw diagnostics — a track ended, the peer connection
    renegotiated, a device was revoked.

    The page reports facts and never interprets them: the Chromium bridge is required to
    hold no logging or metrics, so events travel up and the Python side decides what
    they mean and records them."""

    @property
    def is_media(self) -> bool:
        return self in (MeetMessageType.VIDEO_I420, MeetMessageType.AUDIO_PCM)

    @property
    def is_json(self) -> bool:
        return not self.is_media


class MeetFlags(IntFlag):
    """Advisory per-message flags. Never load-bearing for decoding."""

    NONE = 0x00
    KEYFRAME = 0x01
    SILENCE = 0x02
    """Payload is digital silence. Diagnostics only — it is still sent, because the
    synthetic microphone needs a continuous cadence."""
    MIXED = 0x04
    """Audio is a mix of every remote participant, so ``source_id`` is not meaningful.
    Set on all page→bridge audio."""


class MeetState(StrEnum):
    """Where the browser is in the meeting, as reported over ``MEET_STATE``.

    Modelled on what the Meet UI actually distinguishes, because each of these needs a
    different response and collapsing them would lose that. ``LOBBY`` in particular is
    not a failure and must not start a retry clock — someone has to click "Admit".
    """

    JOINING = "joining"
    LOBBY = "lobby"
    """"Asking to join" — waiting on a host. Healthy, just not yet in."""

    JOINED = "joined"
    LEFT = "left"
    """We left, or were dropped. Recoverable by rejoining."""

    ENDED = "ended"
    """The conference is over. Terminal and not our failure."""

    DENIED = "denied"
    """A host refused entry. Fatal: retrying is what gets an account blocked."""

    EJECTED = "ejected"
    """A host removed us mid-call. Fatal, for the same reason."""

    @property
    def is_in_call(self) -> bool:
        return self is MeetState.JOINED

    @property
    def is_fatal(self) -> bool:
        """True when rejoining is the wrong response."""
        return self in (MeetState.DENIED, MeetState.EJECTED, MeetState.ENDED)


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
    source_id: int = MIXED_SOURCE

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
class MeetMessage:
    """One decoded message off the wire."""

    msg_type: MeetMessageType
    flags: MeetFlags
    seq: int
    pts_us: int
    payload: bytes = field(repr=False)

    def json(self) -> dict[str, Any]:
        """Decode a control payload.

        Raises:
            BridgeProtocolError: the payload is not a JSON object.
        """
        if not self.payload:
            return {}
        try:
            decoded = json.loads(self.payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise BridgeProtocolError(
                f"{self.msg_type.name} payload is not valid UTF-8 JSON: {exc}"
            ) from exc
        if not isinstance(decoded, dict):
            raise BridgeProtocolError(f"{self.msg_type.name} payload is not a JSON object")
        return decoded

    def audio(self) -> tuple[AudioWireHeader, bytes]:
        """Split an ``AUDIO_PCM`` payload into its header and PCM bytes.

        Raises:
            BridgeProtocolError: wrong message type, or a truncated header.
        """
        if self.msg_type is not MeetMessageType.AUDIO_PCM:
            raise BridgeProtocolError(f"expected AUDIO_PCM, got {self.msg_type.name}")
        return decode_audio_payload(self.payload)

    def video(self) -> tuple[VideoWireHeader, bytes]:
        """Split a ``VIDEO_I420`` payload into its header and plane bytes.

        Raises:
            BridgeProtocolError: wrong message type, or a truncated header.
        """
        if self.msg_type is not MeetMessageType.VIDEO_I420:
            raise BridgeProtocolError(f"expected VIDEO_I420, got {self.msg_type.name}")
        return decode_video_payload(self.payload)


# --------------------------------------------------------------------------- #
# Encoding
# --------------------------------------------------------------------------- #


def encode_header(
    msg_type: MeetMessageType,
    *,
    payload_len: int,
    seq: int = 0,
    pts_us: int = 0,
    flags: MeetFlags = MeetFlags.NONE,
) -> bytes:
    """Build the 24-byte message header.

    Raises:
        BridgeProtocolError: the payload is above the ceiling.
    """
    if payload_len > MAX_PAYLOAD_BYTES:
        raise BridgeProtocolError(f"payload of {payload_len} bytes exceeds {MAX_PAYLOAD_BYTES}")
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
    msg_type: MeetMessageType, body: dict[str, Any], *, seq: int = 0, pts_us: int = 0
) -> bytes:
    """Encode a control message.

    ``separators`` and ``sort_keys`` are pinned so the same body always produces the
    same bytes, which is what lets the conformance vector be a literal.
    """
    payload = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return encode_header(msg_type, payload_len=len(payload), seq=seq, pts_us=pts_us) + payload


def encode_audio(
    frame: AudioFrame,
    *,
    seq: int = 0,
    source_id: int = MIXED_SOURCE,
    flags: MeetFlags = MeetFlags.NONE,
) -> bytes:
    """Encode one PCM frame for the synthetic microphone.

    Raises:
        BridgeProtocolError: the frame's sample format has no wire encoding.
    """
    wire_format = _SAMPLE_FORMAT_TO_WIRE.get(frame.format.sample_format)
    if wire_format is None:
        raise BridgeProtocolError(f"unsupported sample format {frame.format.sample_format}")

    if frame.is_silence:
        flags |= MeetFlags.SILENCE

    header = AUDIO_HEADER_STRUCT.pack(
        frame.format.sample_rate_hz,
        frame.format.channels,
        wire_format,
        min(frame.duration_us // 1000, 0xFFFF),
        source_id & 0xFFFFFFFF,
    )
    return (
        encode_header(
            MeetMessageType.AUDIO_PCM,
            payload_len=AUDIO_HEADER_SIZE + len(frame.pcm),
            seq=seq,
            pts_us=frame.pts_us,
            flags=flags,
        )
        + header
        + frame.pcm
    )


def encode_video(frame: VideoFrame, *, seq: int = 0) -> bytes:
    """Encode one packed-I420 frame for the synthetic camera.

    Strides equal the plane widths because the shared decoder emits packed planes, and
    they are on the wire anyway so that ``js/bridge.js`` can hand
    ``WebCodecs.VideoFrame`` an explicit layout rather than inferring one — an inferred
    layout that is wrong produces a sheared image, which is a slow thing to diagnose
    from the far side of a browser.

    Raises:
        BridgeProtocolError: the frame is not I420.
    """
    if frame.format.pixel_format is not PixelFormat.I420:
        raise BridgeProtocolError(
            f"the synthetic camera path carries I420, got {frame.format.pixel_format}"
        )

    flags = MeetFlags.KEYFRAME if frame.is_keyframe else MeetFlags.NONE
    header = VIDEO_HEADER_STRUCT.pack(
        frame.format.width,
        frame.format.height,
        frame.format.width,  # stride_y — planes are packed, so stride == width
        frame.format.width // 2,  # stride_uv
        frame.format.fps,
        0,  # reserved
    )
    return (
        encode_header(
            MeetMessageType.VIDEO_I420,
            payload_len=VIDEO_HEADER_SIZE + len(frame.planes),
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
        BridgeProtocolError: truncated header, unknown sample format, or a header that
            declares an impossible format.
    """
    if len(payload) < AUDIO_HEADER_SIZE:
        raise BridgeProtocolError(
            f"audio payload of {len(payload)} bytes is shorter than its "
            f"{AUDIO_HEADER_SIZE}-byte header"
        )
    rate, channels, wire_format, frame_ms, source_id = AUDIO_HEADER_STRUCT.unpack_from(payload)
    sample_format = _WIRE_TO_SAMPLE_FORMAT.get(wire_format)
    if sample_format is None:
        raise BridgeProtocolError(f"unknown wire sample format {wire_format}")
    if channels < 1:
        raise BridgeProtocolError(f"audio header declares {channels} channels")
    if rate < 1:
        raise BridgeProtocolError(f"audio header declares {rate} Hz")
    return (
        AudioWireHeader(
            sample_rate_hz=rate,
            channels=channels,
            sample_format=sample_format,
            frame_ms=frame_ms,
            source_id=source_id,
        ),
        payload[AUDIO_HEADER_SIZE:],
    )


def decode_video_payload(payload: bytes) -> tuple[VideoWireHeader, bytes]:
    """Split a ``VIDEO_I420`` payload.

    Raises:
        BridgeProtocolError: truncated header.
    """
    if len(payload) < VIDEO_HEADER_SIZE:
        raise BridgeProtocolError(
            f"video payload of {len(payload)} bytes is shorter than its "
            f"{VIDEO_HEADER_SIZE}-byte header"
        )
    width, height, stride_y, stride_uv, fps, _reserved = VIDEO_HEADER_STRUCT.unpack_from(payload)
    return (
        VideoWireHeader(
            width=width, height=height, stride_y=stride_y, stride_uv=stride_uv, fps=fps
        ),
        payload[VIDEO_HEADER_SIZE:],
    )


# --------------------------------------------------------------------------- #
# Message decoding
# --------------------------------------------------------------------------- #


def decode(data: bytes) -> MeetMessage:
    """Decode one complete WebSocket message.

    Single-shot, not incremental: see the module docstring for why a message-oriented
    transport removes the need for stream reassembly.

    Raises:
        BridgeProtocolError: too short for a header, bad magic, unknown version or
            message type, or a length field that disagrees with the frame it arrived in.
    """
    if len(data) < HEADER_SIZE:
        raise BridgeProtocolError(
            f"message of {len(data)} bytes is shorter than the {HEADER_SIZE}-byte header"
        )

    magic, version, raw_type, raw_flags, _reserved, seq, pts_us, payload_len = (
        HEADER_STRUCT.unpack_from(data)
    )

    if magic != MAGIC:
        raise BridgeProtocolError(
            f"bad magic 0x{magic:08X} (expected 0x{MAGIC:08X}); the peer is not the "
            "Google Meet page bridge"
        )
    if version != WIRE_VERSION:
        raise BridgeProtocolError(
            f"wire version {version} is not supported (expected {WIRE_VERSION}); the "
            "page is running a mismatched js/bridge.js"
        )
    if payload_len > MAX_PAYLOAD_BYTES:
        raise BridgeProtocolError(
            f"declared payload of {payload_len} bytes exceeds {MAX_PAYLOAD_BYTES}"
        )

    available = len(data) - HEADER_SIZE
    if available != payload_len:
        # WebSocket already guarantees the frame is whole, so a mismatch is not a short
        # read — it means the sender's own accounting is wrong, which is a script
        # version skew worth naming rather than truncating past.
        raise BridgeProtocolError(
            f"{_type_name(raw_type)} declares {payload_len} payload bytes but the "
            f"message carries {available}"
        )

    try:
        msg_type = MeetMessageType(raw_type)
    except ValueError as exc:
        raise BridgeProtocolError(f"unknown message type 0x{raw_type:02X}") from exc

    return MeetMessage(
        msg_type=msg_type,
        flags=MeetFlags(raw_flags),
        seq=seq,
        pts_us=pts_us,
        payload=bytes(data[HEADER_SIZE : HEADER_SIZE + payload_len]),
    )


def _type_name(raw_type: int) -> str:
    """Best-effort name for an error message, without raising on an unknown type."""
    try:
        return MeetMessageType(raw_type).name
    except ValueError:
        return f"type 0x{raw_type:02X}"
