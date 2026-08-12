"""Parsing chat messages the page observed, and serving them as a ``ChatSource``.

Two responsibilities that are deliberately separate: turning one ``CHAT_MESSAGE`` payload into
a domain ``ChatMessage`` (a pure function, so it is testable against every malformed shape Meet
can produce), and handing those messages to the router as they arrive.

**Why the page cannot be trusted to filter.** ``bridge.js`` reports what the chat panel renders
and nothing more, including the avatar's own messages and whatever text a UI element happens to
carry. Deciding what deserves an answer is policy, and policy lives in Python — the same split
that keeps the browser layer free of judgement everywhere else in this connector.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from src.domain.health import ComponentHealth
from src.domain.meeting import ChatMessage
from src.infrastructure.logging import get_logger
from src.services.media.clock import MediaClock

logger = get_logger(__name__)

COMPONENT_NAME = "google_meet_chat"

MAX_CHARS = 2_000
"""Longest message forwarded, in characters.

A cap rather than unbounded, because the text becomes an LLM prompt: a pasted wall of text
would cost tokens, delay the reply, and is far more likely to be spam than a question. Truncated
rather than dropped, so a long but genuine question still gets an answer."""


def parse_chat_message(body: dict[str, Any], *, received_at_us: int = 0) -> ChatMessage | None:
    """Build a ``ChatMessage`` from a ``CHAT_MESSAGE`` payload, or ``None`` if unusable.

    Never raises, for the reason ``parse_roster`` does not: the page is reporting on a DOM it
    does not control, and one odd entry must not break the feature.
    """
    if not isinstance(body, dict):
        return None

    text = str(body.get("text") or "").strip()
    if not text:
        return None
    if len(text) > MAX_CHARS:
        logger.info("meet_chat.truncated", original_chars=len(text), kept=MAX_CHARS)
        text = text[:MAX_CHARS]

    sender = str(body.get("sender") or "").strip() or None
    return ChatMessage(
        text=text,
        sender=sender,
        received_at_us=received_at_us,
        is_self=bool(body.get("isSelf")),
    )


class MeetChatSource:
    """``ChatSource`` fed by the page's chat observer.

    A bounded queue, and small: chat arrives at human typing speed, so anything that fills this
    is a malfunction rather than load. Overflow drops the *newest* — the opposite of the audio
    policy, and correct for the same reason it is wrong there. Audio must stay in real time so
    stale frames go; a conversation must stay coherent, so an earlier question keeps its place
    ahead of a later one.
    """

    __slots__ = ("_clock", "_dropped", "_queue", "_received", "_seen_ids", "_started")

    def __init__(self, *, clock: MediaClock, maxsize: int = 32) -> None:
        self._clock = clock
        self._queue: asyncio.Queue[ChatMessage] = asyncio.Queue(maxsize=maxsize)
        self._seen_ids: set[str] = set()
        self._received = 0
        self._dropped = 0
        self._started = False

    @property
    def received(self) -> int:
        return self._received

    @property
    def dropped(self) -> int:
        return self._dropped

    async def start(self) -> None:
        self._started = True

    async def stop(self) -> None:
        self._started = False

    def offer(self, body: dict[str, Any], *, message_id: str | None = None) -> bool:
        """Accept one payload from the page. Never blocks, never raises.

        Called from the bridge's read loop, which is the media channel — so this must not be
        able to stall it or throw into it. Both properties are why this is a plain method that
        returns a bool rather than a coroutine that might await.

        Deduplicated on the page's message id because Meet re-renders the chat list on almost
        every DOM mutation: without this, one typed question would be forwarded on every scan
        and the avatar would answer it repeatedly.
        """
        if message_id:
            if message_id in self._seen_ids:
                return False
            self._seen_ids.add(message_id)

        message = parse_chat_message(body, received_at_us=self._clock.now_us())
        if message is None:
            return False

        try:
            self._queue.put_nowait(message)
        except asyncio.QueueFull:
            self._dropped += 1
            logger.warning(
                "meet_chat.dropped",
                reason="queue full",
                dropped_total=self._dropped,
                note="chat is arriving faster than the agent can answer",
            )
            return False

        self._received += 1
        logger.info(
            "meet_chat.received",
            sender=message.sender,
            chars=len(message.text),
            is_self=message.is_self,
            total=self._received,
        )
        return True

    async def messages(self) -> AsyncIterator[ChatMessage]:
        """Yield chat messages as the page observes them."""
        while True:
            yield await self._queue.get()

    def health(self) -> ComponentHealth:
        """Always healthy when started.

        There is no such thing as a broken chat source that can be distinguished from a meeting
        where nobody has typed anything, so claiming otherwise would be invention. The
        watchdog's job is noticing missing *audio*; a silent chat is the normal case.
        """
        if not self._started:
            return ComponentHealth.unknown(COMPONENT_NAME, "not started")
        return ComponentHealth.healthy(COMPONENT_NAME, f"received={self._received}")
