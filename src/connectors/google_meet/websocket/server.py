"""PageBridgeServer — one loopback WebSocket server per session.

**Why the Python side is the server and the browser is the client.** A page can only
open connections outward; nothing can connect *into* it. So the direction is forced, and
it settles two other questions for free — the page needs no inbound port, and the
bridge's own address is something we choose and inject rather than discover.

**Why one server per session rather than one shared server with session routing.** Two
reasons, both about isolation. A shared server would need a session registry, a routing
key on every message, and a rule for what happens when a page connects with a stale key;
a per-session server needs none of that, because the only page that can reach it is the
one we launched. And it lets the port be ephemeral (``bridge_port = 0``), which means two
concurrent sessions cannot collide — where a fixed shared port makes the second session's
bind fail and takes the first one's server with it on restart.

**Security.** The socket is bound to loopback, but loopback is shared with every other
process on the host, so binding is not authentication. Two gates:

* The URL carries a per-session token generated with ``secrets.token_urlsafe``. It is
  checked against the request path *before* the handshake completes, so an unauthorised
  caller never reaches the message loop.
* The token is the only credential, so a caller that has it is already as trusted as the
  page. That is what makes accepting a *replacement* connection safe, and a replacement is
  not optional: an init script runs afresh on every full navigation, so the page opens a
  new socket each time the browser loads a document. The bridge navigates at least twice
  before joining — once to verify the Google session, once to the meeting — and a server
  that refused the second connection would leave the real page permanently unable to
  attach. The incumbent channel is closed rather than leaked.

The token is injected into the page as a constant by ``automation/driver.py`` and never
appears in a log line — ``endpoint_for_log`` exists so that the useful half of the URL
can still be logged.
"""

from __future__ import annotations

import asyncio
import secrets
from contextlib import suppress
from typing import Final

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import WebSocketException
from websockets.http11 import Request, Response

from src.connectors.google_meet.exceptions import BridgeUnavailableError
from src.connectors.google_meet.websocket.channel import PageChannel
from src.infrastructure.logging import get_logger

logger = get_logger(__name__)

_TOKEN_BYTES: Final[int] = 32
_PATH_PREFIX: Final[str] = "/bridge/"

MAX_MESSAGE_BYTES: Final[int] = 12 * 1024 * 1024
"""WebSocket-level frame ceiling, above the protocol's own 8 MB payload limit so that an
oversized frame is rejected by our codec with a named error rather than by the library
with a connection reset. Sized for a 1080p I420 frame plus headroom."""


class PageBridgeServer:
    """A token-gated loopback WebSocket server that accepts exactly one page."""

    __slots__ = (
        "_attached",
        "_channel",
        "_generation",
        "_host",
        "_port",
        "_requested_port",
        "_server",
        "_token",
    )

    def __init__(self, *, host: str = "127.0.0.1", port: int = 0) -> None:
        self._host = host
        self._requested_port = port
        self._port = port
        self._token = secrets.token_urlsafe(_TOKEN_BYTES)
        self._server: Server | None = None
        self._channel: PageChannel | None = None
        # An Event rather than a Future, because a page attaches more than once: every
        # full navigation re-runs the init script and opens a fresh socket. A Future can
        # only be resolved once, so it would report the connection that existed during
        # sign-in verification and never the one that is actually in the meeting.
        self._attached = asyncio.Event()
        self._generation = 0

    # -- addressing --------------------------------------------------------

    @property
    def token(self) -> str:
        """The per-session secret the page must present. Never log this."""
        return self._token

    @property
    def port(self) -> int:
        """The bound port. Meaningful only after ``start()``."""
        return self._port

    @property
    def endpoint(self) -> str:
        """The URL to inject into the page. Contains the token — never log it."""
        return f"ws://{self._host}:{self._port}{_PATH_PREFIX}{self._token}"

    @property
    def endpoint_for_log(self) -> str:
        """The same endpoint with the token redacted, safe for structured logs."""
        return f"ws://{self._host}:{self._port}{_PATH_PREFIX}<token>"

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Bind and begin listening. Idempotent.

        Raises:
            BridgeUnavailableError: the port could not be bound.
        """
        if self._server is not None:
            return

        try:
            self._server = await serve(
                self._handle,
                self._host,
                self._requested_port,
                process_request=self._authorise,
                max_size=MAX_MESSAGE_BYTES,
                # The page runs its own heartbeat over the protocol, and library-level
                # pings racing with it produced spurious closes under load. One liveness
                # mechanism, at the layer that can act on it.
                ping_interval=None,
            )
        except (OSError, WebSocketException) as exc:
            raise BridgeUnavailableError(
                f"cannot bind the page bridge to {self._host}:{self._requested_port}: {exc}"
            ) from exc

        self._port = self._resolve_bound_port()
        logger.info("meet_bridge.listening", endpoint=self.endpoint_for_log)

    def _resolve_bound_port(self) -> int:
        """Read back the port the OS actually assigned.

        Required whenever ``bridge_port`` is 0, which is the default: the injected URL
        has to name the real port, and asking the socket is the only way to know it.
        """
        server = self._server
        if server is None:  # pragma: no cover - start() sets it
            return self._requested_port
        for sock in server.sockets:
            try:
                return int(sock.getsockname()[1])
            except (OSError, IndexError, TypeError, ValueError):  # pragma: no cover
                continue
        return self._requested_port

    async def stop(self) -> None:
        """Close the channel and stop listening. Idempotent."""
        channel, self._channel = self._channel, None
        self._attached.clear()
        if channel is not None:
            await channel.close()

        server, self._server = self._server, None
        if server is not None:
            server.close()
            # Best-effort: teardown must not be able to fail a session stop, and the
            # listening socket is going away with the process either way.
            with suppress(OSError, WebSocketException):
                await server.wait_closed()

    # -- accepting the page ------------------------------------------------

    def _authorise(self, connection: ServerConnection, request: Request) -> Response | None:
        """Reject unauthorised or duplicate connections before the handshake completes.

        Returning a ``Response`` aborts the upgrade, so a caller with the wrong token
        never becomes a WebSocket and never reaches ``_handle``.

        ``secrets.compare_digest`` rather than ``==``: the comparison is against a
        secret, and a timing-distinguishable check on a loopback socket is exactly the
        situation where the difference is measurable.
        """
        path = request.path or ""
        if not path.startswith(_PATH_PREFIX):
            logger.warning("meet_bridge.rejected", reason="bad path")
            return connection.respond(404, "not found\n")

        presented = path[len(_PATH_PREFIX) :]
        if not secrets.compare_digest(presented, self._token):
            logger.warning("meet_bridge.rejected", reason="bad token")
            return connection.respond(403, "forbidden\n")

        return None

    async def _handle(self, connection: ServerConnection) -> None:
        """Hold the accepted connection open until the page goes away.

        ``websockets`` closes the socket as soon as this coroutine returns, so it has to
        stay parked until the channel is finished with. Everything that reads or writes
        does so through the ``PageChannel`` this publishes.
        """
        superseded, self._channel = self._channel, None
        if superseded is not None:
            # A navigation replaced the page. Close the incumbent explicitly rather than
            # dropping the reference: its handler is parked on ``wait_closed`` and would
            # otherwise hold the connection — and a stale channel that still accepts
            # writes would silently swallow media bound for a page that no longer exists.
            logger.info("meet_bridge.page_superseded", remote=superseded.remote)
            await superseded.close()

        channel = PageChannel(connection)
        self._channel = channel
        self._generation += 1
        self._attached.set()
        logger.info(
            "meet_bridge.page_connected", remote=channel.remote, generation=self._generation
        )

        try:
            await connection.wait_closed()
        finally:
            logger.info("meet_bridge.page_disconnected", remote=channel.remote)
            if self._channel is channel:
                self._channel = None
                self._attached.clear()

    async def wait_for_page(self, *, timeout_s: float) -> PageChannel:
        """Wait until a page is attached and return its channel.

        Returns whichever page is attached *now*, which for a browser that has navigated
        is the newest one. Callers must not cache the result across a navigation — the
        bridge re-reads it after joining, for exactly that reason.

        Raises:
            BridgeUnavailableError: the server is not started, or no page attached. A
                timeout here means the injected script never ran — a Chromium that started
                but never reached the bridge — which is a different fault from a join
                timeout and worth reporting as such.
        """
        if self._server is None:
            raise BridgeUnavailableError("page bridge server is not started")

        try:
            await asyncio.wait_for(self._attached.wait(), timeout=timeout_s)
        except TimeoutError as exc:
            raise BridgeUnavailableError(
                f"the Chromium page did not connect to the bridge within {timeout_s}s; "
                "the injected bridge script may not have run"
            ) from exc

        channel = self._channel
        if channel is None:  # pragma: no cover - the event is cleared with the channel
            raise BridgeUnavailableError("the page disconnected while attaching")
        return channel

    @property
    def channel(self) -> PageChannel | None:
        """The live channel, if a page is attached."""
        return self._channel

    @property
    def generation(self) -> int:
        """How many pages have attached. Rises on every navigation."""
        return self._generation
