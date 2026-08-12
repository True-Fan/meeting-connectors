"""A scripted ``ChatSource`` — the port's second implementation.

Lets the whole path from a typed message to a forwarded text frame run with no browser, no
meeting, and no chat panel. ``MeetChatSource`` is the first implementation; this is what makes
the port earn its place under the rule in doc 003 §0.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable

from src.domain.health import ComponentHealth
from src.domain.meeting import ChatMessage

COMPONENT_NAME = "scripted_chat"


class ScriptedChatSource:
    """Yields a fixed list of messages, then waits without ending.

    Not ending matters: the router runs its legs in a task group for the session's lifetime, so
    a source that returned would complete its leg and, under a group that waits for all tasks,
    change the shape of what is being tested. A real chat source is idle, not finished.
    """

    def __init__(self, messages: Iterable[ChatMessage] = ()) -> None:
        self._queue: asyncio.Queue[ChatMessage] = asyncio.Queue()
        for message in messages:
            self._queue.put_nowait(message)
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    def push(self, message: ChatMessage) -> None:
        """Deliver a message mid-test, the way a participant typing does."""
        self._queue.put_nowait(message)

    async def messages(self) -> AsyncIterator[ChatMessage]:
        while True:
            yield await self._queue.get()

    def health(self) -> ComponentHealth:
        return ComponentHealth.healthy(COMPONENT_NAME)
