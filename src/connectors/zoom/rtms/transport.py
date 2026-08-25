"""RTMS WebSocket transport.

A thin seam over ``websockets`` so the handshake logic in ``service.py`` can be
tested against an in-memory transport instead of a live socket. It carries JSON
frames and nothing else — no protocol knowledge lives here.

``orjson`` for decode: at 50 messages/second/participant the JSON+base64 envelope
genuinely costs more than the 640 bytes of audio it wraps, so the faster parser is
worth having on this hop.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Any, Protocol, runtime_checkable

import orjson
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import WebSocketException

from src.connectors.zoom.exceptions import RtmsConnectionError, RtmsProtocolError


@runtime_checkable
class JsonWebSocket(Protocol):
    """A bidirectional JSON message channel."""

    async def send_json(self, payload: dict[str, Any]) -> None: ...
    async def recv_json(self) -> dict[str, Any]: ...
    async def close(self) -> None: ...

    def messages(self) -> AsyncIterator[dict[str, Any]]: ...


class WebSocketTransport:
    """``JsonWebSocket`` over a real ``websockets`` connection."""

    __slots__ = ("_connection", "_url")

    def __init__(self, connection: ClientConnection, *, url: str) -> None:
        self._connection = connection
        self._url = url

    @property
    def url(self) -> str:
        return self._url

    @classmethod
    async def open(
        cls,
        url: str,
        *,
        open_timeout: float = 15.0,
        max_size: int = 16 * 1024 * 1024,
    ) -> WebSocketTransport:
        """Open a connection.

        Raises:
            RtmsConnectionError: the connection could not be established.
        """
        try:
            connection = await connect(
                url,
                open_timeout=open_timeout,
                max_size=max_size,
                # RTMS drives its own keep-alive at the application layer
                # (msg_type 12/13), so protocol-level pings would be redundant
                # traffic that can also race the application watchdog.
                ping_interval=None,
            )
        except (WebSocketException, OSError, TimeoutError) as exc:
            raise RtmsConnectionError(f"cannot connect to {url}: {exc}") from exc
        return cls(connection, url=url)

    async def send_json(self, payload: dict[str, Any]) -> None:
        try:
            await self._connection.send(orjson.dumps(payload))
        except (WebSocketException, OSError) as exc:
            raise RtmsConnectionError(f"send failed on {self._url}: {exc}") from exc

    async def recv_json(self) -> dict[str, Any]:
        try:
            raw = await self._connection.recv()
        except (WebSocketException, OSError) as exc:
            raise RtmsConnectionError(f"receive failed on {self._url}: {exc}") from exc
        return _decode(raw)

    async def messages(self) -> AsyncIterator[dict[str, Any]]:
        """Yield decoded messages until the socket closes."""
        try:
            async for raw in self._connection:
                yield _decode(raw)
        except (WebSocketException, OSError) as exc:
            raise RtmsConnectionError(f"stream ended on {self._url}: {exc}") from exc

    async def close(self) -> None:
        # Already gone is fine: closing is best-effort and must stay idempotent.
        with suppress(WebSocketException, OSError):
            await self._connection.close()


def _decode(raw: str | bytes) -> dict[str, Any]:
    try:
        parsed = orjson.loads(raw)
    except orjson.JSONDecodeError as exc:
        raise RtmsProtocolError("received a non-JSON RTMS frame") from exc
    if not isinstance(parsed, dict):
        raise RtmsProtocolError(f"expected a JSON object, got {type(parsed).__name__}")
    return parsed
