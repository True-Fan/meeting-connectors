"""A scripted ``HandRaiseSource`` — the port's second implementation.

Lets the whole path from a hand going up to an interrupted avatar run with no browser, no
meeting and no DOM. ``MeetHandRaiseSource`` is the first implementation; this is what makes the
port earn its place under the rule in doc 003 §0.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable

from src.domain.health import ComponentHealth
from src.domain.meeting import HandRaise

COMPONENT_NAME = "scripted_hand_raise"


class ScriptedHandRaiseSource:
    """Yields a fixed list of raised hands, then waits without ending.

    Not ending matters for the reason ``ScriptedChatSource`` documents: the router runs its
    legs for the session's lifetime, and a source that returned would finish its leg. A real
    meeting is idle between raised hands, not finished.
    """

    def __init__(self, events: Iterable[HandRaise] = ()) -> None:
        self._queue: asyncio.Queue[HandRaise] = asyncio.Queue()
        for event in events:
            self._queue.put_nowait(event)
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    def push(self, event: HandRaise) -> None:
        """Raise a hand mid-test, the way a participant does."""
        self._queue.put_nowait(event)

    async def events(self) -> AsyncIterator[HandRaise]:
        while True:
            yield await self._queue.get()

    def health(self) -> ComponentHealth:
        return ComponentHealth.healthy(COMPONENT_NAME)
