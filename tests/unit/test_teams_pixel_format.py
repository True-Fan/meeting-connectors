"""I420 → NV12: the reference implementation the .NET sidecar must match.

**This is a specification, not a test of shipped Python code.** The conversion runs in C#
(``Media/SendBuffers.cs``) because the sidecar already has to copy every frame into
unmanaged memory for the media platform, so the interleave rides along on a copy we were
making anyway — whereas doing it in Python would put a per-frame 1.4 MB byte shuffle in the
bridge's event loop.

But "the C# is correct" then rests on an argument rather than on anything executable. So the
transform is specified here, in a form that runs on any machine, and the C# is written
against it. If the two ever disagree, the avatar's video comes out with its colours
swapped — a failure that looks like a decoder bug and is expensive to trace to a
Windows-side byte layout.

Both formats are 8-bit 4:2:0 and share an identical full-resolution Y plane. They differ
only in chroma layout:

    I420:  [Y w*h][U (w/2)*(h/2)][V (w/2)*(h/2)]     two separate quarter planes
    NV12:  [Y w*h][UVUVUV... (w/2)*(h/2)*2]          one interleaved half-height plane

So the conversion is a block copy plus one interleave pass. No resampling, no colour-space
maths, no quality loss, and the total size is unchanged.
"""

from __future__ import annotations

import pytest

from src.domain.media import VideoFormat


def i420_to_nv12(i420: bytes, width: int, height: int) -> bytes:
    """The reference transform. Mirrors ``PixelFormats.I420ToNv12`` in C#."""
    luma = width * height
    chroma_w, chroma_h = width // 2, height // 2
    chroma = chroma_w * chroma_h
    expected = luma + 2 * chroma

    if len(i420) != expected:
        raise ValueError(f"expected {expected} bytes for {width}x{height}, got {len(i420)}")

    y = i420[:luma]
    u = i420[luma : luma + chroma]
    v = i420[luma + chroma :]

    uv = bytearray(2 * chroma)
    uv[0::2] = u
    uv[1::2] = v

    return y + bytes(uv)


# --------------------------------------------------------------------------- #
# The specification
# --------------------------------------------------------------------------- #


def test_y_plane_is_copied_unchanged() -> None:
    width, height = 8, 4
    luma = bytes(range(width * height))
    chroma = width // 2 * (height // 2)
    i420 = luma + b"\xaa" * chroma + b"\xbb" * chroma

    nv12 = i420_to_nv12(i420, width, height)
    assert nv12[: width * height] == luma


def test_chroma_is_interleaved_u_first() -> None:
    """**The byte order that matters.** NV12 is U then V per pair. Swapping them is the
    classic mistake and produces a plausible-looking image with inverted colours, which is
    exactly why it is pinned here."""
    width, height = 4, 4
    luma = b"\x00" * (width * height)
    chroma = width // 2 * (height // 2)  # 4
    u = bytes([1, 2, 3, 4])
    v = bytes([9, 8, 7, 6])
    assert len(u) == len(v) == chroma

    nv12 = i420_to_nv12(luma + u + v, width, height)
    uv = nv12[width * height :]

    assert uv == bytes([1, 9, 2, 8, 3, 7, 4, 6])
    assert uv[0::2] == u
    assert uv[1::2] == v


def test_total_size_is_unchanged() -> None:
    """NV12 and I420 are the same number of bytes, so the sidecar's allocation is exact."""
    for width, height in ((320, 180), (640, 360), (1280, 720), (1920, 1080)):
        video_format = VideoFormat(width=width, height=height, fps=30)
        i420 = b"\x7f" * video_format.frame_size_bytes

        nv12 = i420_to_nv12(i420, width, height)

        assert len(nv12) == len(i420) == video_format.frame_size_bytes
        assert len(nv12) == width * height * 3 // 2


def test_every_teams_send_geometry_converts() -> None:
    from src.connectors.teams.config import SUPPORTED_SEND_VIDEO_FORMATS

    for width, height, fps in sorted(SUPPORTED_SEND_VIDEO_FORMATS):
        video_format = VideoFormat(width=width, height=height, fps=fps)
        nv12 = i420_to_nv12(b"\x40" * video_format.frame_size_bytes, width, height)
        assert len(nv12) == video_format.frame_size_bytes


def test_a_short_frame_is_rejected() -> None:
    """The C# raises here too rather than reading past the buffer."""
    with pytest.raises(ValueError, match="expected 24 bytes"):
        i420_to_nv12(b"\x00" * 10, 4, 4)


def test_round_trip_back_to_i420_recovers_the_original() -> None:
    """Proves the transform loses nothing — it is a permutation, not a conversion."""
    width, height = 8, 8
    luma = bytes(range(width * height))
    chroma = width // 2 * (height // 2)
    u = bytes(range(100, 100 + chroma))
    v = bytes(range(200, 200 + chroma))
    original = luma + u + v

    nv12 = i420_to_nv12(original, width, height)

    recovered_y = nv12[: width * height]
    uv = nv12[width * height :]
    recovered = recovered_y + uv[0::2] + uv[1::2]

    assert recovered == original
