"""Inbound raised-hand port.

Implementations:

* ``connectors.google_meet.meeting.hand_raise.MeetHandRaiseSource`` — hands the page observed
  going up in Meet's participant list and tiles
* ``tests.fakes.hand_raise.ScriptedHandRaiseSource`` — a fixed list, so the whole path from a
  raised hand to an interrupted avatar is testable with no browser and no meeting

**Why this is not part of ``ChatSource``.** They arrive by the same mechanism — the page
watching the DOM — and mean opposite things. A chat message is a question that waits its turn;
a raised hand is a claim on the *current* turn, and the router responds to it by stopping the
avatar mid-sentence. Sharing a port would mean a discriminator on every message and a branch in
every consumer, which is the same distinction with the name taken off it.

An async iterator rather than a callback, matching ``ChatSource`` and ``AudioSource``: the
consumer's pull rate is the backpressure signal.

The port is optional to a session: Zoom and Teams supply nothing today, and the router treats
its absence as "this platform does not report raised hands", not as a fault.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from src.domain.health import ComponentHealth
from src.domain.meeting import HandRaise


@runtime_checkable
class HandRaiseSource(Protocol):
    """A source of raised-hand events for one session."""

    async def start(self) -> None:
        """Begin observing raised hands. Returns once events can begin flowing."""
        ...

    async def stop(self) -> None:
        """Stop observing and release resources. Must be idempotent."""
        ...

    def events(self) -> AsyncIterator[HandRaise]:
        """Yield raised-hand events until the source is stopped."""
        ...

    def health(self) -> ComponentHealth:
        """Current health. Called by the supervisor; must not block."""
        ...
