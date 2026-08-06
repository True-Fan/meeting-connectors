"""The page bridge wire codec.

The conformance vector is the point of this file. ``js/bridge.js`` implements the same
header independently, and a mismatch between the two would be invisible until a live
meeting produced silence or a sheared frame — so the bytes are pinned as literals here,
exactly as ``test_teams_sidecar_protocol.py`` pins Teams'.
"""

from __future__ import annotations

import struct

import pytest

from src.connectors.google_meet.exceptions import BridgeProtocolError
from src.connectors.google_meet.websocket.protocol import (
    AUDIO_HEADER_SIZE,
    HEADER_SIZE,
    MAGIC,
    MAX_PAYLOAD_BYTES,
    MIXED_SOURCE,
    VIDEO_HEADER_SIZE,
    WIRE_VERSION,
    MeetFlags,
    MeetMessageType,
    MeetState,
    decode,
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


def _audio(ctx: FrameContext, *, pcm: bytes | None = None, pts_us: int = 0) -> AudioFrame:
    fmt = AudioFormat(sample_rate_hz=48_000, channels=1, sample_format=SampleFormat.S16LE)
    payload = pcm if pcm is not None else b"\x01\x02" * 480
    return AudioFrame(pcm=payload, pts_us=pts_us, format=fmt, ctx=ctx)


def _video(ctx: FrameContext, *, width: int = 4, height: int = 4, pts_us: int = 0) -> VideoFrame:
    fmt = VideoFormat(width=width, height=height, fps=25, pixel_format=PixelFormat.I420)
    return VideoFrame(planes=b"\x10" * fmt.frame_size_bytes, pts_us=pts_us, format=fmt, ctx=ctx)


# --------------------------------------------------------------------------- #
# Conformance
# --------------------------------------------------------------------------- #


class TestConformanceVector:
    """Pinned bytes. ``js/bridge.js`` must produce exactly these."""

    def test_header_layout_is_frozen(self) -> None:
        header = encode_header(
            MeetMessageType.AUDIO_PCM,
            payload_len=12,
            seq=0x01020304,
            pts_us=0x1122334455667788,
            flags=MeetFlags.MIXED,
        )
        assert header == bytes.fromhex(
            "474d4331"  # magic 'GMC1'
            "01"  # wire version
            "02"  # AUDIO_PCM
            "04"  # MeetFlags.MIXED
            "00"  # reserved
            "01020304"  # seq
            "1122334455667788"  # pts_us, signed 64-bit big-endian
            "0000000c"  # payload length
        )
        assert len(header) == HEADER_SIZE == 24

    def test_magic_spells_gmc1_and_differs_from_the_other_connectors(self) -> None:
        """A bridge pointed at the wrong peer must fail on the first frame, not decode it."""
        assert MAGIC.to_bytes(4, "big") == b"GMC1"

        from src.connectors.teams.sidecar.protocol import MAGIC as TEAMS_MAGIC

        assert MAGIC != TEAMS_MAGIC

    def test_sub_header_sizes_are_frozen(self) -> None:
        assert AUDIO_HEADER_SIZE == 12
        assert VIDEO_HEADER_SIZE == 12

    def test_json_encoding_is_deterministic(self) -> None:
        """Pinned separators and key order are what make a literal vector possible."""
        first = encode_json(MeetMessageType.HELLO, {"b": 2, "a": 1})
        second = encode_json(MeetMessageType.HELLO, {"a": 1, "b": 2})
        assert first == second
        assert first[HEADER_SIZE:] == b'{"a":1,"b":2}'


# --------------------------------------------------------------------------- #
# Round trips
# --------------------------------------------------------------------------- #


class TestRoundTrip:
    def test_audio(self, frame_ctx: FrameContext) -> None:
        frame = _audio(frame_ctx, pts_us=987_654)
        message = decode(encode_audio(frame, seq=7))

        assert message.msg_type is MeetMessageType.AUDIO_PCM
        assert message.seq == 7
        assert message.pts_us == 987_654

        header, pcm = message.audio()
        assert pcm == frame.pcm
        assert header.to_format() == frame.format
        assert header.frame_ms == 10  # 480 samples at 48 kHz
        assert header.source_id == MIXED_SOURCE

    def test_video_carries_explicit_strides(self, frame_ctx: FrameContext) -> None:
        """An inferred layout that is wrong shears the image, so the strides are on the wire."""
        frame = _video(frame_ctx, width=8, height=6)
        header, planes = decode(encode_video(frame, seq=3)).video()

        assert (header.width, header.height) == (8, 6)
        assert header.stride_y == 8
        assert header.stride_uv == 4
        assert header.fps == 25
        assert len(planes) == frame.format.frame_size_bytes

    def test_silence_is_flagged(self, frame_ctx: FrameContext) -> None:
        silent = _audio(frame_ctx, pcm=b"\x00" * 960)
        assert MeetFlags.SILENCE in decode(encode_audio(silent)).flags

    def test_keyframe_is_flagged(self, frame_ctx: FrameContext) -> None:
        fmt = VideoFormat(width=4, height=4, fps=25)
        frame = VideoFrame(
            planes=b"\x00" * fmt.frame_size_bytes,
            pts_us=0,
            format=fmt,
            ctx=frame_ctx,
            is_keyframe=True,
        )
        assert MeetFlags.KEYFRAME in decode(encode_video(frame)).flags

    def test_json_round_trip(self) -> None:
        body = {"state": "joined", "url": "https://meet.google.com/abc-defg-hij"}
        assert decode(encode_json(MeetMessageType.MEET_STATE, body)).json() == body

    def test_empty_json_payload_decodes_to_an_empty_dict(self) -> None:
        message = decode(encode_header(MeetMessageType.READY, payload_len=0))
        assert message.json() == {}


# --------------------------------------------------------------------------- #
# Rejection
# --------------------------------------------------------------------------- #


class TestRejection:
    def test_short_message(self) -> None:
        with pytest.raises(BridgeProtocolError, match="shorter than"):
            decode(b"\x00" * 8)

    def test_bad_magic_names_the_real_problem(self) -> None:
        payload = bytearray(encode_json(MeetMessageType.HELLO, {}))
        payload[0:4] = b"TMC1"
        with pytest.raises(BridgeProtocolError, match="not the Google Meet page bridge"):
            decode(bytes(payload))

    def test_wrong_wire_version_points_at_the_asset(self) -> None:
        payload = bytearray(encode_json(MeetMessageType.HELLO, {}))
        payload[4] = WIRE_VERSION + 1
        with pytest.raises(BridgeProtocolError, match=r"mismatched js/bridge\.js"):
            decode(bytes(payload))

    def test_unknown_message_type(self) -> None:
        payload = bytearray(encode_json(MeetMessageType.HELLO, {}))
        payload[5] = 0x7F
        with pytest.raises(BridgeProtocolError, match=r"unknown message type 0x7F"):
            decode(bytes(payload))

    def test_length_disagreement_is_a_version_skew_not_a_short_read(self) -> None:
        """WebSocket delivers whole frames, so a length mismatch means the sender is wrong.

        Truncating past it would silently hand the pipeline a partial frame; the codec says
        so instead.
        """
        payload = bytearray(encode_json(MeetMessageType.HELLO, {"a": 1}))
        struct.pack_into(">I", payload, 20, 999)
        with pytest.raises(BridgeProtocolError, match="declares 999 payload bytes"):
            decode(bytes(payload))

    def test_oversized_payload_is_refused_before_allocation(self) -> None:
        with pytest.raises(BridgeProtocolError, match="exceeds"):
            encode_header(MeetMessageType.VIDEO_I420, payload_len=MAX_PAYLOAD_BYTES + 1)

    def test_truncated_audio_header(self) -> None:
        message = decode(encode_header(MeetMessageType.AUDIO_PCM, payload_len=0))
        with pytest.raises(BridgeProtocolError, match="shorter than its 12-byte header"):
            message.audio()

    def test_asking_audio_of_a_video_message(self, frame_ctx: FrameContext) -> None:
        message = decode(encode_video(_video(frame_ctx)))
        with pytest.raises(BridgeProtocolError, match="expected AUDIO_PCM"):
            message.audio()

    def test_non_json_control_payload(self) -> None:
        raw = encode_header(MeetMessageType.HELLO, payload_len=3) + b"\xff\xfe\xfd"
        with pytest.raises(BridgeProtocolError, match="not valid UTF-8 JSON"):
            decode(raw).json()

    def test_json_array_is_not_an_object(self) -> None:
        body = b"[1,2]"
        raw = encode_header(MeetMessageType.HELLO, payload_len=len(body)) + body
        with pytest.raises(BridgeProtocolError, match="not a JSON object"):
            decode(raw).json()


# --------------------------------------------------------------------------- #
# Meeting state
# --------------------------------------------------------------------------- #


class TestMeetState:
    @pytest.mark.parametrize(
        "state",
        [MeetState.DENIED, MeetState.EJECTED, MeetState.ENDED],
    )
    def test_terminal_states_are_fatal(self, state: MeetState) -> None:
        """Retrying a denial is what gets an automated Google account restricted."""
        assert state.is_fatal

    @pytest.mark.parametrize(
        "state",
        [MeetState.JOINING, MeetState.LOBBY, MeetState.JOINED, MeetState.LEFT],
    )
    def test_recoverable_states_are_not_fatal(self, state: MeetState) -> None:
        assert not state.is_fatal

    def test_the_lobby_is_not_a_failure(self) -> None:
        """Someone has to click Admit; that wait must not start a retry clock."""
        assert not MeetState.LOBBY.is_fatal
        assert not MeetState.LOBBY.is_in_call

    def test_only_joined_counts_as_being_in_the_call(self) -> None:
        assert MeetState.JOINED.is_in_call
        assert not MeetState.LOBBY.is_in_call
