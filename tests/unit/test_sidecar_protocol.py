"""Conformance tests for the FROZEN sidecar IPC protocol.

These are the contract the C++ sidecar must satisfy. The byte-level vectors are what
make ``docs/design/004-sidecar-ipc-protocol.md`` a specification rather than a
description — if this file and that document disagree, one of them is a bug.
"""

from __future__ import annotations

import json

import pytest

from src.connectors.zoom.publisher.protocol import (
    AUDIO_HEADER_SIZE,
    HEADER_SIZE,
    HEADER_STRUCT,
    MAGIC,
    MAX_PAYLOAD_BYTES,
    VIDEO_HEADER_SIZE,
    WIRE_VERSION,
    SidecarFlags,
    SidecarFrameDecoder,
    SidecarMessage,
    SidecarMessageType,
    SidecarProtocolError,
    decode_audio_payload,
    decode_video_payload,
    encode,
    encode_audio,
    encode_json,
    encode_video,
)
from src.domain.context import FrameContext
from src.domain.media import AudioFormat, AudioFrame, VideoFormat, VideoFrame


class TestFrozenConstants:
    """If any of these change, the wire version must change with them."""

    def test_magic_is_zmc1(self) -> None:
        assert MAGIC == 0x5A4D4331
        assert MAGIC.to_bytes(4, "big") == b"ZMC1"

    def test_wire_version_is_one(self) -> None:
        assert WIRE_VERSION == 1

    def test_header_sizes(self) -> None:
        assert HEADER_SIZE == 24
        assert VIDEO_HEADER_SIZE == 12
        assert AUDIO_HEADER_SIZE == 8

    def test_message_type_values(self) -> None:
        assert SidecarMessageType.VIDEO_I420 == 0x01
        assert SidecarMessageType.AUDIO_PCM == 0x02
        assert SidecarMessageType.CONTROL_JOIN == 0x03
        assert SidecarMessageType.CONTROL_LEAVE == 0x04
        assert SidecarMessageType.HEARTBEAT == 0x05
        assert SidecarMessageType.READY == 0x06
        assert SidecarMessageType.ERROR == 0x07

    def test_flag_values(self) -> None:
        assert SidecarFlags.KEYFRAME == 0x01
        assert SidecarFlags.IDLE == 0x02
        assert SidecarFlags.END_OF_STREAM == 0x04

    def test_reference_test_vector(self) -> None:
        """The exact 26 bytes documented in spec §7."""
        expected = bytes.fromhex(
            "5A4D4331"  # magic "ZMC1"
            "01"  # version
            "03"  # CONTROL_JOIN
            "00"  # flags
            "00"  # reserved
            "00000000"  # seq
            "0000000000000000"  # pts_us
            "00000002"  # length
            "7B7D"  # "{}"
        )
        actual = encode(SidecarMessage(msg_type=SidecarMessageType.CONTROL_JOIN, payload=b"{}"))
        assert actual == expected
        assert len(actual) == 26

    def test_header_is_big_endian(self) -> None:
        """Network order, so Python and C++ cannot disagree on host endianness."""
        raw = encode(SidecarMessage(msg_type=SidecarMessageType.HEARTBEAT, seq=1, pts_us=2))
        magic, version, msg_type, _flags, _res, seq, pts, length = HEADER_STRUCT.unpack_from(raw)
        assert (magic, version, msg_type, seq, pts, length) == (
            MAGIC,
            WIRE_VERSION,
            int(SidecarMessageType.HEARTBEAT),
            1,
            2,
            0,
        )
        assert raw[:4] == b"ZMC1"


class TestEncodeGuards:
    def test_rejects_oversized_payload(self) -> None:
        with pytest.raises(SidecarProtocolError, match="cap"):
            encode(
                SidecarMessage(
                    msg_type=SidecarMessageType.AUDIO_PCM,
                    payload=b"\x00" * (MAX_PAYLOAD_BYTES + 1),
                )
            )

    def test_rejects_out_of_range_seq(self) -> None:
        with pytest.raises(SidecarProtocolError, match="u32"):
            encode(SidecarMessage(msg_type=SidecarMessageType.HEARTBEAT, seq=2**32))

    def test_encode_json_rejects_media_types(self) -> None:
        with pytest.raises(SidecarProtocolError, match="media type"):
            encode_json(SidecarMessageType.VIDEO_I420, {})


class TestMediaEncoding:
    def test_video_roundtrip(self, frame_ctx: FrameContext) -> None:
        fmt = VideoFormat(64, 48, 25)
        frame = VideoFrame(
            planes=bytes(range(256)) * (fmt.frame_size_bytes // 256),
            pts_us=123_456,
            format=fmt,
            ctx=frame_ctx,
            is_keyframe=True,
        )
        decoder = SidecarFrameDecoder()
        (message,) = list(decoder.feed(encode_video(frame, seq=7)))

        assert message.msg_type is SidecarMessageType.VIDEO_I420
        assert message.seq == 7
        assert message.pts_us == 123_456
        assert SidecarFlags.KEYFRAME in message.flags

        payload = decode_video_payload(message.payload)
        assert (payload.width, payload.height) == (64, 48)
        assert payload.planes == frame.planes
        assert payload.is_packed

    def test_video_geometry_travels_per_frame(self, frame_ctx: FrameContext) -> None:
        """A mid-session resolution change must not desync the sidecar."""
        sizes = [(64, 48), (128, 96)]
        decoder = SidecarFrameDecoder()
        stream = b"".join(
            encode_video(
                VideoFrame(
                    planes=b"\x00" * VideoFormat(w, h, 25).frame_size_bytes,
                    pts_us=i * 40_000,
                    format=VideoFormat(w, h, 25),
                    ctx=frame_ctx,
                ),
                seq=i,
            )
            for i, (w, h) in enumerate(sizes)
        )
        decoded = [decode_video_payload(m.payload) for m in decoder.feed(stream)]
        assert [(p.width, p.height) for p in decoded] == sizes

    def test_idle_flag_is_set(self, frame_ctx: FrameContext) -> None:
        """Idle frames are distinguishable so idle publishing can be metered."""
        fmt = VideoFormat(64, 48, 25)
        frame = VideoFrame(
            planes=b"\x00" * fmt.frame_size_bytes, pts_us=0, format=fmt, ctx=frame_ctx
        )
        decoder = SidecarFrameDecoder()
        (message,) = list(decoder.feed(encode_video(frame, seq=0, idle=True)))
        assert SidecarFlags.IDLE in message.flags
        assert SidecarFlags.KEYFRAME not in message.flags

    def test_audio_roundtrip(self, frame_ctx: FrameContext) -> None:
        fmt = AudioFormat(32_000, 1)
        frame = AudioFrame(pcm=b"\x01\x02" * 320, pts_us=999, format=fmt, ctx=frame_ctx)
        decoder = SidecarFrameDecoder()
        (message,) = list(decoder.feed(encode_audio(frame, seq=3)))

        payload = decode_audio_payload(message.payload)
        assert payload.sample_rate_hz == 32_000
        assert payload.channels == 1
        assert payload.pcm == frame.pcm
        assert message.pts_us == 999

    def test_audio_format_is_explicit_not_assumed(self, frame_ctx: FrameContext) -> None:
        """The publish sample rate is config until M5; a silent mismatch would
        produce pitch-shifted audio rather than an error."""
        for rate in (16_000, 32_000, 48_000):
            frame = AudioFrame(
                pcm=b"\x00" * 64, pts_us=0, format=AudioFormat(rate, 1), ctx=frame_ctx
            )
            decoder = SidecarFrameDecoder()
            (message,) = list(decoder.feed(encode_audio(frame, seq=0)))
            assert decode_audio_payload(message.payload).sample_rate_hz == rate

    def test_truncated_video_payload_is_rejected(self) -> None:
        with pytest.raises(SidecarProtocolError, match="sub-header"):
            decode_video_payload(b"\x00" * 4)

    def test_wrong_plane_length_is_rejected(self) -> None:
        header = (64).to_bytes(2, "big") + (48).to_bytes(2, "big") + b"\x00" * 8
        with pytest.raises(SidecarProtocolError, match="planes"):
            decode_video_payload(header + b"\x00" * 10)

    def test_unknown_sample_format_is_rejected(self) -> None:
        payload = (32_000).to_bytes(4, "big") + bytes([1, 99, 0, 0])
        with pytest.raises(SidecarProtocolError, match="sample format"):
            decode_audio_payload(payload)


class TestControlMessages:
    def test_join_carries_identity(self, frame_ctx: FrameContext) -> None:
        """Session identity is bound once at join and echoed thereafter, rather
        than repeated on every frame (spec §5.3)."""
        body = {
            "session_id": frame_ctx.session_id,
            "correlation_id": frame_ctx.correlation_id,
            "meeting_number": "1234567890",
            "display_name": "Avatar",
            "sdk_jwt": "eyJ...",
        }
        decoder = SidecarFrameDecoder()
        (message,) = list(decoder.feed(encode_json(SidecarMessageType.CONTROL_JOIN, body)))
        assert message.json() == body

    def test_ready_reports_license_and_participant(self) -> None:
        body = {"has_raw_data_license": True, "participant_id": 16778240}
        decoder = SidecarFrameDecoder()
        (message,) = list(decoder.feed(encode_json(SidecarMessageType.READY, body)))
        parsed = message.json()
        assert parsed["has_raw_data_license"] is True
        assert parsed["participant_id"] == 16778240

    def test_error_carries_fatal_flag(self) -> None:
        body = {"code": "JOIN_FAILED", "message": "rejected", "fatal": True}
        decoder = SidecarFrameDecoder()
        (message,) = list(decoder.feed(encode_json(SidecarMessageType.ERROR, body)))
        assert message.json()["fatal"] is True

    def test_empty_json_payload_parses_as_empty_dict(self) -> None:
        message = SidecarMessage(msg_type=SidecarMessageType.HEARTBEAT, payload=b"")
        assert message.json() == {}

    def test_json_on_media_message_is_an_error(self) -> None:
        message = SidecarMessage(msg_type=SidecarMessageType.AUDIO_PCM, payload=b"\x00")
        with pytest.raises(SidecarProtocolError, match="binary"):
            message.json()

    def test_malformed_json_is_rejected(self) -> None:
        message = SidecarMessage(msg_type=SidecarMessageType.READY, payload=b"{not json")
        with pytest.raises(SidecarProtocolError, match="valid JSON"):
            message.json()

    def test_non_object_json_is_rejected(self) -> None:
        message = SidecarMessage(
            msg_type=SidecarMessageType.READY, payload=json.dumps([1, 2]).encode()
        )
        with pytest.raises(SidecarProtocolError, match="JSON object"):
            message.json()


class TestIncrementalDecoding:
    """A SOCK_STREAM delivers arbitrary fragments; the decoder must not care."""

    def test_byte_at_a_time(self) -> None:
        stream = encode_json(SidecarMessageType.HEARTBEAT, {"sent_at_us": 42})
        decoder = SidecarFrameDecoder()
        messages = [m for byte in stream for m in decoder.feed(bytes([byte]))]
        assert len(messages) == 1
        assert messages[0].json() == {"sent_at_us": 42}
        assert decoder.pending_bytes == 0

    def test_multiple_messages_in_one_chunk(self) -> None:
        stream = b"".join(
            encode_json(SidecarMessageType.HEARTBEAT, {"sent_at_us": i}) for i in range(5)
        )
        decoder = SidecarFrameDecoder()
        messages = list(decoder.feed(stream))
        assert [m.json()["sent_at_us"] for m in messages] == list(range(5))

    def test_partial_header_is_retained(self) -> None:
        stream = encode_json(SidecarMessageType.HEARTBEAT, {"a": 1})
        decoder = SidecarFrameDecoder()
        assert list(decoder.feed(stream[:10])) == []
        assert decoder.pending_bytes == 10
        assert len(list(decoder.feed(stream[10:]))) == 1

    def test_partial_payload_is_retained(self) -> None:
        stream = encode_json(SidecarMessageType.READY, {"key": "value" * 20})
        decoder = SidecarFrameDecoder()
        assert list(decoder.feed(stream[: HEADER_SIZE + 5])) == []
        assert len(list(decoder.feed(stream[HEADER_SIZE + 5 :]))) == 1

    def test_split_across_message_boundary(self) -> None:
        a = encode_json(SidecarMessageType.HEARTBEAT, {"n": 1})
        b = encode_json(SidecarMessageType.HEARTBEAT, {"n": 2})
        decoder = SidecarFrameDecoder()
        first = list(decoder.feed(a + b[:6]))
        assert [m.json()["n"] for m in first] == [1]
        second = list(decoder.feed(b[6:]))
        assert [m.json()["n"] for m in second] == [2]

    def test_zero_length_payload(self) -> None:
        decoder = SidecarFrameDecoder()
        (message,) = list(decoder.feed(encode(SidecarMessage(SidecarMessageType.HEARTBEAT))))
        assert message.payload == b""

    def test_reset_discards_buffer(self) -> None:
        decoder = SidecarFrameDecoder()
        list(decoder.feed(b"\x5a\x4d"))
        assert decoder.pending_bytes == 2
        decoder.reset()
        assert decoder.pending_bytes == 0


class TestFramingFailures:
    """Desync is fatal by design — a heuristic resync would publish garbage while
    reporting success (spec §6)."""

    def test_bad_magic_raises(self) -> None:
        bad = b"XXXX" + encode(SidecarMessage(SidecarMessageType.HEARTBEAT))[4:]
        decoder = SidecarFrameDecoder()
        with pytest.raises(SidecarProtocolError, match="desync"):
            list(decoder.feed(bad))

    def test_unsupported_version_raises(self) -> None:
        raw = bytearray(encode(SidecarMessage(SidecarMessageType.HEARTBEAT)))
        raw[4] = 99
        decoder = SidecarFrameDecoder()
        with pytest.raises(SidecarProtocolError, match="wire version"):
            list(decoder.feed(bytes(raw)))

    def test_unknown_message_type_raises(self) -> None:
        raw = bytearray(encode(SidecarMessage(SidecarMessageType.HEARTBEAT)))
        raw[5] = 0x7E
        decoder = SidecarFrameDecoder()
        with pytest.raises(SidecarProtocolError, match="unknown message type"):
            list(decoder.feed(bytes(raw)))

    def test_oversized_declared_length_raises_before_allocating(self) -> None:
        raw = bytearray(encode(SidecarMessage(SidecarMessageType.HEARTBEAT)))
        raw[20:24] = (MAX_PAYLOAD_BYTES + 1).to_bytes(4, "big")
        decoder = SidecarFrameDecoder()
        with pytest.raises(SidecarProtocolError, match="exceeds"):
            list(decoder.feed(bytes(raw)))

    def test_unknown_flag_bits_are_preserved_not_rejected(self) -> None:
        """Forward compatibility: a newer peer setting a reserved bit stays
        interoperable (spec §4)."""
        raw = bytearray(encode(SidecarMessage(SidecarMessageType.HEARTBEAT)))
        raw[6] = 0x80
        decoder = SidecarFrameDecoder()
        (message,) = list(decoder.feed(bytes(raw)))
        assert int(message.flags) == 0x80
