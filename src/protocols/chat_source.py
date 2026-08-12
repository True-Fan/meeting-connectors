"""Inbound meeting-chat port.

Implementations:

* ``connectors.google_meet.chat.MeetChatSource`` — messages the page observed in Meet's
  chat panel
* ``tests.fakes.chat.ScriptedChatSource`` — a fixed list, so the whole path from a typed
  message to a spoken answer is testable with no browser and no meeting

An async iterator rather than a callback, matching ``AudioSource`` for the same reason: the
consumer's pull rate is the backpressure signal, and a source that outruns it can drop at the
point where the drop is countable.

**Why chat is a separate port rather than another kind of frame on ``AudioSource``.** They
share no properties worth unifying. Audio is a continuous, real-time, lossy stream measured in
frames per second, whose late frames are worthless and are dropped on purpose. Chat is
discrete, rare, and must not be dropped — a question asked once and silently discarded is
indistinguishable to the asker from an avatar that ignored them. Nothing about the two
lifecycles is shared, so joining them would only mean branching inside every consumer.

The port is optional to a session: Zoom and Teams supply nothing today and the router treats
its absence as "this platform has no chat", not as a fault.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from src.domain.health import ComponentHealth
from src.domain.meeting import ChatMessage


@runtime_checkable
class ChatSource(Protocol):
    """A source of meeting chat messages for one session."""

    async def start(self) -> None:
        """Begin observing chat. Returns once messages can begin flowing."""
        ...

    async def stop(self) -> None:
        """Stop observing and release resources. Must be idempotent."""
        ...

    def messages(self) -> AsyncIterator[ChatMessage]:
        """Yield chat messages until the source is stopped."""
        ...

    def health(self) -> ComponentHealth:
        """Current health. Called by the supervisor; must not block."""
        ...
