"""Synthetic fragmented-MP4 builders.

Structurally valid box layouts for framing tests — enough for the framer to parse and
classify, without needing a real encoder. Where a genuinely decodable stream is required
(the ffmpeg tests), ffmpeg generates it instead.
"""

from __future__ import annotations

import struct


def box(box_type: bytes, payload: bytes = b"") -> bytes:
    """Build one MP4 box with a 32-bit size header."""
    return struct.pack(">I4s", 8 + len(payload), box_type) + payload


def box64(box_type: bytes, payload: bytes = b"") -> bytes:
    """Build one box using the 64-bit extended size form."""
    return struct.pack(">I4s", 1, box_type) + struct.pack(">Q", 16 + len(payload)) + payload


def init_segment(*, brand: bytes = b"iso5") -> bytes:
    """``ftyp`` + ``moov`` — the init segment a streaming decoder needs first."""
    return box(b"ftyp", brand + b"\x00\x00\x02\x00" + brand) + box(b"moov", b"\x00" * 64)


def fragment(index: int = 0, *, payload_size: int = 128) -> bytes:
    """``moof`` + ``mdat`` — one independently decodable media fragment."""
    return box(b"moof", struct.pack(">I", index) + b"\x00" * 28) + box(
        b"mdat", bytes((index + 1) % 256) * payload_size
    )


def stream(fragments: int = 3) -> bytes:
    """A complete init segment followed by ``fragments`` media fragments."""
    return init_segment() + b"".join(fragment(i) for i in range(fragments))


def non_fragmented_mp4() -> bytes:
    """A *plain* MP4: media boxes first, ``moov`` at the end.

    This is assumption A1's failure case (doc 003 §9). It cannot be decoded while
    streaming, and the framer must say so rather than waiting forever for a fragment.
    """
    return box(b"ftyp", b"isom" + b"\x00" * 8) + box(b"mdat", b"\x00" * 256) + box(
        b"moov", b"\x00" * 64
    )
