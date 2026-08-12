"""WebSocket transport to the avatar agent.

Wire shape: a JSON text handshake, then binary frames in both directions — PCM out,
fMP4 in. Implements the ``AvatarTransport`` port.

Two properties matter more than anything else here:

* **Sending never blocks the ingest reader.** ``send_pcm`` offers to a bounded queue
  and returns; a writer task drains it. If the avatar stalls, we drop oldest audio and
  count it. Blocking here would stall the RTMS reader and cause Zoom-side loss —
  strictly worse than a drop we chose (doc 003 §7.1).
* **Framing is box-aware**, so the init segment is identifiable for decoder restart.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress

import orjson
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import WebSocketException

from src.avatar.framing import Fmp4Framer, Fmp4FramingError
from src.domain.avatar import AvatarClientHello, AvatarServerHello, check_handshake
from src.domain.context import FrameContext
from src.domain.health import ComponentHealth, ComponentState
from src.domain.media import MediaChunk
from src.infrastructure.logging import get_logger
from src.infrastructure.metrics import MetricName, MetricsCollector
from src.services.media.clock import MediaClock
from src.services.media.queues import BoundedFrameQueue, OverflowPolicy, QueueClosedError

logger = get_logger(__name__)

COMPONENT_NAME = "avatar_transport"


class AvatarTransportError(Exception):
    """The avatar transport failed. Recoverable unless stated otherwise."""


class WebSocketAvatarTransport:
    """``AvatarTransport`` over a real WebSocket."""

    __slots__ = (
        "_chunks",
        "_clock",
        "_connection",
        "_ctx",
        "_detail",
        "_framer",
        "_metrics",
        "_open_timeout_s",
        "_send_lock",
        "_send_queue",
        "_state",
        "_url",
        "_writer",
    )

    def __init__(
        self,
        *,
        url: str,
        ctx: FrameContext,
        clock: MediaClock,
        send_queue_size: int = 25,
        chunk_queue_size: int = 64,
        open_timeout_s: float = 10.0,
        metrics: MetricsCollector | None = None,
    ) -> None:
        self._url = url
        self._ctx = ctx
        self._clock = clock
        self._metrics = metrics
        self._open_timeout_s = open_timeout_s

        self._send_queue: BoundedFrameQueue[bytes] = BoundedFrameQueue(
            name="avatar_send",
            maxsize=send_queue_size,
            policy=OverflowPolicy.DROP_OLDEST,
            metrics=metrics,
        )
        self._chunks: BoundedFrameQueue[MediaChunk] = BoundedFrameQueue(
            name="avatar_chunks",
            maxsize=chunk_queue_size,
            # Never drop the oldest here: fMP4 fragments are order-dependent, and
            # discarding an earlier fragment corrupts every later one. Under pressure
            # refuse the newest instead.
            policy=OverflowPolicy.DROP_NEWEST,
            metrics=metrics,
        )
        self._framer = Fmp4Framer(ctx=ctx)
        # Serialises the PCM writer against control frames. One socket, two producers.
        self._send_lock = asyncio.Lock()
        self._connection: ClientConnection | None = None
        self._writer: asyncio.Task[None] | None = None
        self._state = ComponentState.UNKNOWN
        self._detail: str | None = None

    # -- AvatarTransport ---------------------------------------------------

    async def connect(self, hello: AvatarClientHello) -> AvatarServerHello:
        """Open the socket and complete the handshake.

        Raises:
            AvatarTransportError: the connection failed or the reply was malformed.
            AvatarProtocolMismatchError: the agent is incompatible (from
                ``check_handshake``) — not recoverable by retrying.
        """
        try:
            connection = await connect(
                self._url,
                open_timeout=self._open_timeout_s,
                max_size=None,  # fMP4 fragments may be large; the framer bounds them
                ping_interval=20.0,
                ping_timeout=20.0,
            )
        except (WebSocketException, OSError, TimeoutError) as exc:
            self._fail(f"connect failed: {exc}")
            raise AvatarTransportError(f"cannot connect to avatar at {self._url}: {exc}") from exc

        self._connection = connection

        try:
            await connection.send(hello.model_dump_json())
            raw = await asyncio.wait_for(connection.recv(), timeout=self._open_timeout_s)
        except (WebSocketException, OSError, TimeoutError) as exc:
            self._fail(f"handshake failed: {exc}")
            raise AvatarTransportError(f"avatar handshake failed: {exc}") from exc

        reply = self._parse_hello(raw)
        negotiated = check_handshake(hello, reply)

        self._framer.reset()
        self._state = ComponentState.HEALTHY
        self._detail = None
        self._writer = asyncio.create_task(self._drain_send_queue(), name="avatar-writer")

        logger.info("avatar.connected", url=self._url, protocol_version=str(negotiated))
        return reply

    async def close(self) -> None:
        """Close the socket. Idempotent."""
        writer, self._writer = self._writer, None
        if writer is not None:
            writer.cancel()
            with suppress(asyncio.CancelledError):
                await writer

        self._send_queue.close()
        self._chunks.close()

        connection, self._connection = self._connection, None
        if connection is not None:
            with suppress(WebSocketException, OSError):
                await connection.close()
        self._state = ComponentState.UNKNOWN
        self._detail = "closed"

    async def send_pcm(self, pcm: bytes) -> None:
        """Offer PCM for sending. Never blocks."""
        accepted = self._send_queue.put(pcm, ctx=self._ctx, reason="avatar_backpressure")
        if not accepted:
            self._state = ComponentState.DEGRADED
            self._detail = "send queue saturated"

    async def send_control(self, payload: str) -> None:
        """Send one JSON control frame, serialised against the PCM writer.

        **Sent directly rather than queued, and that is the point of the lock.** The PCM queue
        drops its oldest entry when full, which is right for audio and wrong for a question
        somebody typed: dropping it looks, to them, exactly like an avatar that ignored them.
        So chat bypasses the queue.

        Bypassing it means two tasks could call ``connection.send`` at once, and a WebSocket
        connection is not safe for concurrent sends — interleaved frames would corrupt the
        stream for both. The lock makes the two paths mutually exclusive while leaving the
        queue's own drop policy intact for audio.

        Failures are logged and swallowed. A chat message that cannot be delivered must not
        take down a session that is otherwise carrying a conversation.
        """
        connection = self._connection
        if connection is None:
            logger.warning("avatar.control_dropped", reason="not connected")
            return
        try:
            async with self._send_lock:
                await connection.send(payload)
        except (WebSocketException, OSError) as exc:
            logger.warning("avatar.control_send_failed", error=str(exc))

    async def chunks(self) -> AsyncIterator[MediaChunk]:
        """Yield fMP4 chunks streamed back by the agent."""
        reader = asyncio.create_task(self._read_loop(), name="avatar-reader")
        try:
            while True:
                try:
                    yield await self._chunks.get()
                except QueueClosedError:
                    return
        finally:
            reader.cancel()
            with suppress(asyncio.CancelledError):
                await reader

    def health(self) -> ComponentHealth:
        return ComponentHealth(name=COMPONENT_NAME, state=self._state, detail=self._detail)

    # -- internals ---------------------------------------------------------

    def _parse_hello(self, raw: str | bytes) -> AvatarServerHello:
        try:
            payload = orjson.loads(raw)
        except orjson.JSONDecodeError as exc:
            raise AvatarTransportError("avatar handshake reply was not JSON") from exc
        try:
            return AvatarServerHello.model_validate(payload)
        except ValueError as exc:
            raise AvatarTransportError(f"avatar handshake reply was malformed: {exc}") from exc

    async def _drain_send_queue(self) -> None:
        connection = self._connection
        if connection is None:
            return
        while True:
            try:
                pcm = await self._send_queue.get()
            except QueueClosedError:
                return
            started = self._clock.now_us()
            try:
                # Held only for the write itself, so a control frame waits microseconds
                # rather than for the queue to drain.
                async with self._send_lock:
                    await connection.send(pcm)
            except (WebSocketException, OSError) as exc:
                self._fail(f"send failed: {exc}")
                return
            if self._metrics is not None:
                self._metrics.observe(
                    MetricName.WEBSOCKET_SEND_US,
                    self._clock.now_us() - started,
                    ctx=self._ctx,
                )

    async def _read_loop(self) -> None:
        connection = self._connection
        if connection is None:
            return
        try:
            async for message in connection:
                if isinstance(message, str):
                    # Text after the handshake is a control/status message, not media.
                    logger.debug("avatar.text_message", body=message[:512])
                    continue
                self._ingest(message)
        except (WebSocketException, OSError) as exc:
            self._fail(f"stream ended: {exc}")
        except Fmp4FramingError as exc:
            # Not recoverable by reconnecting — the agent is emitting the wrong
            # container. Surface it loudly; assumption A1 (doc 003 §9).
            self._fail(f"framing error: {exc}")
            logger.error("avatar.framing_error", error=str(exc))
        finally:
            self._chunks.close()

    def _ingest(self, message: bytes) -> None:
        received_at = self._clock.now_us()
        for chunk in self._framer.feed(message, received_at_us=received_at):
            self._chunks.put(chunk, ctx=self._ctx, reason="chunk_overflow")
            if self._metrics is not None:
                self._metrics.increment(
                    MetricName.FRAMES_RECEIVED_TOTAL,
                    ctx=self._ctx,
                    kind="init_segment" if chunk.is_init_segment else "fragment",
                )

    def _fail(self, detail: str) -> None:
        self._state = ComponentState.UNHEALTHY
        self._detail = detail
