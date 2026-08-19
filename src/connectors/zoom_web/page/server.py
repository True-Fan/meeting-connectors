"""The loopback WebSocket that carries the avatar's audio into the page.

Bound to ``127.0.0.1`` on an **ephemeral port**, with a per-session token compared
using ``compare_digest``. The socket is reachable by anything running on the host, so
an unauthenticated one would let a co-resident process speak as the avatar, and a
fixed port would make that trivially discoverable.

**Chromium must be launched with ``LocalNetworkAccessChecks`` disabled** or a page on
``app.zoom.us`` cannot open a connection to loopback at all — the flag is already in
the Meet launcher with the reasoning. Without it the page silently never connects and
the avatar is mute with nothing in the logs to say why, which is why ``connected``
below is surfaced in health rather than kept private.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Callable
from contextlib import suppress
from hmac import compare_digest
from typing import Any

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import WebSocketException
from websockets.http11 import Request, Response

from src.connectors.zoom_web.page.protocol import decode_event
from src.infrastructure.logging import get_logger

logger = get_logger(__name__)

TOKEN_QUERY_KEY = "token"

EventHandler = Callable[[dict[str, Any]], None]
"""What a decoded page event is handed to.

**Synchronous and obliged not to raise**, because it is called from this server's read loop.
An async handler would let a slow listener hold up the socket that also carries the avatar's
voice into the page, and an exception would drop the connection — which is the avatar going
mute because a hand-raise observer had a bad day. Every handler in this connector is written
to that rule, and ``_dispatch`` enforces it anyway."""


class PageAudioServer:
    """Serves the avatar's PCM to the page, and receives what only the page can see.

    **Named for what it was, and it now carries a second, much smaller thing.** Audio out is
    still the whole reason it exists: everything about the socket — the ephemeral port, the
    per-session token, the broadcast to every attached frame — is shaped by that. The return
    direction is a handful of JSON events a minute reporting a raised hand, which is the one
    signal Zoom's API does not offer and a browser can see. Renaming the class for a feature
    that is a rounding error on its traffic would cost every reader who knows it by this name.
    """

    __slots__ = (
        "_clients",
        "_events_dropped",
        "_events_received",
        "_handler",
        "_host",
        "_port",
        "_ready",
        "_server",
        "_token",
    )

    def __init__(self, *, host: str = "127.0.0.1") -> None:
        self._host = host
        self._port = 0
        self._token = secrets.token_urlsafe(24)
        self._server: Server | None = None
        self._clients: set[ServerConnection] = set()
        self._ready = asyncio.Event()
        self._handler: EventHandler | None = None
        self._events_received = 0
        self._events_dropped = 0

    @property
    def endpoint(self) -> str:
        """The URL the page connects back to. Only valid after ``start``."""
        return f"ws://{self._host}:{self._port}/?{TOKEN_QUERY_KEY}={self._token}"

    @property
    def connected(self) -> bool:
        return bool(self._clients)

    @property
    def attached_pages(self) -> int:
        return len(self._clients)

    @property
    def events_received(self) -> int:
        return self._events_received

    @property
    def events_dropped(self) -> int:
        """Frames the page sent that were not usable events. Non-zero means script skew."""
        return self._events_dropped

    def set_event_handler(self, handler: EventHandler | None) -> None:
        """Register what page events are delivered to. Replaces any previous handler.

        One handler rather than a listener list, because there is one consumer and a fan-out
        with no second subscriber is a shape maintained for nobody. The session fans out in
        Python if it ever needs to, where the cost of getting it wrong is visible.
        """
        self._handler = handler

    async def start(self) -> None:
        """Bind the loopback socket. Idempotent."""
        if self._server is not None:
            return
        self._server = await serve(
            self._handle, self._host, 0, process_request=self._authenticate
        )
        sockets = getattr(self._server, "sockets", None) or []
        if sockets:
            self._port = sockets[0].getsockname()[1]
        logger.info("zoom_web.page_server_listening", port=self._port)

    async def wait_connected(self, timeout_s: float) -> bool:
        """Wait for the page to attach. False on timeout rather than raising.

        The caller decides what an unattached page means; here it is only a fact.
        """
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._ready.wait(), timeout=timeout_s)
        return self._ready.is_set()

    async def send(self, payload: bytes) -> None:
        """Push one framed message to **every** attached frame.

        Broadcast rather than "the" page, because ``add_init_script`` runs in every
        frame Chromium creates: several of them open a socket and each builds its own
        microphone track. Only the frame whose track Zoom actually selected matters,
        and nothing here can tell which that is — so all of them are fed and the
        others discard it, which costs a few copies of a 20 ms frame.

        Keeping a single "latest" client instead loses the race outright: a
        short-lived frame connecting last takes ownership, and closing clears it,
        leaving the real meeting frame attached but never sent to.

        Never raises. A send that fails is a page that went away, which the session
        notices through health rather than through an exception on the pacer's path.
        """
        if not self._clients:
            return
        for client in tuple(self._clients):
            try:
                await client.send(payload)
            except (WebSocketException, RuntimeError):
                self._clients.discard(client)

    async def stop(self) -> None:
        """Close the socket and release the port. Idempotent."""
        server, self._server = self._server, None
        self._clients.clear()
        self._ready.clear()
        if server is None:
            return
        server.close()
        with suppress(Exception):
            await server.wait_closed()

    # -- connection handling ------------------------------------------------

    def _authenticate(
        self, connection: ServerConnection, request: Request
    ) -> Response | None:
        """Reject anything without this session's token, before the handshake."""
        _, _, query = request.path.partition("?")
        supplied = ""
        for part in query.split("&"):
            key, _, value = part.partition("=")
            if key == TOKEN_QUERY_KEY:
                supplied = value
                break
        if not compare_digest(supplied, self._token):
            logger.warning("zoom_web.page_auth_rejected")
            return connection.respond(401, "unauthorized\n")
        return None

    async def _handle(self, connection: ServerConnection) -> None:
        self._clients.add(connection)
        self._ready.set()
        logger.info("zoom_web.page_connected", attached=len(self._clients))
        try:
            # Iterating is also what keeps the connection alive for us to send on, which
            # is the only thing this loop used to do — the page had nothing to say.
            async for message in connection:
                self._dispatch(message)
        except WebSocketException:
            pass
        finally:
            self._clients.discard(connection)
            logger.info("zoom_web.page_disconnected", attached=len(self._clients))

    def _dispatch(self, message: str | bytes) -> None:
        """Decode one page frame and hand it to the handler. Never raises.

        **Every frame from every attached page is dispatched**, and the duplication that
        implies is deliberate. ``add_init_script`` runs in every frame Chromium creates, so
        several sockets exist and any of them may be the one whose DOM has the participant
        panel in it — there is no way here to tell which. Filtering to one would risk
        listening to the wrong frame; dispatching all of them costs a duplicate hand-raise
        event, which is exactly what the per-participant cooldown downstream is for.

        The handler's exceptions are swallowed for the reason ``RtmsService._notify``
        swallows its observer's: this is the loop that carries the avatar's voice into the
        page, and a bookkeeping listener may not be able to close it.
        """
        event = decode_event(message)
        if event is None:
            self._events_dropped += 1
            if self._events_dropped == 1:
                logger.warning(
                    "zoom_web.page_event_unusable",
                    note="the page sent a frame that is not a JSON event; the injected "
                    "script and this build may have drifted apart",
                )
            return

        self._events_received += 1
        handler = self._handler
        if handler is None:
            return
        try:
            handler(event)
        except Exception as exc:
            logger.warning(
                "zoom_web.page_event_handler_failed",
                event_type=event.get("type"),
                error=str(exc),
            )
