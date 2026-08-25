"""The loopback WebSocket that carries the avatar's audio into the page, and the
meeting's audio and observations back out.

Bound to ``127.0.0.1`` on an **ephemeral port**, with a per-session token compared using
``compare_digest``. The socket is reachable by anything running on the host, so an
unauthenticated one would let a co-resident process speak as the avatar, and a fixed port
would make that trivially discoverable.

**Chromium must be launched with ``LocalNetworkAccessChecks`` disabled** or a page on
``teams.microsoft.com`` cannot open a connection to loopback at all — the flag is already in
the shared launcher with the full reasoning (``google_meet/browser/launcher.py``). Without it
the page silently never connects and the avatar is mute with nothing in the logs to say why,
which is why ``connected`` below is surfaced in health rather than kept private.
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

from src.connectors.teams_web.page.protocol import decode_audio, decode_event
from src.infrastructure.logging import get_logger

logger = get_logger(__name__)

TOKEN_QUERY_KEY = "token"

EventHandler = Callable[[dict[str, Any]], None]
"""What a decoded page event is handed to.

**Synchronous and obliged not to raise**, because it is called from this server's read loop.
An async handler would let a slow listener hold up the socket that also carries the avatar's
voice into the page, and an exception would drop the connection — which is the avatar going
mute because a chat observer had a bad day. Every handler in this connector is written to
that rule, and ``_dispatch`` enforces it anyway."""

AudioHandler = Callable[[bytes], None]
"""What one tapped PCM buffer from the page is handed to.

Synchronous and non-raising for the same reason, and with one addition that matters more
here: this fires **fifty times a second**. It must not block. ``PageAudioSource`` satisfies
that by doing nothing but a bounded ``put_nowait``, so a router that has stopped pulling
costs dropped frames at a counted point rather than a stalled read loop."""


class PageAudioServer:
    """Serves the avatar's PCM to the page, and receives everything only the page can see.

    **Named for the direction that dominates its traffic.** Audio out is what shapes the
    socket — the ephemeral port, the per-session token, the broadcast to every attached
    frame. Audio in is the same order of magnitude; the observations are a handful of JSON
    messages a second and a rounding error on both.
    """

    __slots__ = (
        "_audio_dropped",
        "_audio_handler",
        "_audio_received",
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
        self._audio_handler: AudioHandler | None = None
        self._events_received = 0
        self._events_dropped = 0
        self._audio_received = 0
        self._audio_dropped = 0

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

    @property
    def audio_received(self) -> int:
        """Tapped audio frames accepted from the page.

        **Zero is the first diagnosis for a deaf avatar.** The tap is the one part of this
        connector that depends on *where* Teams renders its audio rather than on what it
        renders, so an operator needs to know whether frames are arriving at all before
        looking at anything else. Surfaced through ``PageAudioSource.health``."""
        return self._audio_received

    @property
    def audio_dropped(self) -> int:
        """Binary frames from the page that were not decodable audio."""
        return self._audio_dropped

    def set_audio_handler(self, handler: AudioHandler | None) -> None:
        """Register where tapped meeting audio goes. ``None`` discards it.

        ``None`` is the state before ``PageAudioSource.start``: the page begins tapping as
        soon as it loads, and a frame arriving before the router is ready is dropped at a
        counted point rather than queued into something nobody will drain.
        """
        self._audio_handler = handler

    def set_event_handler(self, handler: EventHandler | None) -> None:
        """Register what page events are delivered to. Replaces any previous handler.

        One handler rather than a listener list, because there is one consumer and a fan-out
        with no second subscriber is a shape maintained for nobody. The observer fans out in
        Python, where the ordering between consumers is written down.
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
        logger.info("teams_web.page_server_listening", port=self._port)

    async def wait_connected(self, timeout_s: float) -> bool:
        """Wait for the page to attach. False on timeout rather than raising.

        The caller decides what an unattached page means; here it is only a fact.
        """
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._ready.wait(), timeout=timeout_s)
        return self._ready.is_set()

    async def send(self, payload: bytes) -> None:
        """Push one framed message to **every** attached frame.

        Broadcast rather than "the" page, because ``add_init_script`` runs in every frame
        Chromium creates and Teams creates several: each opens a socket and builds its own
        microphone track. Only the frame whose track Teams actually selected matters, and
        nothing here can tell which that is — so all of them are fed and the others discard
        it, which costs a few copies of a 20 ms frame.

        Keeping a single "latest" client instead loses the race outright: a short-lived frame
        connecting last takes ownership, and closing clears it, leaving the real meeting
        frame attached but never sent to.

        Never raises. A send that fails is a page that went away, which the session notices
        through health rather than through an exception on the pacer's path.
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
            logger.warning("teams_web.page_auth_rejected")
            return connection.respond(401, "unauthorized\n")
        return None

    async def _handle(self, connection: ServerConnection) -> None:
        self._clients.add(connection)
        self._ready.set()
        logger.info("teams_web.page_connected", attached=len(self._clients))
        try:
            # Iterating is also what keeps the connection alive for us to send on.
            async for message in connection:
                self._dispatch(message)
        except WebSocketException:
            pass
        finally:
            self._clients.discard(connection)
            logger.info("teams_web.page_disconnected", attached=len(self._clients))

    def _dispatch(self, message: str | bytes) -> None:
        """Route one page frame by its transport type. Never raises.

        **Binary is audio and text is an event**, which is the split ``page/protocol.py``
        describes. Routing on the frame type rather than on a parsed discriminator is what
        keeps the audio path free of a JSON decode fifty times a second — and it is a
        property the WebSocket transport already guarantees, so nothing is being inferred.
        """
        if isinstance(message, bytes | bytearray):
            self._dispatch_audio(bytes(message))
            return
        self._dispatch_event(message)

    def _dispatch_audio(self, message: bytes) -> None:
        """Hand one tapped PCM buffer to the audio handler. Never raises.

        Decoded before the handler check rather than after, so ``audio_dropped`` counts
        page/bridge script skew: a page sending frames nobody registered for is a different
        fault from a page sending frames nobody can parse, and only the second should look
        like a protocol problem.
        """
        pcm = decode_audio(message)
        if pcm is None:
            self._audio_dropped += 1
            if self._audio_dropped == 1:
                logger.warning(
                    "teams_web.page_audio_unusable",
                    note="the page sent a binary frame that is not a tapped audio frame; "
                    "the injected script and this build may have drifted apart",
                )
            return

        self._audio_received += 1
        if self._audio_received == 1:
            # The counterpart of ``TeamsWebMediaSink``'s first-publish line, and the one
            # that answers "is the tap working at all" without an operator having to reason
            # about where Teams renders its audio.
            logger.info("teams_web.first_audio_tapped", samples=len(pcm) // 2)

        handler = self._audio_handler
        if handler is None:
            return
        try:
            handler(pcm)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("teams_web.page_audio_handler_failed", error=str(exc))

    def _dispatch_event(self, message: str | bytes) -> None:
        """Decode one page event and hand it to the handler. Never raises.

        **Every frame from every attached page is dispatched**, and the duplication that
        implies is deliberate. ``add_init_script`` runs in every frame Chromium creates, so
        several sockets exist and any of them may be the one whose DOM has the roster in it —
        there is no way here to tell which. Filtering to one would risk listening to the
        wrong frame; dispatching all of them costs a duplicate event, which the observer's
        own state and the per-participant cooldown downstream already absorb.

        The handler's exceptions are swallowed because this is the loop that carries the
        avatar's voice into the page, and a bookkeeping listener may not be able to close it.
        """
        event = decode_event(message)
        if event is None:
            self._events_dropped += 1
            if self._events_dropped == 1:
                logger.warning(
                    "teams_web.page_event_unusable",
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
                "teams_web.page_event_handler_failed",
                event_type=event.get("type"),
                error=str(exc),
            )
