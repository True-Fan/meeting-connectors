"""RtmsAudioSource — the ``AudioSource`` port over RTMS.

Owns retry and health; ``RtmsService`` owns the protocol. Two separate reasons to
change, so two objects.

**RTMS sessions cannot be resumed** (doc 001 §10). A reconnect re-handshakes from
scratch and audio spoken during the gap is permanently lost — so the gap duration is
logged explicitly rather than being silently papered over. That number is the honest
measure of what a reconnect cost.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import suppress

from src.connectors.zoom.exceptions import (
    RtmsError,
    RtmsHandshakeError,
    RtmsProtocolError,
)
from src.connectors.zoom.rtms.service import RtmsService, TransportFactory
from src.domain.context import FrameContext
from src.domain.health import ComponentHealth, ComponentState
from src.domain.media import AudioFormat, AudioFrame
from src.infrastructure.logging import get_logger
from src.infrastructure.metrics import MetricName, MetricsCollector
from src.infrastructure.reconnect import ReconnectPolicy
from src.services.media.clock import MediaClock
from src.services.media.queues import (
    BoundedFrameQueue,
    OverflowPolicy,
    QueueClosedError,
)

logger = get_logger(__name__)

COMPONENT_NAME = "rtms_ingest"


class RtmsAudioSource:
    """Live Zoom participant audio, with reconnect."""

    __slots__ = (
        "_clock",
        "_ctx",
        "_current",
        "_detail",
        "_meeting_uuid",
        "_metrics",
        "_per_participant_audio",
        "_policy",
        "_queue",
        "_reconnects",
        "_send_rate_ms",
        "_signaling_url",
        "_signature",
        "_state",
        "_stream_id",
        "_task",
        "_transport_factory",
    )

    def __init__(
        self,
        *,
        meeting_uuid: str,
        rtms_stream_id: str,
        signaling_url: str,
        signature: str,
        ctx: FrameContext,
        clock: MediaClock,
        queue_size: int = 50,
        send_rate_ms: int = 20,
        per_participant_audio: bool = True,
        policy: ReconnectPolicy | None = None,
        metrics: MetricsCollector | None = None,
        transport_factory: TransportFactory | None = None,
    ) -> None:
        self._meeting_uuid = meeting_uuid
        self._stream_id = rtms_stream_id
        self._signaling_url = signaling_url
        self._signature = signature
        self._ctx = ctx
        self._clock = clock
        self._send_rate_ms = send_rate_ms
        self._per_participant_audio = per_participant_audio
        self._policy = policy or ReconnectPolicy()
        self._metrics = metrics
        self._transport_factory = transport_factory

        self._queue: BoundedFrameQueue[AudioFrame] = BoundedFrameQueue(
            name="rtms_inbound",
            maxsize=queue_size,
            policy=OverflowPolicy.DROP_OLDEST,
            metrics=metrics,
        )
        self._state = ComponentState.UNKNOWN
        self._detail: str | None = None
        self._task: asyncio.Task[None] | None = None
        self._current: RtmsService | None = None
        self._reconnects = 0

    # -- AudioSource -------------------------------------------------------

    async def start(self) -> None:
        """Attach and begin streaming. Returns once the first attach succeeds.

        Raises:
            RtmsError: the first attach failed unrecoverably (bad signature,
                rejected handshake). There is no point retrying a rejection.
        """
        if self._task is not None:
            return

        service = self._build_service()
        await service.attach(self._signaling_url)
        self._current = service
        self._state = ComponentState.HEALTHY
        self._detail = None
        self._task = asyncio.create_task(self._supervise(service), name="rtms-source")

    async def stop(self) -> None:
        """Detach and release. Idempotent."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            # We cancelled it deliberately; its cancellation is not an error.
            with suppress(asyncio.CancelledError):
                await task
        if self._current is not None:
            await self._current.detach()
            self._current = None
        self._queue.close()
        self._state = ComponentState.UNKNOWN
        self._detail = "stopped"

    async def frames(self) -> AsyncIterator[AudioFrame]:
        """Yield audio frames until the source is stopped."""
        while True:
            try:
                yield await self._queue.get()
            except QueueClosedError:
                return

    def health(self) -> ComponentHealth:
        return ComponentHealth(name=COMPONENT_NAME, state=self._state, detail=self._detail)

    # -- internals ---------------------------------------------------------

    @property
    def audio_format(self) -> AudioFormat:
        if self._current is not None:
            return self._current.audio_format
        return self._build_service().audio_format

    @property
    def reconnects(self) -> int:
        return self._reconnects

    def _build_service(self) -> RtmsService:
        return RtmsService(
            meeting_uuid=self._meeting_uuid,
            rtms_stream_id=self._stream_id,
            signature=self._signature,
            ctx=self._ctx,
            clock=self._clock,
            queue=self._queue,
            send_rate_ms=self._send_rate_ms,
            per_participant_audio=self._per_participant_audio,
            metrics=self._metrics,
            transport_factory=self._transport_factory,
        )

    async def _supervise(self, service: RtmsService) -> None:
        """Run the attached service, reconnecting on recoverable failure."""
        current = service
        attempt = 0

        while True:
            try:
                await current.run()
                # run() returning without error means both sockets closed cleanly,
                # which for a live meeting still means we have stopped hearing.
                raise RtmsError("RTMS stream ended")
            except asyncio.CancelledError:
                await current.detach()
                raise
            except (RtmsHandshakeError, RtmsProtocolError) as exc:
                # A rejected handshake or a contract violation will not fix itself.
                self._fail(f"unrecoverable: {exc}")
                return
            except (RtmsError, OSError) as exc:
                await current.detach()
                attempt += 1
                if self._policy.exhausted(attempt):
                    self._fail(f"reconnect budget exhausted after {attempt - 1} attempts: {exc}")
                    return

                self._state = ComponentState.UNHEALTHY
                self._detail = str(exc)
                gap_started = time.monotonic()

                delay = await self._policy.sleep(attempt)
                logger.warning(
                    "rtms.reconnecting", attempt=attempt, delay_s=round(delay, 3), error=str(exc)
                )

                try:
                    current = self._build_service()
                    await current.attach(self._signaling_url)
                except (RtmsError, OSError) as reattach_error:
                    self._detail = str(reattach_error)
                    continue

                # RTMS cannot resume: state the cost of the gap plainly.
                gap_s = time.monotonic() - gap_started
                self._reconnects += 1
                self._current = current
                self._state = ComponentState.HEALTHY
                self._detail = None
                attempt = 0

                # Buffered frames are stale by definition; replaying them would burst.
                self._queue.clear()

                logger.warning(
                    "rtms.reconnected",
                    audio_gap_s=round(gap_s, 3),
                    note="RTMS cannot resume; audio during the gap is permanently lost",
                )
                if self._metrics is not None:
                    self._metrics.increment(
                        MetricName.RECONNECTS_TOTAL, ctx=self._ctx, component=COMPONENT_NAME
                    )

    def _fail(self, detail: str) -> None:
        self._state = ComponentState.UNHEALTHY
        self._detail = detail
        logger.error("rtms.failed", detail=detail)
        self._queue.close()
