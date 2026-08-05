"""Session media clock.

One monotonic time base per session. Every PTS in the system is expressed on it.

This exists because Zoom's send-audio and send-video are separate paths with a
documented desync risk (doc 001 §7.1). Publishing "as decoded" guarantees drift,
because decode *completion* time has nothing to do with *presentation* time. So the
clock is captured once at session start and every frame is rebased onto it — audio
and video paced from one timeline, not two.

Monotonic, not wall clock: NTP steps and DST must not move a presentation timeline.
"""

from __future__ import annotations

import time


class MediaClock:
    """A monotonic microsecond clock rooted at session start."""

    __slots__ = ("_origin_ns",)

    def __init__(self, *, origin_ns: int | None = None) -> None:
        self._origin_ns = time.monotonic_ns() if origin_ns is None else origin_ns

    @property
    def origin_ns(self) -> int:
        """The monotonic nanosecond reading this clock is rooted at."""
        return self._origin_ns

    def now_us(self) -> int:
        """Microseconds elapsed since session start."""
        return (time.monotonic_ns() - self._origin_ns) // 1_000

    def deadline_delay_s(self, pts_us: int) -> float:
        """Seconds to wait until ``pts_us`` arrives.

        Zero or negative when the deadline has already passed — the caller decides
        whether that means "send now" or "drop as late", because those differ
        between audio and video.
        """
        return (pts_us - self.now_us()) / 1_000_000.0

    def is_late(self, pts_us: int, *, tolerance_us: int = 0) -> bool:
        """True when ``pts_us`` is already in the past beyond ``tolerance_us``."""
        return self.now_us() - pts_us > tolerance_us
