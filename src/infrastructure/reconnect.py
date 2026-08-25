"""Reconnect policy: exponential backoff with full jitter.

**Full** jitter, not "some" jitter: the delay is drawn uniformly from
``[0, computed_backoff]``. If several sessions lose a connection to the same Zoom
edge at the same moment — which is exactly when reconnect storms happen — equal
backoff makes them retry in lockstep and collide again. Randomising the whole
interval decorrelates them.

Used by both RTMS legs and the publisher sidecar, so retry behaviour is one
implementation rather than three subtly different ones.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ReconnectPolicy:
    """Backoff parameters for one recoverable component."""

    initial_delay_s: float = 0.5
    max_delay_s: float = 15.0
    max_attempts: int = 10
    multiplier: float = 2.0

    def __post_init__(self) -> None:
        if self.initial_delay_s <= 0:
            raise ValueError("initial_delay_s must be positive")
        if self.max_delay_s < self.initial_delay_s:
            raise ValueError("max_delay_s must be >= initial_delay_s")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")

    def backoff_ceiling_s(self, attempt: int) -> float:
        """Upper bound of the jitter window for a 1-based attempt number."""
        if attempt < 1:
            raise ValueError("attempt is 1-based")
        raw = self.initial_delay_s * (self.multiplier ** (attempt - 1))
        return min(raw, self.max_delay_s)

    def delay_s(self, attempt: int, *, rng: random.Random | None = None) -> float:
        """Draw the actual delay for a 1-based attempt number."""
        source = rng or random
        return source.uniform(0.0, self.backoff_ceiling_s(attempt))

    def exhausted(self, attempt: int) -> bool:
        """True when ``attempt`` exceeds the budget and the component must fail."""
        return attempt > self.max_attempts

    async def sleep(self, attempt: int, *, rng: random.Random | None = None) -> float:
        """Wait the drawn delay and return it (for logging)."""
        delay = self.delay_s(attempt, rng=rng)
        await asyncio.sleep(delay)
        return delay
