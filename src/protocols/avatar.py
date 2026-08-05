"""Avatar transport port.

Implementations:

* ``avatar.ws_transport.WebSocketAvatarTransport`` — the real agent (M3)
* ``tests.fakes.FakeAvatarTransport`` — canned fMP4, no avatar service needed

The port is the *transport*, not the client: ``AvatarClient`` owns protocol
concerns (handshake validation, init-segment caching, backpressure accounting) and
depends on this to move bytes. That split is what lets the client's logic be tested
without a socket.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from src.domain.avatar import AvatarClientHello, AvatarServerHello
from src.domain.health import ComponentHealth
from src.domain.media import MediaChunk


@runtime_checkable
class AvatarTransport(Protocol):
    """A bidirectional byte transport to the avatar agent."""

    async def connect(self, hello: AvatarClientHello) -> AvatarServerHello:
        """Open the connection and complete the protocol handshake.

        Raises:
            AvatarProtocolMismatchError: the agent's reply is incompatible.
        """
        ...

    async def close(self) -> None:
        """Close the connection. Must be idempotent."""
        ...

    async def send_pcm(self, pcm: bytes) -> None:
        """Send one PCM payload in ``AVATAR_INPUT_FORMAT``."""
        ...

    def chunks(self) -> AsyncIterator[MediaChunk]:
        """Yield container chunks streamed back by the agent."""
        ...

    def health(self) -> ComponentHealth:
        """Current health. Called by the supervisor; must not block."""
        ...
