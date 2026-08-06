"""ConnectorRegistry — platform → session factory.

Doc 003 §0.1 cut this from doc 002's design with a good reason: *"registry
indirection with one entry"*. With Teams there are two entries, and the alternative
is an ``if platform is ZOOM: ... elif platform is TEAMS: ...`` in ``MeetingService``
that every future connector has to edit. This is the smallest thing that removes
that branch.

It holds factories, not sessions. Lookup is the whole job — no lifecycle, no
capability negotiation, no URL resolution. Adding Google Meet is one ``register``
call in ``containers.py`` and nothing else in ``services/``.
"""

from __future__ import annotations

from collections.abc import Iterator

from src.domain.exceptions import DomainError
from src.domain.meeting import MeetingPlatform
from src.protocols.connector import ConnectorSessionFactory


class UnsupportedPlatformError(DomainError):
    """A session was requested for a platform no connector is registered for.

    Distinct from "the platform does not exist": a connector can be absent because
    it is unconfigured in this deployment, which is an operator-fixable 4xx rather
    than a bug.
    """

    def __init__(self, platform: MeetingPlatform, supported: frozenset[MeetingPlatform]) -> None:
        available = ", ".join(sorted(supported)) or "none"
        super().__init__(
            f"no connector registered for platform {platform!r} (registered: {available})"
        )
        self.platform = platform
        self.supported = supported


class ConnectorRegistry:
    """Maps a meeting platform to the factory that builds sessions for it."""

    __slots__ = ("_factories",)

    def __init__(self) -> None:
        self._factories: dict[MeetingPlatform, ConnectorSessionFactory] = {}

    def register(
        self, platform: MeetingPlatform, factory: ConnectorSessionFactory
    ) -> ConnectorRegistry:
        """Register a connector. Returns self so wiring can chain.

        Raises:
            ValueError: the platform already has a factory. Silently replacing one
                connector with another is never intentional.
        """
        if platform in self._factories:
            raise ValueError(f"platform {platform!r} is already registered")
        self._factories[platform] = factory
        return self

    def get(self, platform: MeetingPlatform) -> ConnectorSessionFactory:
        """Look up a connector.

        Raises:
            UnsupportedPlatformError: nothing is registered for ``platform``.
        """
        factory = self._factories.get(platform)
        if factory is None:
            raise UnsupportedPlatformError(platform, self.supported())
        return factory

    def supported(self) -> frozenset[MeetingPlatform]:
        """Every platform this deployment can serve."""
        return frozenset(self._factories)

    def __contains__(self, platform: object) -> bool:
        return platform in self._factories

    def __len__(self) -> int:
        return len(self._factories)

    def __iter__(self) -> Iterator[MeetingPlatform]:
        return iter(self._factories)
