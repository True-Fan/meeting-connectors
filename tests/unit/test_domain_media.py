"""Canonical media model."""

from __future__ import annotations

import pytest

from src.domain.context import FrameContext
from src.domain.exceptions import InvalidFrameError
from src.domain.media import (
    AudioFormat,
    AudioFrame,
    MediaChunk,
    VideoFormat,
    VideoFrame,
)


class TestAudioFormat:
    def test_bytes_per_frame(self) -> None:
        assert AudioFormat(16_000, 1).bytes_per_frame == 2
        assert AudioFormat(48_000, 2).bytes_per_frame == 4

    def test_bytes_for_duration_of_20ms_at_16k_mono(self) -> None:
        # 16000 * 0.02 = 320 samples * 2 bytes = 640 bytes — one RTMS frame.
        assert AudioFormat(16_000, 1).bytes_for_duration(20_000) == 640

    @pytest.mark.parametrize(("rate", "channels"), [(0, 1), (-1, 1), (16_000, 0)])
    def test_rejects_nonsense(self, rate: int, channels: int) -> None:
        with pytest.raises(InvalidFrameError):
            AudioFormat(rate, channels)

    def test_is_hashable_and_comparable(self) -> None:
        assert AudioFormat(16_000, 1) == AudioFormat(16_000, 1)
        assert len({AudioFormat(16_000, 1), AudioFormat(16_000, 1)}) == 1


class TestVideoFormat:
    def test_i420_frame_size(self) -> None:
        assert VideoFormat(1280, 720, 25).frame_size_bytes == 1280 * 720 * 3 // 2

    def test_frame_duration(self) -> None:
        assert VideoFormat(640, 480, 25).frame_duration_us == 40_000

    def test_rejects_odd_dimensions(self) -> None:
        # I420 subsamples chroma 2x2; odd dimensions have no valid plane layout.
        with pytest.raises(InvalidFrameError, match="even dimensions"):
            VideoFormat(1281, 720, 25)

    @pytest.mark.parametrize(("w", "h", "fps"), [(0, 720, 25), (1280, 0, 25), (1280, 720, 0)])
    def test_rejects_nonsense(self, w: int, h: int, fps: int) -> None:
        with pytest.raises(InvalidFrameError):
            VideoFormat(w, h, fps)


class TestAudioFrame:
    def test_derived_properties(self, frame_ctx: FrameContext) -> None:
        fmt = AudioFormat(16_000, 1)
        frame = AudioFrame(pcm=b"\x00" * 640, pts_us=0, format=fmt, ctx=frame_ctx)
        assert frame.sample_count == 320
        assert frame.duration_us == 20_000
        assert frame.is_silence

    def test_detects_non_silence(self, frame_ctx: FrameContext) -> None:
        frame = AudioFrame(
            pcm=b"\x00\x01" * 320, pts_us=0, format=AudioFormat(16_000, 1), ctx=frame_ctx
        )
        assert not frame.is_silence

    def test_rejects_partial_sample(self, frame_ctx: FrameContext) -> None:
        with pytest.raises(InvalidFrameError, match="whole number of samples"):
            AudioFrame(pcm=b"\x00" * 641, pts_us=0, format=AudioFormat(16_000, 1), ctx=frame_ctx)

    def test_rejects_negative_pts(self, frame_ctx: FrameContext) -> None:
        with pytest.raises(InvalidFrameError, match="pts_us"):
            AudioFrame(pcm=b"", pts_us=-1, format=AudioFormat(16_000, 1), ctx=frame_ctx)

    def test_carries_frame_context(self, frame_ctx: FrameContext) -> None:
        """Every frame carries session and correlation identity."""
        frame = AudioFrame(pcm=b"", pts_us=0, format=AudioFormat(16_000, 1), ctx=frame_ctx)
        assert frame.ctx.session_id == frame_ctx.session_id
        assert frame.ctx.correlation_id == frame_ctx.correlation_id

    def test_is_immutable(self, frame_ctx: FrameContext) -> None:
        frame = AudioFrame(pcm=b"", pts_us=0, format=AudioFormat(16_000, 1), ctx=frame_ctx)
        with pytest.raises((AttributeError, TypeError)):
            frame.pts_us = 5  # type: ignore[misc]


class TestVideoFrame:
    def test_accepts_correctly_sized_planes(self, frame_ctx: FrameContext) -> None:
        fmt = VideoFormat(64, 48, 25)
        frame = VideoFrame(
            planes=b"\x00" * fmt.frame_size_bytes, pts_us=1000, format=fmt, ctx=frame_ctx
        )
        assert len(frame.planes) == fmt.frame_size_bytes

    def test_rejects_wrong_plane_size(self, frame_ctx: FrameContext) -> None:
        fmt = VideoFormat(64, 48, 25)
        with pytest.raises(InvalidFrameError, match="planes length"):
            VideoFrame(planes=b"\x00" * 10, pts_us=0, format=fmt, ctx=frame_ctx)


class TestMediaChunk:
    def test_init_segment_flag_defaults_false(self, frame_ctx: FrameContext) -> None:
        chunk = MediaChunk(data=b"abc", seq=1, received_at_us=5, ctx=frame_ctx)
        assert not chunk.is_init_segment
        assert chunk.size_bytes == 3

    def test_init_segment_is_marked(self, frame_ctx: FrameContext) -> None:
        """The decoder must replay this on restart or video stays black forever."""
        chunk = MediaChunk(
            data=b"ftypmoov", seq=0, received_at_us=0, ctx=frame_ctx, is_init_segment=True
        )
        assert chunk.is_init_segment

    def test_rejects_negative_seq(self, frame_ctx: FrameContext) -> None:
        with pytest.raises(InvalidFrameError, match="seq"):
            MediaChunk(data=b"", seq=-1, received_at_us=0, ctx=frame_ctx)
