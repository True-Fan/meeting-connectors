"""ZoomMeetingSession — composition of one live avatar-in-a-meeting.

Holds the two independently-recovering legs (doc 003 §6.3):

* **ingest** — RTMS audio in (Python WebSocket)
* **publish** — Meeting SDK audio/video out (C++ sidecar)

The asymmetry is the architecture: audio arrives over a Python socket, media leaves
through a native process, and they meet only in ``services/media``.

**The publisher starts independently of ingest**, so the avatar joins, appears, and
idles before it can hear anything — which is the correct behaviour for a participant
who has joined but not yet spoken (doc 003 §3.1).
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path

from src.avatar.client import AvatarClient
from src.avatar.ws_transport import WebSocketAvatarTransport
from src.connectors.zoom.auth.rtms_signature import build_signature
from src.connectors.zoom.auth.sdk_jwt import SdkJwtFactory
from src.connectors.zoom.config import ZoomConnectorConfig
from src.connectors.zoom.publisher.publisher import MeetingPublisher
from src.connectors.zoom.publisher.uds_client import SidecarUdsClient
from src.connectors.zoom.rtms.audio_source import RtmsAudioSource
from src.connectors.zoom.rtms.mapping import rtms_attachment
from src.domain.health import ComponentHealth, ComponentState, HealthReport
from src.domain.media import AudioFormat, VideoFormat
from src.domain.session import SessionContext
from src.infrastructure.logging import get_logger
from src.infrastructure.metrics import MetricsCollector
from src.infrastructure.reconnect import ReconnectPolicy
from src.protocols.audio_source import AudioSource
from src.protocols.sink import MediaSink
from src.services.media.clock import MediaClock
from src.services.media.decode_pipeline import DecodePipeline
from src.services.media.decoders.ffmpeg import FfmpegDecoder
from src.services.media.echo_guard import EchoGuard
from src.services.media.idle_source import IdleFrameSource
from src.services.media.pacer import Pacer
from src.services.media.router import MediaRouter

logger = get_logger(__name__)


class ZoomMeetingSession:
    """One avatar participating in one Zoom meeting."""

    __slots__ = ("_clock", "_publisher", "_router", "_session", "_source", "_task")

    def __init__(
        self,
        *,
        session: SessionContext,
        clock: MediaClock,
        source: AudioSource,
        publisher: MediaSink,
        router: MediaRouter,
    ) -> None:
        self._session = session
        self._clock = clock
        self._source = source
        self._publisher = publisher
        self._router = router
        self._task: asyncio.Task[None] | None = None

    @property
    def session(self) -> SessionContext:
        return self._session

    @property
    def router(self) -> MediaRouter:
        return self._router

    async def start(self) -> None:
        """Start the publish leg, then ingest, then routing.

        Publish first and deliberately: it is what makes the avatar visible. Ingest
        may still be waiting on an ``rtms_started`` webhook we do not control.
        """
        await self._publisher.start(self._session.meeting)
        await self._source.start()
        self._task = asyncio.create_task(self._router.run(), name="media-router")

    async def stop(self) -> None:
        """Tear down in a fixed order. Idempotent.

        Publisher first so the participant leaves promptly rather than lingering as a
        frozen tile; then avatar and ingest; then the router's queues (doc 003 §6.3).
        """
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

        await self._publisher.stop()
        await self._source.stop()
        self._router.close()

    def health(self) -> HealthReport:
        return HealthReport(
            components=(*self._router.health().components, self._publisher.health())
        )

    def leg_states(self) -> tuple[ComponentState, ComponentState]:
        """``(ingest, publish)`` health, the input to session-state derivation."""
        return self._source.health().state, self._publisher.health().state


class ZoomSessionFactory:
    """Builds a fully wired ``ZoomMeetingSession``.

    All concrete-type knowledge for the Zoom feature lives here, so
    ``MeetingService`` composes sessions without naming RTMS, ffmpeg, or the sidecar.
    """

    def __init__(
        self,
        *,
        config: ZoomConnectorConfig,
        metrics: MetricsCollector | None = None,
        sink_override: MediaSink | None = None,
        source_override: AudioSource | None = None,
    ) -> None:
        self._config = config
        self._metrics = metrics
        # Overrides let M4-style verification run the whole pipeline into a FileSink,
        # and let tests substitute fakes, without a second code path.
        self._sink_override = sink_override
        self._source_override = source_override

    def build(self, session: SessionContext) -> ZoomMeetingSession:
        config = self._config
        ctx = session.frame_context()
        clock = MediaClock()

        video_format = config.video_format
        publish_audio_format = config.publish_audio_format

        source = self._source_override or self._build_source(session, ctx, clock)
        publisher = self._sink_override or self._build_publisher(
            ctx, clock, video_format, publish_audio_format
        )

        echo_guard = EchoGuard(
            per_participant_audio=config.per_participant_audio,
            hangover_ms=config.echo_gate_hangover_ms,
            metrics=self._metrics,
        )
        echo_guard.set_own_participant(publisher.own_participant())

        avatar = AvatarClient(
            transport=WebSocketAvatarTransport(
                url=config.avatar_url,
                ctx=ctx,
                clock=clock,
                send_queue_size=config.avatar_send_queue_size,
                open_timeout_s=config.avatar_connect_timeout_s,
                metrics=self._metrics,
            ),
            ctx=ctx,
            policy=ReconnectPolicy(
                initial_delay_s=config.avatar_reconnect_initial_delay_s,
                max_delay_s=config.avatar_reconnect_max_delay_s,
                max_attempts=config.avatar_reconnect_max_attempts,
            ),
            metrics=self._metrics,
        )

        decode = DecodePipeline(
            decoder=FfmpegDecoder(
                ctx=ctx,
                clock=clock,
                video_format=video_format,
                audio_format=publish_audio_format,
                metrics=self._metrics,
            ),
            ctx=ctx,
            metrics=self._metrics,
        )

        idle = self._build_idle(ctx, video_format, publish_audio_format)

        pacer = Pacer(
            ctx=ctx,
            clock=clock,
            sink=publisher,
            idle=idle,
            video_format=video_format,
            audio_format=publish_audio_format,
            echo_guard=echo_guard,
            video_queue_size=config.video_queue_size,
            audio_queue_size=config.audio_queue_size,
            metrics=self._metrics,
        )

        router = MediaRouter(
            ctx=ctx,
            clock=clock,
            source=source,
            avatar=avatar,
            decode=decode,
            pacer=pacer,
            echo_guard=echo_guard,
            metrics=self._metrics,
        )

        return ZoomMeetingSession(
            session=session, clock=clock, source=source, publisher=publisher, router=router
        )

    # -- component builders ------------------------------------------------

    def _build_source(
        self, session: SessionContext, ctx: object, clock: MediaClock
    ) -> AudioSource:
        meeting_uuid, stream_id, signaling_url = rtms_attachment(session.meeting)
        signature = build_signature(
            client_id=self._config.client_id,
            client_secret=self._config.client_secret,
            meeting_uuid=meeting_uuid,
            rtms_stream_id=stream_id,
        )
        return RtmsAudioSource(
            meeting_uuid=meeting_uuid,
            rtms_stream_id=stream_id,
            signaling_url=signaling_url,
            signature=signature,
            ctx=ctx,  # type: ignore[arg-type]
            clock=clock,
            queue_size=self._config.inbound_queue_size,
            send_rate_ms=self._config.rtms_send_rate_ms,
            per_participant_audio=self._config.per_participant_audio,
            metrics=self._metrics,
        )

    def _build_publisher(
        self,
        ctx: object,
        clock: MediaClock,
        video_format: VideoFormat,
        audio_format: AudioFormat,
    ) -> MediaSink:
        return MeetingPublisher(
            client=SidecarUdsClient(
                uds_path=self._config.sidecar_uds_path,
                connect_timeout_s=self._config.sidecar_connect_timeout_s,
            ),
            jwt_factory=SdkJwtFactory(
                sdk_key=self._config.sdk_key, sdk_secret=self._config.sdk_secret
            ),
            ctx=ctx,  # type: ignore[arg-type]
            clock=clock,
            video_format=video_format,
            audio_format=audio_format,
            policy=ReconnectPolicy(max_attempts=self._config.sidecar_reconnect_max_attempts),
            metrics=self._metrics,
        )

    def _build_idle(
        self, ctx: object, video_format: VideoFormat, audio_format: AudioFormat
    ) -> IdleFrameSource:
        clip_path = self._config.idle_clip_path
        if clip_path is not None and Path(clip_path).exists():
            return IdleFrameSource.from_raw_clip(
                Path(clip_path),
                ctx=ctx,  # type: ignore[arg-type]
                video_format=video_format,
                audio_format=audio_format,
            )
        return IdleFrameSource(
            ctx=ctx,  # type: ignore[arg-type]
            video_format=video_format,
            audio_format=audio_format,
        )


def unhealthy(name: str, detail: str) -> ComponentHealth:
    """Convenience for reporting a component we could not even construct."""
    return ComponentHealth.unhealthy(name, detail)
