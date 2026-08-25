"""Domain-level exceptions.

These carry no transport or platform detail. Connector- and API-specific errors
live in their own packages and translate into these where a domain concept is
being violated.
"""

from __future__ import annotations


class DomainError(Exception):
    """Base class for all domain errors."""


class InvalidFrameError(DomainError):
    """A media frame's payload is inconsistent with its declared format."""


class IllegalStateTransitionError(DomainError):
    """A session was asked to move between two states that are not connected."""

    def __init__(self, current: str, requested: str) -> None:
        super().__init__(f"illegal session transition: {current} -> {requested}")
        self.current = current
        self.requested = requested


class AvatarProtocolMismatchError(DomainError):
    """The avatar agent speaks an incompatible protocol major version."""

    def __init__(self, ours: str, theirs: str) -> None:
        super().__init__(f"avatar protocol mismatch: bridge speaks {ours}, agent speaks {theirs}")
        self.ours = ours
        self.theirs = theirs
