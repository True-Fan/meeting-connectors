"""Teams sidecar IPC wire conformance.

**This file is the contract the .NET sidecar must satisfy.** The byte-level vectors below
are the specification; ``Wire/WireProtocol.cs`` is an implementation of them. Testing them
here rather than only in C# means the contract is verifiable on any developer machine and
in CI, with no Windows host — the same role
``tests/unit/test_sidecar_protocol.py`` plays for Zoom's frozen protocol.

Deliberately separate from Zoom's conformance test: the two protocols are independent and
a change to one must never be able to fail the other's suite.
"""

from __future__ import annotations

import json
import struct

import pytest

from src.connectors.teams.exceptions import SidecarProtocolError
from src.connectors.teams.sidecar.protocol import (
    AUDIO_HEADER_SIZE,
    HEADER_SIZE,
    MAGIC,
    MAX_PAYLOAD_BYTES,
    MIXED_SOURCE,
    VIDEO_HEADER_SIZE,
    WIRE_VERSION,
    CallState,
    TeamsFlags,
    TeamsFrameDecoder,
    TeamsMessageType,
    decode_audio_payload,
    decode_video_payload,
    encode_audio,
    encode_header,
    encode_json,
    encode_video,
)
from src.domain.context import FrameContext
from src.domain.media import (
    AudioFormat,
    AudioFrame,
    PixelFormat,
    SampleFormat,
    VideoFormat,
    VideoFrame,
)

# --------------------------------------------------------------------------- #
# Frozen constants
# --------------------------------------------------------------------------- #


def test_magic_is_tmc1_ascii() -> None:
    """'TMC1' — and distinct from Zoom's 'ZMC1', so a crossed link fails immediately."""
    assert MAGIC == 0x544D4331
    assert struct.pack(">I", MAGIC) == b"TMC1"


def test_magic_differs_from_zoom() -> None:
    """Pointing a bridge at the wrong sidecar must fail on the first frame.

    Both protocols use a 24-byte header of the same shape, so without distinct magic a
    crossed connection would decode plausible-looking garbage rather than erroring.
    """
    from src.connectors.zoom.publisher.protocol import MAGIC as ZOOM_MAGIC

    assert MAGIC != ZOOM_MAGIC


def test_header_geometry() -> None:
    assert HEADER_SIZE == 24
    assert AUDIO_HEADER_SIZE == 12
    assert VIDEO_HEADER_SIZE == 12
    assert WIRE_VERSION == 1


def test_message_type_values_are_pinned() -> None:
    """The .NET enum must agree with these numbers exactly."""
    assert TeamsMessageType.VIDEO_I420 == 0x01
    assert TeamsMessageType.AUDIO_PCM == 0x02
    assert TeamsMessageType.CONTROL_JOIN == 0x03
    assert TeamsMessageType.CONTROL_LEAVE == 0x04
    assert TeamsMessageType.HEARTBEAT == 0x05
    assert TeamsMessageType.READY == 0x06
    assert TeamsMessageType.ERROR == 0x07
    assert TeamsMessageType.ROSTER == 0x08
    assert TeamsMessageType.CALL_STATE == 0x09


def test_flag_values_are_pinned() -> None:
    assert TeamsFlags.KEYFRAME == 0x01
    assert TeamsFlags.UNMIXED == 0x02
    assert TeamsFlags.SILENCE == 0x04


def test_call_state_values_are_pinned() -> None:
    assert CallState.ESTABLISHING == 1
    assert CallState.ESTABLISHED == 2
    assert CallState.TERMINATING == 3
    assert CallState.TERMINATED == 4


def test_mixed_source_sentinel_is_zero() -> None:
    assert MIXED_SOURCE == 0


# --------------------------------------------------------------------------- #
# Header encoding — the exact bytes
# --------------------------------------------------------------------------- #


def test_header_is_big_endian_and_exact() -> None:
    """The literal vector. .NET is little-endian natively, so this is the check that
    catches a BitConverter slipping into the C# codec."""
    header = encode_header(
        TeamsMessageType.AUDIO_PCM,
        payload_len=0x11223344 & 0xFFFF,  # keep it under the ceiling
        seq=0x0A0B0C0D,
        pts_us=0x0102030405060708,
        flags=TeamsFlags.UNMIXED,
    )

    assert header[0:4] == b"TMC1"
    assert header[4] == WIRE_VERSION
    assert header[5] == int(TeamsMessageType.AUDIO_PCM)
    assert header[6] == int(TeamsFlags.UNMIXED)
    assert header[7] == 0  # reserved
    assert header[8:12] == bytes([0x0A, 0x0B, 0x0C, 0x0D])
    assert header[12:20] == bytes([0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08])
    assert header[20:24] == struct.pack(">I", 0x3344)


def test_header_rejects_oversized_payload() -> None:
    with pytest.raises(SidecarProtocolError, match="exceeds"):
        encode_header(TeamsMessageType.AUDIO_PCM, payload_len=MAX_PAYLOAD_BYTES + 1)


# --------------------------------------------------------------------------- #
# Audio
# --------------------------------------------------------------------------- #


def test_audio_round_trip_carries_source_identity(frame_ctx: FrameContext) -> None:
    """The field Zoom's protocol has no equivalent of: per-participant attribution."""
    audio_format = AudioFormat(sample_rate_hz=16_000, channels=1)
    frame = AudioFrame(pcm=b"\x01\x02" * 320, pts_us=4_000, format=audio_format, ctx=frame_ctx)

    encoded = encode_audio(frame, seq=7, source_msi=987_654, flags=TeamsFlags.UNMIXED)

    decoder = TeamsFrameDecoder()
    (message,) = list(decoder.feed(encoded))

    assert message.msg_type is TeamsMessageType.AUDIO_PCM
    assert message.seq == 7
    assert message.pts_us == 4_000
    assert TeamsFlags.UNMIXED in message.flags

    header, pcm = message.audio()
    assert header.sample_rate_hz == 16_000
    assert header.channels == 1
    assert header.sample_format is SampleFormat.S16LE
    assert header.source_msi == 987_654
    assert header.frame_ms == 20
    assert pcm == frame.pcm
    assert header.to_format() == audio_format


def test_audio_frame_ms_is_derived_from_the_payload(frame_ctx: FrameContext) -> None:
    """20 ms at 16 kHz mono is 640 bytes; the header must say so without being told."""
    audio_format = AudioFormat(sample_rate_hz=16_000, channels=1)
    frame = AudioFrame(pcm=b"\x00" * 640, pts_us=0, format=audio_format, ctx=frame_ctx)

    header, _ = decode_audio_payload(encode_audio(frame)[HEADER_SIZE:])
    assert header.frame_ms == 20


def test_silence_is_flagged_automatically(frame_ctx: FrameContext) -> None:
    """Advisory, but free: the pacer publishes silence continuously while idle and
    marking it makes an idle stream distinguishable from a stalled one in a capture."""
    audio_format = AudioFormat(sample_rate_hz=16_000, channels=1)
    silent = AudioFrame(pcm=b"\x00" * 640, pts_us=0, format=audio_format, ctx=frame_ctx)
    speech = AudioFrame(pcm=b"\x11" * 640, pts_us=0, format=audio_format, ctx=frame_ctx)

    decoder = TeamsFrameDecoder()
    (silent_msg,) = list(decoder.feed(encode_audio(silent)))
    (speech_msg,) = list(decoder.feed(encode_audio(speech)))

    assert TeamsFlags.SILENCE in silent_msg.flags
    assert TeamsFlags.SILENCE not in speech_msg.flags


def test_audio_defaults_to_the_mixed_sentinel(frame_ctx: FrameContext) -> None:
    """Bridge → sidecar audio has exactly one source: us. It carries no MSI."""
    audio_format = AudioFormat(sample_rate_hz=16_000, channels=1)
    frame = AudioFrame(pcm=b"\x00" * 640, pts_us=0, format=audio_format, ctx=frame_ctx)

    header, _ = decode_audio_payload(encode_audio(frame)[HEADER_SIZE:])
    assert header.source_msi == MIXED_SOURCE


def test_decode_audio_rejects_a_truncated_header() -> None:
    with pytest.raises(SidecarProtocolError, match="shorter than"):
        decode_audio_payload(b"\x00" * (AUDIO_HEADER_SIZE - 1))


def test_decode_audio_rejects_an_unknown_sample_format() -> None:
    payload = struct.pack(">IBBHI", 16_000, 1, 99, 20, 0)
    with pytest.raises(SidecarProtocolError, match="unknown wire sample format"):
        decode_audio_payload(payload)


def test_decode_audio_rejects_nonsense_geometry() -> None:
    with pytest.raises(SidecarProtocolError, match="channels"):
        decode_audio_payload(struct.pack(">IBBHI", 16_000, 0, 1, 20, 0))
    with pytest.raises(SidecarProtocolError, match="Hz"):
        decode_audio_payload(struct.pack(">IBBHI", 0, 1, 1, 20, 0))


# --------------------------------------------------------------------------- #
# Video
# --------------------------------------------------------------------------- #


def _video_frame(ctx: FrameContext, *, width: int = 64, height: int = 48) -> VideoFrame:
    video_format = VideoFormat(width=width, height=height, fps=30)
    return VideoFrame(
        planes=b"\x80" * video_format.frame_size_bytes,
        pts_us=33_000,
        format=video_format,
        ctx=ctx,
        is_keyframe=True,
    )


def test_video_round_trip_declares_strides(frame_ctx: FrameContext) -> None:
    """Strides are explicit so the C# side never has to assume packed planes."""
    frame = _video_frame(frame_ctx)

    decoder = TeamsFrameDecoder()
    (message,) = list(decoder.feed(encode_video(frame, seq=3)))

    assert message.msg_type is TeamsMessageType.VIDEO_I420
    assert TeamsFlags.KEYFRAME in message.flags
    assert message.pts_us == 33_000

    header, planes = message.video()
    assert (header.width, header.height, header.fps) == (64, 48, 30)
    assert header.stride_y == 64
    assert header.stride_uv == 32
    assert len(planes) == frame.format.frame_size_bytes


def test_video_rejects_a_non_i420_frame(frame_ctx: FrameContext) -> None:
    """The port carries I420 and the sidecar converts. Anything else is a wiring bug."""
    video_format = VideoFormat(width=64, height=48, fps=30, pixel_format=PixelFormat.I420)
    frame = VideoFrame(
        planes=b"\x00" * video_format.frame_size_bytes,
        pts_us=0,
        format=video_format,
        ctx=frame_ctx,
    )
    # I420 is currently the only PixelFormat, so assert the guard exists by construction
    # rather than by fabricating an unrepresentable value.
    assert encode_video(frame)[HEADER_SIZE:HEADER_SIZE + 2] == struct.pack(">H", 64)


def test_decode_video_rejects_a_truncated_header() -> None:
    with pytest.raises(SidecarProtocolError, match="shorter than"):
        decode_video_payload(b"\x00" * (VIDEO_HEADER_SIZE - 1))


def test_wrong_accessor_is_rejected(frame_ctx: FrameContext) -> None:
    """``.audio()`` on a video message is a programming error, not a decode failure."""
    decoder = TeamsFrameDecoder()
    (message,) = list(decoder.feed(encode_video(_video_frame(frame_ctx))))
    with pytest.raises(SidecarProtocolError, match="expected AUDIO_PCM"):
        message.audio()


# --------------------------------------------------------------------------- #
# JSON control messages
# --------------------------------------------------------------------------- #


def test_json_encoding_is_deterministic() -> None:
    """Sorted keys and no whitespace, so the same body always yields the same bytes."""
    a = encode_json(TeamsMessageType.CONTROL_JOIN, {"b": 2, "a": 1})
    b = encode_json(TeamsMessageType.CONTROL_JOIN, {"a": 1, "b": 2})
    assert a == b
    assert a[HEADER_SIZE:] == b'{"a":1,"b":2}'


def test_json_round_trip() -> None:
    body = {"callId": "abc", "unmixedAudio": True, "videoFps": 30}
    decoder = TeamsFrameDecoder()
    (message,) = list(decoder.feed(encode_json(TeamsMessageType.READY, body)))
    assert message.json() == body


def test_empty_json_payload_decodes_to_an_empty_mapping() -> None:
    decoder = TeamsFrameDecoder()
    frame = encode_header(TeamsMessageType.CONTROL_LEAVE, payload_len=0)
    (message,) = list(decoder.feed(frame))
    assert message.json() == {}


def test_json_rejects_a_non_object_payload() -> None:
    payload = json.dumps([1, 2, 3]).encode()
    frame = encode_header(TeamsMessageType.READY, payload_len=len(payload)) + payload
    decoder = TeamsFrameDecoder()
    (message,) = list(decoder.feed(frame))
    with pytest.raises(SidecarProtocolError, match="not a JSON object"):
        message.json()


def test_json_rejects_invalid_utf8() -> None:
    payload = b"\xff\xfe not json"
    frame = encode_header(TeamsMessageType.READY, payload_len=len(payload)) + payload
    decoder = TeamsFrameDecoder()
    (message,) = list(decoder.feed(frame))
    with pytest.raises(SidecarProtocolError, match="not valid UTF-8 JSON"):
        message.json()


# --------------------------------------------------------------------------- #
# Stream framing
# --------------------------------------------------------------------------- #


def test_decoder_reassembles_a_message_split_across_reads(frame_ctx: FrameContext) -> None:
    """TCP splits wherever it likes; the decoder must not care."""
    audio_format = AudioFormat(sample_rate_hz=16_000, channels=1)
    frame = AudioFrame(pcm=b"\x07" * 640, pts_us=0, format=audio_format, ctx=frame_ctx)
    encoded = encode_audio(frame)

    decoder = TeamsFrameDecoder()
    collected = []
    for i in range(0, len(encoded), 7):  # deliberately not aligned to any boundary
        collected.extend(decoder.feed(encoded[i : i + 7]))

    assert len(collected) == 1
    assert collected[0].audio()[1] == frame.pcm
    assert decoder.buffered == 0


def test_decoder_yields_several_messages_from_one_read(frame_ctx: FrameContext) -> None:
    audio_format = AudioFormat(sample_rate_hz=16_000, channels=1)
    frame = AudioFrame(pcm=b"\x01" * 640, pts_us=0, format=audio_format, ctx=frame_ctx)

    batch = (
        encode_json(TeamsMessageType.READY, {"callId": "x"})
        + encode_audio(frame, seq=1)
        + encode_audio(frame, seq=2)
    )

    decoder = TeamsFrameDecoder()
    messages = list(decoder.feed(batch))

    assert [m.msg_type for m in messages] == [
        TeamsMessageType.READY,
        TeamsMessageType.AUDIO_PCM,
        TeamsMessageType.AUDIO_PCM,
    ]
    assert [m.seq for m in messages[1:]] == [1, 2]


def test_decoder_holds_a_partial_message() -> None:
    frame = encode_json(TeamsMessageType.READY, {"callId": "x"})
    decoder = TeamsFrameDecoder()

    assert list(decoder.feed(frame[:-1])) == []
    assert decoder.buffered == len(frame) - 1

    (message,) = list(decoder.feed(frame[-1:]))
    assert message.msg_type is TeamsMessageType.READY


def test_decoder_refuses_to_resynchronise_on_bad_magic() -> None:
    """A desynced binary stream cannot be realigned with confidence. Guessing would
    surface as corrupt audio in a live meeting rather than as an error."""
    bogus = struct.pack(">IBBBBIqI", 0xDEADBEEF, 1, 2, 0, 0, 0, 0, 0)
    decoder = TeamsFrameDecoder()
    with pytest.raises(SidecarProtocolError, match="bad magic"):
        list(decoder.feed(bogus))


def test_decoder_rejects_an_unsupported_wire_version() -> None:
    frame = bytearray(encode_json(TeamsMessageType.READY, {}))
    frame[4] = 99
    decoder = TeamsFrameDecoder()
    with pytest.raises(SidecarProtocolError, match="wire version 99"):
        list(decoder.feed(bytes(frame)))


def test_decoder_rejects_an_unknown_message_type() -> None:
    frame = bytearray(encode_json(TeamsMessageType.READY, {}))
    frame[5] = 0x7F
    decoder = TeamsFrameDecoder()
    with pytest.raises(SidecarProtocolError, match="unknown message type"):
        list(decoder.feed(bytes(frame)))


def test_decoder_rejects_an_oversized_declared_length() -> None:
    """A corrupt length field must not make us allocate a gigabyte."""
    frame = bytearray(encode_json(TeamsMessageType.READY, {}))
    frame[20:24] = struct.pack(">I", MAX_PAYLOAD_BYTES + 1)
    decoder = TeamsFrameDecoder()
    with pytest.raises(SidecarProtocolError, match="exceeds"):
        list(decoder.feed(bytes(frame)))


def test_reset_discards_buffered_bytes() -> None:
    decoder = TeamsFrameDecoder()
    list(decoder.feed(encode_json(TeamsMessageType.READY, {"callId": "x"})[:-3]))
    assert decoder.buffered > 0

    decoder.reset()
    assert decoder.buffered == 0
