"""Fragmented-MP4 framing.

The avatar streams fMP4 over a WebSocket, and WebSocket message boundaries have no
relationship to MP4 box boundaries. This module re-establishes structure by parsing
top-level boxes and emitting two kinds of chunk:

* **the init segment** — ``ftyp`` … ``moov``, emitted once as ``seq 0``;
* **media fragments** — each ``[styp?] moof mdat`` group, one chunk per fragment.

Why bother, when ffmpeg would happily eat the undifferentiated byte stream? Because
of decoder restart. An fMP4 decoder cannot resume from a mid-stream ``moof`` — it
needs the init segment first. Identifying that segment is what makes restart produce
video instead of a permanently black frame (doc 003 §0.2). Fragment boundaries also
give "time to first fragment" a precise meaning for latency measurement.

A plain (non-fragmented) MP4 puts ``moov`` at the *end* of the file and cannot be
decoded while streaming at all. That case is detected and reported explicitly rather
than hanging forever waiting for a fragment — assumption A1 in doc 003 §9.
"""

from __future__ import annotations

import struct
from collections.abc import Iterator
from dataclasses import dataclass, field

from src.domain.context import FrameContext
from src.domain.media import MediaChunk

_HEADER = struct.Struct(">I4s")
_HEADER_SIZE = _HEADER.size  # 8
_EXT_SIZE = struct.Struct(">Q")

BOX_FTYP = b"ftyp"
BOX_MOOV = b"moov"
BOX_MOOF = b"moof"
BOX_MDAT = b"mdat"

_INIT_BOXES = frozenset({BOX_FTYP, BOX_MOOV, b"free", b"skip", b"styp", b"sidx"})

MAX_BOX_BYTES = 64 * 1024 * 1024
"""Bounds allocation from a corrupt size field."""


class Fmp4FramingError(Exception):
    """The byte stream is not usable fragmented MP4."""


@dataclass(frozen=True, slots=True)
class BoxHeader:
    """A parsed top-level box header."""

    size: int
    """Total box size including the header."""
    box_type: bytes
    header_size: int


def parse_box_header(buffer: bytes | bytearray, offset: int = 0) -> BoxHeader | None:
    """Parse a box header at ``offset``.

    Returns ``None`` when more bytes are needed.

    Raises:
        Fmp4FramingError: the header is structurally invalid.
    """
    if len(buffer) - offset < _HEADER_SIZE:
        return None

    size, box_type = _HEADER.unpack_from(buffer, offset)
    header_size = _HEADER_SIZE

    if size == 1:
        # 64-bit extended size follows the type.
        if len(buffer) - offset < _HEADER_SIZE + 8:
            return None
        (size,) = _EXT_SIZE.unpack_from(buffer, offset + _HEADER_SIZE)
        header_size = _HEADER_SIZE + 8
    elif size == 0:
        # "Extends to end of file" — meaningless in a live stream, and it would make
        # every subsequent byte unparseable.
        raise Fmp4FramingError(
            f"box {box_type!r} declares size 0 (to end of file), "
            "which cannot occur in a streamed fMP4"
        )

    if size < header_size:
        raise Fmp4FramingError(f"box {box_type!r} size {size} is smaller than its header")
    if size > MAX_BOX_BYTES:
        raise Fmp4FramingError(f"box {box_type!r} size {size} exceeds {MAX_BOX_BYTES} cap")

    return BoxHeader(size=size, box_type=box_type, header_size=header_size)


@dataclass(slots=True)
class Fmp4Framer:
    """Incremental fMP4 box framer.

    Usage::

        framer = Fmp4Framer(ctx=ctx)
        for chunk in framer.feed(ws_message):
            ...
    """

    ctx: FrameContext
    _buffer: bytearray = field(default_factory=bytearray)
    _init_done: bool = False
    _seen_ftyp: bool = False
    _seq: int = 0
    _boxes_seen: int = 0

    @property
    def init_segment_emitted(self) -> bool:
        return self._init_done

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    @property
    def boxes_seen(self) -> int:
        return self._boxes_seen

    def feed(self, data: bytes, *, received_at_us: int = 0) -> Iterator[MediaChunk]:
        """Consume bytes and yield every complete chunk.

        Raises:
            Fmp4FramingError: the stream is not usable fragmented MP4.
        """
        self._buffer.extend(data)

        while True:
            chunk = self._next_chunk(received_at_us)
            if chunk is None:
                return
            yield chunk

    def _next_chunk(self, received_at_us: int) -> MediaChunk | None:
        offset = 0
        saw_moov = False
        saw_mdat = False

        while True:
            header = parse_box_header(self._buffer, offset)
            if header is None:
                return None  # need more bytes for the header
            if len(self._buffer) - offset < header.size:
                return None  # need more bytes for the body

            box_type = header.box_type
            offset += header.size
            self._boxes_seen += 1

            if not self._init_done:
                if box_type == BOX_FTYP:
                    self._seen_ftyp = True
                if box_type == BOX_MOOV:
                    saw_moov = True
                    break
                if box_type in (BOX_MOOF, BOX_MDAT):
                    # A fragment before moov means the index has not arrived, and in
                    # a plain MP4 it never will — it lives at the end of the file.
                    raise Fmp4FramingError(
                        f"encountered {box_type.decode()} before moov: the stream is not "
                        "fragmented MP4 (needs -movflags +frag_keyframe+empty_moov). "
                        "A plain MP4 cannot be decoded while streaming."
                    )
                if box_type not in _INIT_BOXES:
                    raise Fmp4FramingError(
                        f"unexpected box {box_type.decode()!r} before moov"
                    )
                continue

            # Post-init: a fragment is complete once its mdat is complete.
            if box_type == BOX_MDAT:
                saw_mdat = True
                break

        if not (saw_moov or saw_mdat):  # pragma: no cover - loop only breaks on these
            return None

        payload = bytes(self._buffer[:offset])
        del self._buffer[:offset]

        is_init = saw_moov
        if is_init:
            if not self._seen_ftyp:
                raise Fmp4FramingError("moov arrived without a preceding ftyp")
            self._init_done = True

        chunk = MediaChunk(
            data=payload,
            seq=self._seq,
            received_at_us=received_at_us,
            ctx=self.ctx,
            is_init_segment=is_init,
        )
        self._seq += 1
        return chunk

    def reset(self) -> None:
        """Discard buffered bytes and restart framing.

        Called on avatar reconnect: a partial fragment from the old connection cannot
        be completed by the new one. ``seq`` continues rising so chunk identity stays
        unique across reconnects.
        """
        self._buffer.clear()
        self._init_done = False
        self._seen_ftyp = False
