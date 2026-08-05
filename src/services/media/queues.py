"""Bounded frame queues with an explicit drop policy.

"Buffer minimally" (doc 003 §7.2) means buffers are small, bounded, and drop rather
than grow. Two rules make that safe:

* **Overflow is counted, never silent.** A silent drop is how a latency bug becomes
  unfalsifiable — you cannot tell a slow stage from a lossy one.
* **``put`` never blocks the producer.** The RTMS reader must never be stalled by a
  slow downstream stage; blocking there causes Zoom-side loss, which is worse than a
  drop we chose and recorded.
"""

from __future__ import annotations

import asyncio
from collections import deque
from enum import StrEnum

from src.domain.context import FrameContext
from src.infrastructure.metrics import MetricName, MetricsCollector


class OverflowPolicy(StrEnum):
    """What to do when a full queue receives another item."""

    DROP_OLDEST = "drop_oldest"
    """Prefer fresh media. Correct for realtime — a stale frame has no value."""

    DROP_NEWEST = "drop_newest"
    """Preserve an in-progress sequence, e.g. a decoder's fragment ordering."""


class BoundedFrameQueue[T]:
    """An asyncio queue with a fixed depth and a non-blocking producer."""

    __slots__ = (
        "_closed",
        "_deque",
        "_dropped",
        "_maxsize",
        "_metrics",
        "_name",
        "_not_empty",
        "_policy",
    )

    def __init__(
        self,
        *,
        name: str,
        maxsize: int,
        policy: OverflowPolicy = OverflowPolicy.DROP_OLDEST,
        metrics: MetricsCollector | None = None,
    ) -> None:
        if maxsize < 1:
            raise ValueError("maxsize must be positive")
        self._name = name
        self._maxsize = maxsize
        self._policy = policy
        self._metrics = metrics
        self._deque: deque[T] = deque()
        self._not_empty = asyncio.Event()
        self._dropped = 0
        self._closed = False

    @property
    def name(self) -> str:
        return self._name

    @property
    def maxsize(self) -> int:
        return self._maxsize

    @property
    def dropped(self) -> int:
        """Lifetime count of items discarded by the overflow policy."""
        return self._dropped

    def qsize(self) -> int:
        return len(self._deque)

    def is_full(self) -> bool:
        return len(self._deque) >= self._maxsize

    def put(self, item: T, *, ctx: FrameContext | None = None, reason: str = "overflow") -> bool:
        """Offer an item. Never blocks, never raises when full.

        Returns:
            True if the item was queued, False if the policy discarded something.
        """
        if self._closed:
            return False

        accepted = True
        if len(self._deque) >= self._maxsize:
            if self._policy is OverflowPolicy.DROP_OLDEST:
                self._deque.popleft()
                self._deque.append(item)
            else:
                accepted = False
            self._dropped += 1
            if self._metrics is not None:
                self._metrics.increment(
                    MetricName.FRAMES_DROPPED_TOTAL, ctx=ctx, stage=self._name, reason=reason
                )
        else:
            self._deque.append(item)

        if self._deque:
            self._not_empty.set()
        return accepted

    def get_nowait(self) -> T | None:
        """Remove and return the oldest item, or ``None`` when empty.

        The pacer needs this: it is already awake on a clock deadline and must decide
        between a queued frame and an idle frame *without* blocking, since blocking
        would mean skipping the deadline entirely.
        """
        if not self._deque:
            return None
        item = self._deque.popleft()
        if not self._deque:
            self._not_empty.clear()
        return item

    async def get(self) -> T:
        """Wait for and remove the oldest item.

        Raises:
            asyncio.CancelledError: propagated from the wait.
            QueueClosedError: the queue was closed while empty.
        """
        while True:
            if self._deque:
                item = self._deque.popleft()
                if not self._deque:
                    self._not_empty.clear()
                return item
            if self._closed:
                raise QueueClosedError(self._name)
            await self._not_empty.wait()

    def close(self) -> None:
        """Close the queue, waking any waiter. Idempotent."""
        self._closed = True
        self._not_empty.set()

    def clear(self) -> None:
        """Discard everything buffered without counting it as a policy drop.

        Used on reconnect, where buffered frames are stale by definition and
        replaying them would burst (doc 003 §7.3).
        """
        self._deque.clear()
        self._not_empty.clear()


class QueueClosedError(RuntimeError):
    """A closed, empty queue was read from."""

    def __init__(self, name: str) -> None:
        super().__init__(f"queue {name!r} is closed")
