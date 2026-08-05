"""MediaRouter — the data plane.

Moves frames between ingest, the avatar, the decoder and the pacer. Routing only:
echo policy lives in ``EchoGuard``, decoder lifecycle in ``DecodePipeline``, timing in
``Pacer`` (doc 002 §1.2 D3 split this apart).

Four concurrent legs, all for the session's lifetime:

* ``_route_inbound``  — ingest → echo guard → avatar
* ``_route_chunks``   — avatar fMP4 → decode pipeline
* ``_route_video``    — decoder video → pacer
* ``_route_audio``    — decoder audio → pacer

The pacer runs its own loops, which is what keeps publishing continuous even when
every one of these legs is idle.

**Direct calls over bounded queues, not an event bus.** Doc 003 §0.1 cut the bus for a
concrete reason: a fan-out bus has no coherent backpressure semantics, so a slow
subscriber would either stall the meeting or silently drop — and the per-stage drop
policy in doc 003 §7.2 cannot be expressed as bus semantics. Events describe; queues
carry.
"""

from __future__ import annotations

import asyncio

from src.avatar.client import AvatarClient
from src.domain.context import FrameContext
from src.domain.health import ComponentHealth, HealthReport
from src.domain.media import AudioFrame
from src.infrastructure.logging import get_logger
from src.infrastructure.metrics import MetricName, MetricsCollector
from src.protocols.audio_source import AudioSource
from src.services.media.clock import MediaClock
from src.services.media.decode_pipeline import DecodePipeline
from src.services.media.echo_guard import EchoGuard
from src.services.media.pacer import Pacer

logger = get_logger(__name__)

COMPONENT_NAME = "media_router"


class MediaRouter:
    """Routes media between ingest, the avatar agent, and the publisher."""

    __slots__ = (
        "_avatar",
        "_clock",
        "_ctx",
        "_decode",
        "_echo_guard",
        "_forwarded",
        "_metrics",
        "_pacer",
        "_source",
        "_suppressed",
    )

    def __init__(
        self,
        *,
        ctx: FrameContext,
        clock: MediaClock,
        source: AudioSource,
        avatar: AvatarClient,
        decode: DecodePipeline,
        pacer: Pacer,
        echo_guard: EchoGuard,
        metrics: MetricsCollector | None = None,
    ) -> None:
        self._ctx = ctx
        self._clock = clock
        self._source = source
        self._avatar = avatar
        self._decode = decode
        self._pacer = pacer
        self._echo_guard = echo_guard
        self._metrics = metrics
        self._forwarded = 0
        self._suppressed = 0

    @property
    def stats(self) -> dict[str, int]:
        return {
            "forwarded": self._forwarded,
            "suppressed": self._suppressed,
            **self._pacer.stats,
        }

    async def run(self) -> None:
        """Run every routing leg until cancelled or a leg fails."""
        async with asyncio.TaskGroup() as group:
            group.create_task(self._route_inbound(), name="route-inbound")
            group.create_task(self._route_chunks(), name="route-chunks")
            group.create_task(self._route_video(), name="route-video")
            group.create_task(self._route_audio(), name="route-audio")
            group.create_task(self._pacer.run(), name="pacer")

    # -- inbound: Zoom → avatar -------------------------------------------

    async def _route_inbound(self) -> None:
        async for frame in self._source.frames():
            await self._forward(frame)

    async def _forward(self, frame: AudioFrame) -> None:
        now_us = self._clock.now_us()

        if not self._echo_guard.should_forward(frame, now_us=now_us):
            self._suppressed += 1
            return

        started = self._clock.now_us()
        await self._avatar.send(frame)
        self._forwarded += 1

        if self._metrics is not None:
            self._metrics.observe(
                MetricName.ROUTER_TO_AVATAR_US, self._clock.now_us() - started, ctx=frame.ctx
            )
            # Ingest→router latency measured against the frame's own PTS, which was
            # stamped when RTMS delivered it.
            self._metrics.observe(
                MetricName.INGEST_TO_ROUTER_US, max(now_us - frame.pts_us, 0), ctx=frame.ctx
            )

    # -- avatar → decoder --------------------------------------------------

    async def _route_chunks(self) -> None:
        first_fragment_seen = False
        async for chunk in self._avatar.chunks():
            await self._decode.feed(chunk)

            if not chunk.is_init_segment and not first_fragment_seen:
                first_fragment_seen = True
                # Time to first fragment is the avatar's contribution to perceived
                # latency, and the number most worth watching (doc 003 §7.5).
                if self._metrics is not None:
                    self._metrics.observe(
                        MetricName.AVATAR_RTT_US,
                        self._clock.now_us() - chunk.received_at_us,
                        ctx=chunk.ctx,
                    )
                logger.info("router.first_fragment", seq=chunk.seq, bytes=chunk.size_bytes)

    # -- decoder → pacer ---------------------------------------------------

    async def _route_video(self) -> None:
        async for frame in self._decode.decoder.video():
            self._pacer.submit_video(frame)
            if self._metrics is not None:
                self._metrics.observe(
                    MetricName.VIDEO_DELAY_US,
                    max(self._clock.now_us() - frame.pts_us, 0),
                    ctx=frame.ctx,
                )

    async def _route_audio(self) -> None:
        async for frame in self._decode.decoder.audio():
            self._pacer.submit_audio(frame)
            if self._metrics is not None:
                self._metrics.observe(
                    MetricName.AUDIO_DELAY_US,
                    max(self._clock.now_us() - frame.pts_us, 0),
                    ctx=frame.ctx,
                )

    # -- health ------------------------------------------------------------

    def health(self) -> HealthReport:
        return HealthReport(
            components=(
                self._source.health(),
                self._avatar.health(),
                self._decode.health(),
                ComponentHealth.healthy(COMPONENT_NAME, f"forwarded={self._forwarded}"),
            )
        )

    def close(self) -> None:
        self._pacer.close()
