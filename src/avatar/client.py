"""AvatarClient — protocol concerns over an ``AvatarTransport``.

Split from the transport so its logic is testable without a socket. The client owns:

* the handshake (built from the fixed contract in ``domain.avatar``);
* **init-segment caching**, replayed into every new decoder;
* reconnect with backoff;
* PCM forwarding with format validation.

The init-segment cache is the load-bearing piece. An fMP4 decoder cannot resume from
a mid-stream ``moof``, so on decoder restart the cached ``ftyp``+``moov`` must be
replayed first. Without it, restart looks successful and produces permanently black
video (doc 003 §0.2).

This module is platform-blind by construction: no Zoom import, enforced by
``tests/architecture/test_layering.py``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from src.domain.avatar import AVATAR_INPUT_FORMAT, AvatarClientHello
from src.domain.context import FrameContext
from src.domain.exceptions import AvatarProtocolMismatchError, InvalidFrameError
from src.domain.health import ComponentHealth, ComponentState
from src.domain.media import AudioFrame, MediaChunk
from src.infrastructure.logging import get_logger
from src.infrastructure.metrics import MetricName, MetricsCollector
from src.infrastructure.reconnect import ReconnectPolicy
from src.protocols.avatar import AvatarTransport

logger = get_logger(__name__)

COMPONENT_NAME = "avatar_client"


class AvatarClient:
    """Sends PCM to the avatar agent and yields the fMP4 it streams back."""

    __slots__ = (
        "_connected",
        "_ctx",
        "_init_segment",
        "_metrics",
        "_policy",
        "_transport",
        "_transport_factory",
    )

    def __init__(
        self,
        *,
        transport: AvatarTransport,
        ctx: FrameContext,
        policy: ReconnectPolicy | None = None,
        metrics: MetricsCollector | None = None,
        transport_factory: object | None = None,
    ) -> None:
        self._transport = transport
        self._ctx = ctx
        self._policy = policy or ReconnectPolicy()
        self._metrics = metrics
        self._transport_factory = transport_factory
        self._init_segment: MediaChunk | None = None
        self._connected = False

    @property
    def init_segment(self) -> MediaChunk | None:
        """The cached ``ftyp``+``moov``, for decoder (re)start."""
        return self._init_segment

    @property
    def is_connected(self) -> bool:
        return self._connected

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Connect and complete the handshake.

        Raises:
            AvatarProtocolMismatchError: the agent is incompatible. Not retried —
                a version mismatch will not resolve itself.
            AvatarTransportError: the connection failed.
        """
        hello = AvatarClientHello(
            session_id=self._ctx.session_id, correlation_id=self._ctx.correlation_id
        )
        await self._transport.connect(hello)
        self._connected = True

    async def stop(self) -> None:
        """Disconnect. Idempotent."""
        self._connected = False
        await self._transport.close()

    def health(self) -> ComponentHealth:
        transport_health = self._transport.health()
        if transport_health.state is ComponentState.HEALTHY and not self._connected:
            return ComponentHealth.unknown(COMPONENT_NAME, "not started")
        return ComponentHealth(
            name=COMPONENT_NAME,
            state=transport_health.state,
            detail=transport_health.detail,
        )

    # -- media -------------------------------------------------------------

    async def send(self, frame: AudioFrame) -> None:
        """Forward one audio frame to the agent.

        The format is asserted rather than converted. RTMS is configured to deliver
        exactly ``AVATAR_INPUT_FORMAT``, so a mismatch is a wiring bug that should
        surface here — not something to paper over with a silent resample in the hot
        path (doc 003 §3.3).

        Raises:
            InvalidFrameError: the frame is not in the avatar's input format.
        """
        if frame.format != AVATAR_INPUT_FORMAT:
            raise InvalidFrameError(
                f"avatar requires {AVATAR_INPUT_FORMAT}, got {frame.format}. "
                "RTMS should have been subscribed to the avatar's native format."
            )
        await self._transport.send_pcm(frame.pcm)

    async def chunks(self) -> AsyncIterator[MediaChunk]:
        """Yield fMP4 chunks, caching the init segment as it passes."""
        async for chunk in self._transport.chunks():
            if chunk.is_init_segment:
                self._init_segment = chunk
                logger.info(
                    "avatar.init_segment_cached",
                    bytes=chunk.size_bytes,
                    note="replayed into every decoder restart",
                )
            yield chunk

    async def reconnect(self) -> bool:
        """Reconnect with backoff.

        The cached init segment is **kept** across reconnects: the agent may not
        resend it, and the decoder still needs it.

        Returns:
            True on success, False when the retry budget is exhausted.
        """
        await self._transport.close()
        self._connected = False

        attempt = 0
        while True:
            attempt += 1
            if self._policy.exhausted(attempt):
                logger.error("avatar.reconnect_exhausted", attempts=attempt - 1)
                return False

            delay = await self._policy.sleep(attempt)
            try:
                await self.start()
            except AvatarProtocolMismatchError:
                raise  # never recoverable
            except Exception as exc:
                logger.warning(
                    "avatar.reconnect_failed",
                    attempt=attempt,
                    delay_s=round(delay, 3),
                    error=str(exc),
                )
                continue

            if self._metrics is not None:
                self._metrics.increment(
                    MetricName.RECONNECTS_TOTAL, ctx=self._ctx, component=COMPONENT_NAME
                )
            logger.info("avatar.reconnected", attempts=attempt)
            return True

    async def __aenter__(self) -> AvatarClient:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await asyncio.shield(self.stop())
