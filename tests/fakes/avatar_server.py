"""A real WebSocket avatar agent, for testing the real transport.

``FakeAvatarTransport`` substitutes the ``AvatarTransport`` port, which makes
``AvatarClient``'s protocol logic testable without a socket — and that is exactly why it
cannot catch a *lifecycle* fault. Its ``send_pcm`` appends to a list unconditionally, so a
pipeline that never connected the real transport still looks like it is delivering audio.

This stub sits one layer lower: it is a genuine WebSocket server speaking the real avatar
wire protocol, so ``WebSocketAvatarTransport`` runs unmodified against it — handshake, writer
task, reader task and all. That is what makes it possible to assert the thing that actually
matters: **did PCM leave the process.**

Wire shape, from ``avatar/ws_transport.py``:

1. the client opens the socket and sends ``AvatarClientHello`` as text JSON;
2. the server replies with ``AvatarServerHello`` as text JSON;
3. thereafter both directions are binary — PCM up, fragmented MP4 down.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import orjson
from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import WebSocketException


class StubAvatarServer:
    """A WebSocket avatar agent that records what it was sent.

    Args:
        reply: Handshake reply to send. Defaults to a compatible one. Override it to
            exercise the mismatch paths against a real socket.
        respond_with: fMP4 bytes to stream back once the first PCM frame arrives.
        accept: Set False to reject the handshake, so the client's failure path runs.
    """

    def __init__(
        self,
        *,
        reply: dict[str, Any] | None = None,
        respond_with: bytes | None = None,
        accept: bool = True,
    ) -> None:
        self._reply = reply or {
            "protocol_version": "1.0",
            "accepted": accept,
            "container": "fmp4",
        }
        self._respond_with = respond_with
        self._server: Server | None = None
        self._port = 0

        self.hellos: list[dict[str, Any]] = []
        self.received_pcm: list[bytes] = []
        self.connections = 0
        self.pcm_arrived = asyncio.Event()

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> str:
        """Bind on an ephemeral loopback port and return the URL to connect to."""
        self._server = await serve(self._handle, "127.0.0.1", 0, ping_interval=None)
        self._port = self._server.sockets[0].getsockname()[1]
        return self.url

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self._port}/stream"

    async def stop(self) -> None:
        """Stop listening. Idempotent."""
        server, self._server = self._server, None
        if server is None:
            return
        server.close()
        with contextlib.suppress(OSError, WebSocketException):
            await server.wait_closed()

    # -- the agent's behaviour ---------------------------------------------

    async def _handle(self, connection: ServerConnection) -> None:
        self.connections += 1
        try:
            raw = await connection.recv()
            self.hellos.append(orjson.loads(raw))
            await connection.send(orjson.dumps(self._reply).decode())

            async for message in connection:
                if isinstance(message, str):
                    continue
                self.received_pcm.append(message)
                self.pcm_arrived.set()
                if self._respond_with is not None and len(self.received_pcm) == 1:
                    await connection.send(self._respond_with)
        except (WebSocketException, OSError):
            # The client closing is the normal end of a session, not a fault.
            pass

    # -- assertions --------------------------------------------------------

    @property
    def total_pcm_bytes(self) -> int:
        return sum(len(pcm) for pcm in self.received_pcm)

    async def wait_for_pcm(self, *, timeout_s: float = 2.0) -> None:
        """Wait until at least one PCM frame has arrived.

        Raises:
            AssertionError: no audio arrived in time, which is the failure this stub
                exists to make visible.
        """
        try:
            await asyncio.wait_for(self.pcm_arrived.wait(), timeout=timeout_s)
        except TimeoutError:
            raise AssertionError(
                f"no PCM reached the avatar agent within {timeout_s}s: the media pipeline "
                "queued audio but never delivered it"
            ) from None
