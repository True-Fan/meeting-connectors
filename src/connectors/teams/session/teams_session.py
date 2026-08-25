"""TeamsMeetingSession — composition of one live avatar-in-a-Teams-meeting.

The Teams counterpart to ``connectors/zoom/session/zoom_session.py``, and the place
where the two platforms' shapes visibly differ:

* **Zoom** holds two independently-recovering legs — an RTMS WebSocket for ingest and a
  C++ sidecar over a Unix socket for egress. Either can drop and heal without the
  other noticing.
* **Teams** holds one ``TeamsSidecarLink``: a single Graph call with a single
  ``LocalMediaSession`` on a Windows host carrying both directions. Recovery is a full
  rejoin because a media session cannot outlive its call's signalling.

Everything between the two legs is the *same shared code* — ``AvatarClient``,
``MediaRouter``, ``DecodePipeline``, ``FfmpegDecoder``, ``Pacer``, ``EchoGuard``,
``IdleFrameSource``, ``MediaClock``. That is the payoff: the media pipeline was written
once, for Zoom, and Teams reuses it without a line changing on either side. What Teams
adds is a platform adapter, not a second pipeline.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path

from src.avatar.client import AvatarClient
from src.avatar.ws_transport import WebSocketAvatarTransport
from src.connectors.teams.config import TeamsConnectorConfig
from src.connectors.teams.ingest.audio_source import TeamsAudioSource
from src.connectors.teams.publisher.publisher import TeamsMediaSink
from src.connectors.teams.sidecar.link import ClientFactory, TeamsSidecarLink
from src.domain.context import FrameContext
from src.domain.health import ComponentState, HealthReport
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


class TeamsMeetingSession:
    """One avatar participating in one Microsoft Teams meeting."""

    __slots__ = ("_clock", "_link", "_publisher", "_router", "_session", "_source", "_task")

    def __init__(
        self,
        *,
        session: SessionContext,
        clock: MediaClock,
        link: TeamsSidecarLink,
        source: AudioSource,
        publisher: MediaSink,
        router: MediaRouter,
    ) -> None:
        self._session = session
        self._clock = clock
        self._link = link
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
        """Join the call, then start routing.

        One join covers both directions, so unlike Zoom there is no "publish first,
        ingest may still be waiting" ordering to get right — and no join race to
        resolve, because we initiate the call rather than waiting for the platform to
        notify us (doc 005 §3.4). The link's first join runs inline, so a bad
        credential fails session creation instead of degrading a live session.
        """
        await self._link.start(self._session.meeting)
        await self._source.start()
        self._task = asyncio.create_task(self._router.run(), name="media-router")

    async def stop(self) -> None:
        """Tear down in a fixed order. Idempotent.

        The router first, so nothing is mid-publish; then the link, which leaves the
        call and closes the socket for both legs at once; then the router's queues.
        """
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

        await self._link.stop()
        self._router.close()

    def health(self) -> HealthReport:
        return HealthReport(
            components=(*self._router.health().components, self._publisher.health())
        )

    def leg_states(self) -> tuple[ComponentState, ComponentState]:
        """``(ingest, publish)`` health.

        Both derive from the one link, so the pair moves together. That is the platform
        being reported accurately rather than an abstraction leaking: there is no state
        in which Teams ingest is healthy while Teams egress is not.
        """
        state = self._link.health().state
        return state, state


class TeamsSessionFactory:
    """Builds a fully wired ``TeamsMeetingSession``.

    All concrete-type knowledge for the Teams feature lives here, so ``MeetingService``
    composes sessions without naming Graph, the media platform, or the sidecar.
    Structurally identical in role to ``ZoomSessionFactory`` — which is what lets both
    satisfy ``ConnectorSessionFactory`` and be registered side by side.
    """

    def __init__(
        self,
        *,
        config: TeamsConnectorConfig,
        metrics: MetricsCollector | None = None,
        sink_override: MediaSink | None = None,
        source_override: AudioSource | None = None,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self._config = config
        self._metrics = metrics
        # Overrides mirror ZoomSessionFactory's: they let the whole pipeline run into a
        # FileSink for verification, and let tests substitute fakes, without a second
        # code path. ``client_factory`` additionally allows an in-process fake sidecar,
        # which is how Teams is testable with no Windows host.
        self._sink_override = sink_override
        self._source_override = source_override
        self._client_factory = client_factory

    def build(self, session: SessionContext) -> TeamsMeetingSession:
        config = self._config
        ctx = session.frame_context()
        clock = MediaClock()

        video_format = config.video_format
        publish_audio_format = config.publish_audio_format

        link = TeamsSidecarLink(
            config=config,
            ctx=ctx,
            clock=clock,
            metrics=self._metrics,
            policy=ReconnectPolicy(max_attempts=config.sidecar_reconnect_max_attempts),
            client_factory=self._client_factory,
        )

        source = self._source_override or TeamsAudioSource(link=link)
        publisher = self._sink_override or TeamsMediaSink(link=link)

        echo_guard = EchoGuard(
            # Unmixed audio gives per-participant attribution; without it the guard
            # correctly falls back to strict gating. Capability as data, not a branch.
            per_participant_audio=config.unmixed_audio,
            hangover_ms=config.echo_gate_hangover_ms,
            metrics=self._metrics,
        )
        echo_guard.set_own_participant(publisher.own_participant())
        # Teams learns the bot's identity from the roster, which lands *after* the
        # join — so unlike Zoom the value above is normally ``None`` here. Subscribing
        # closes the loop the moment it is known instead of leaving the identity filter
        # permanently disarmed.
        link.add_participant_listener(echo_guard.set_own_participant)

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

        return TeamsMeetingSession(
            session=session,
            clock=clock,
            link=link,
            source=source,
            publisher=publisher,
            router=router,
        )

    # -- component builders ------------------------------------------------

    def _build_idle(
        self, ctx: FrameContext, video_format: VideoFormat, audio_format: AudioFormat
    ) -> IdleFrameSource:
        """Idle media, so the avatar reads as a person between utterances.

        Teams needs this for the same reason Zoom does (doc 003 §1.4): the video socket
        must be fed at the negotiated cadence, and a frozen tile reads as a broken
        connection rather than as someone listening.
        """
        clip_path = self._config.idle_clip_path
        if clip_path is not None and Path(clip_path).exists():
            return IdleFrameSource.from_raw_clip(
                clip_path, ctx=ctx, video_format=video_format, audio_format=audio_format
            )
        return IdleFrameSource(ctx=ctx, video_format=video_format, audio_format=audio_format)
