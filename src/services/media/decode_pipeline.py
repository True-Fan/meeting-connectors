"""DecodePipeline — decoder lifecycle and restart.

Separated from the decoder itself because "how to decode" and "what to do when
decoding fails" are different concerns (doc 002 §1.2 D3). The pipeline owns:

* feeding chunks;
* caching the init segment and **replaying it on every restart**;
* restart on decoder death, with a bounded budget.

The init-segment replay is the whole reason this class exists. Restarting an fMP4
decoder without it yields a process that runs happily and emits nothing — recovery
that reports success and produces permanently black video.
"""

from __future__ import annotations

from src.domain.context import FrameContext
from src.domain.health import ComponentHealth, ComponentState
from src.domain.media import MediaChunk
from src.infrastructure.logging import get_logger
from src.infrastructure.metrics import MetricName, MetricsCollector
from src.protocols.decoder import MediaDecoder

logger = get_logger(__name__)

COMPONENT_NAME = "decode_pipeline"

DEFAULT_MAX_RESTARTS = 5


class DecodePipeline:
    """Owns one decoder and its restart policy."""

    __slots__ = (
        "_ctx",
        "_decoder",
        "_decoder_factory",
        "_init_segment",
        "_max_restarts",
        "_metrics",
        "_restarts",
        "_started",
    )

    def __init__(
        self,
        *,
        decoder: MediaDecoder,
        ctx: FrameContext,
        decoder_factory: object | None = None,
        max_restarts: int = DEFAULT_MAX_RESTARTS,
        metrics: MetricsCollector | None = None,
    ) -> None:
        self._decoder = decoder
        self._ctx = ctx
        self._decoder_factory = decoder_factory
        self._max_restarts = max_restarts
        self._metrics = metrics
        self._init_segment: MediaChunk | None = None
        self._restarts = 0
        self._started = False

    @property
    def decoder(self) -> MediaDecoder:
        return self._decoder

    @property
    def restarts(self) -> int:
        return self._restarts

    @property
    def init_segment(self) -> MediaChunk | None:
        return self._init_segment

    async def start(self, init_segment: MediaChunk | None = None) -> None:
        """Start decoding. Caches ``init_segment`` for later restarts."""
        if init_segment is not None:
            self._init_segment = init_segment
        await self._decoder.start(self._init_segment)
        self._started = True

    async def feed(self, chunk: MediaChunk) -> None:
        """Feed one chunk, caching it if it is the init segment."""
        if chunk.is_init_segment:
            self._init_segment = chunk
        if not self._started:
            await self.start()
        await self._decoder.feed(chunk)

    async def stop(self) -> None:
        """Stop decoding. Idempotent."""
        self._started = False
        await self._decoder.stop()

    async def restart(self) -> bool:
        """Restart the decoder, replaying the cached init segment.

        Returns:
            True on success, False when the restart budget is exhausted or no init
            segment is available — restarting without one would produce black video,
            so refusing is more honest than pretending to recover.
        """
        if self._restarts >= self._max_restarts:
            logger.error("decoder.restart_budget_exhausted", restarts=self._restarts)
            return False

        self._restarts += 1
        await self._decoder.stop()

        if self._init_segment is None:
            logger.error(
                "decoder.restart_without_init_segment",
                note="fMP4 cannot resume from a mid-stream moof; refusing to restart blind",
            )
            return False

        if self._decoder_factory is not None:
            self._decoder = self._decoder_factory()  # type: ignore[operator]

        await self._decoder.start(self._init_segment)
        self._started = True

        if self._metrics is not None:
            self._metrics.increment(MetricName.DECODER_RESTARTS_TOTAL, ctx=self._ctx)
        logger.warning(
            "decoder.restarted",
            restarts=self._restarts,
            init_segment_bytes=self._init_segment.size_bytes,
        )
        return True

    def health(self) -> ComponentHealth:
        inner = self._decoder.health()
        if not self._started:
            return ComponentHealth.unknown(COMPONENT_NAME, "not started")
        detail = inner.detail
        if self._restarts:
            detail = f"{detail or 'ok'} (restarts={self._restarts})"
        state = inner.state
        if state is ComponentState.HEALTHY and self._restarts:
            state = ComponentState.DEGRADED
        return ComponentHealth(name=COMPONENT_NAME, state=state, detail=detail)
