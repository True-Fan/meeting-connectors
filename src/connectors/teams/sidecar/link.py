"""TeamsSidecarLink — one media session, shared by ingest and egress.

**The structural difference from Zoom, and the reason this class exists.** Zoom has two
independent integrations: RTMS carries audio in over its own WebSocket, and the Meeting
SDK sidecar carries audio and video out over its own socket. Either can fail and
recover without touching the other, which is why ``ZoomMeetingSession`` holds two
separately-supervised legs.

Teams has one. The ``Microsoft.Graph.Communications.Calls.Media`` platform owns receive
*and* send inside a single ``LocalMediaSession`` bound to a single Graph call, so
participant audio and the avatar's audio/video traverse the same link and share one
fate. Splitting them into two independently-recovering legs would be a lie the health
report told the operator.

So this object owns the connection, the join, the roster, and recovery; the
``AudioSource`` and ``MediaSink`` adapters are thin views onto it. Both legs report the
link's health, which is honest: when the link goes, both are gone.

Recovery is a **full rejoin**. Doc 002 §2.2 predicted this and called it
``ReconnectScope.FULL``; the enum was correctly cut in doc 003 §0.1, and what survives
is the behaviour — a reconnect here re-creates the Graph call, because a media session
cannot be re-attached to a call whose signalling has already gone.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress

from src.connectors.teams.config import TeamsConnectorConfig
from src.connectors.teams.exceptions import (
    SidecarFatalError,
    SidecarProtocolError,
    SidecarUnavailableError,
    TeamsSidecarError,
)
from src.connectors.teams.graph.join_url import resolve_join_descriptor
from src.connectors.teams.graph.models import (
    ParticipantInfo,
    SidecarError,
    SidecarReady,
)
from src.connectors.teams.ingest.mapping import to_audio_frame, to_participant_ref
from src.connectors.teams.sidecar.protocol import (
    CallState,
    TeamsMessage,
    TeamsMessageType,
    encode_audio,
    encode_video,
)
from src.connectors.teams.sidecar.tcp_client import TeamsSidecarClient, build_ssl_context
from src.domain.context import FrameContext
from src.domain.health import ComponentHealth, ComponentState
from src.domain.media import AudioFrame, VideoFrame
from src.domain.meeting import MeetingContext, ParticipantRef
from src.infrastructure.logging import get_logger
from src.infrastructure.metrics import MetricName, MetricsCollector
from src.infrastructure.reconnect import ReconnectPolicy
from src.services.media.clock import MediaClock
from src.services.media.queues import BoundedFrameQueue, OverflowPolicy

logger = get_logger(__name__)

COMPONENT_NAME = "teams_media_link"

VIDEO_BACKPRESSURE_BYTES = 4 * 1024 * 1024
"""Above this queued-write depth, video frames are dropped rather than queued. Roughly
one 1080p frame of slack — enough to absorb a hiccup, not a backlog."""

ClientFactory = Callable[[], TeamsSidecarClient]

ParticipantListener = Callable[[ParticipantRef], None]


class TeamsSidecarLink:
    """The link to the Windows media sidecar for one meeting."""

    __slots__ = (
        "_audio_queue",
        "_audio_seq",
        "_call_state",
        "_client",
        "_client_factory",
        "_clock",
        "_config",
        "_ctx",
        "_detail",
        "_dropped_audio",
        "_dropped_video",
        "_listeners",
        "_meeting",
        "_metrics",
        "_own_participant",
        "_policy",
        "_ready",
        "_reconnects",
        "_roster",
        "_state",
        "_task",
        "_video_seq",
    )

    def __init__(
        self,
        *,
        config: TeamsConnectorConfig,
        ctx: FrameContext,
        clock: MediaClock,
        metrics: MetricsCollector | None = None,
        policy: ReconnectPolicy | None = None,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self._config = config
        self._ctx = ctx
        self._clock = clock
        self._metrics = metrics
        self._policy = policy or ReconnectPolicy(
            max_attempts=config.sidecar_reconnect_max_attempts
        )
        # Injectable so the entire Teams pipeline can be exercised against an
        # in-process fake sidecar — no Windows host, no Azure tenant, no Graph consent.
        # That is what makes this connector developable on the machines we have.
        self._client_factory = client_factory or self._default_client_factory

        self._client: TeamsSidecarClient | None = None
        self._meeting: MeetingContext | None = None
        self._ready: SidecarReady | None = None
        self._call_state: CallState | None = None
        self._own_participant: ParticipantRef | None = None
        self._roster: dict[int, ParticipantRef] = {}
        self._listeners: list[ParticipantListener] = []

        self._audio_queue: BoundedFrameQueue[AudioFrame] = BoundedFrameQueue(
            name="teams_inbound",
            maxsize=config.inbound_queue_size,
            policy=OverflowPolicy.DROP_OLDEST,
            metrics=metrics,
        )

        self._state = ComponentState.UNKNOWN
        self._detail: str | None = None
        self._task: asyncio.Task[None] | None = None
        self._audio_seq = 0
        self._video_seq = 0
        self._dropped_video = 0
        self._dropped_audio = 0
        self._reconnects = 0

    # -- lifecycle ---------------------------------------------------------

    async def start(self, meeting: MeetingContext) -> None:
        """Connect, join the meeting, and begin receiving.

        The first join happens **inline** rather than in the background, deliberately:
        a rejected credential, an unconsented permission, or an unparseable join URL
        then fails ``POST /sessions`` with a precise reason, instead of returning 202
        and leaving an operator to discover from a health endpoint that the avatar
        never arrived.

        Raises:
            SidecarFatalError: unrecoverable — bad credentials, missing consent,
                media platform failure.
            SidecarUnavailableError: the sidecar is unreachable.
            JoinUrlError: the meeting cannot be resolved into a Graph join.
        """
        if self._task is not None:
            return
        self._meeting = meeting
        await self._connect_and_join(meeting)
        self._task = asyncio.create_task(self._supervise(), name="teams-link")

    async def stop(self) -> None:
        """Leave the call and close the link. Idempotent."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            # We cancelled it deliberately; its cancellation is not an error.
            with suppress(asyncio.CancelledError):
                await task

        await self._leave_and_close()
        self._audio_queue.close()
        self._state = ComponentState.UNKNOWN
        self._detail = "stopped"
        self._own_participant = None
        self._roster.clear()

    # -- ingest ------------------------------------------------------------

    def audio_queue(self) -> BoundedFrameQueue[AudioFrame]:
        """The queue inbound participant audio is delivered into."""
        return self._audio_queue

    # -- egress ------------------------------------------------------------

    async def publish_audio(self, frame: AudioFrame) -> None:
        """Send one PCM frame to the media platform. Always drains.

        Unlike Zoom's publisher this **absorbs** a transport failure rather than
        raising. The pacer runs a continuous cadence and is the same shared component
        for both connectors; letting an error escape would tear its task group down
        mid-reconnect, when this link is seconds away from healing itself. A dropped
        frame is counted and the leg is marked degraded, which is what the supervisor
        acts on.
        """
        client = self._client
        if client is None or not client.is_connected:
            self._count_drop(frame_ctx=frame.ctx, kind="audio", reason="link_down")
            return

        started = self._clock.now_us()
        payload = encode_audio(frame, seq=self._audio_seq)
        self._audio_seq = (self._audio_seq + 1) % (2**32)
        try:
            await client.send_raw(payload, drain=True)
        except SidecarUnavailableError as exc:
            self._degrade(f"audio write failed: {exc}")
            self._count_drop(frame_ctx=frame.ctx, kind="audio", reason="write_failed")
            return
        self._observe_ipc(started, frame, kind="audio")

    async def publish_video(self, frame: VideoFrame) -> None:
        """Send one I420 frame, dropping it under backpressure.

        The sidecar converts I420 to the NV12 the media platform requires while
        copying into its send buffer (doc 005 §4.1) — it makes that copy regardless,
        so the interleave costs almost nothing there, whereas a per-frame 1.4 MB byte
        shuffle in Python would sit directly in the event loop.
        """
        client = self._client
        if client is None or not client.is_connected:
            self._count_drop(frame_ctx=frame.ctx, kind="video", reason="link_down")
            return

        if client.write_buffer_size() > VIDEO_BACKPRESSURE_BYTES:
            self._dropped_video += 1
            self._degrade("dropping video under link backpressure")
            self._count_drop(frame_ctx=frame.ctx, kind="video", reason="backpressure")
            return

        started = self._clock.now_us()
        payload = encode_video(frame, seq=self._video_seq)
        self._video_seq = (self._video_seq + 1) % (2**32)
        try:
            # drain=False: a video frame must not stall the pacer's audio loop.
            await client.send_raw(payload, drain=False)
        except SidecarUnavailableError as exc:
            self._degrade(f"video write failed: {exc}")
            self._count_drop(frame_ctx=frame.ctx, kind="video", reason="write_failed")
            return
        self._observe_ipc(started, frame, kind="video")

    # -- observation -------------------------------------------------------

    def health(self) -> ComponentHealth:
        client = self._client
        if self._state is ComponentState.HEALTHY and (client is None or not client.is_connected):
            return ComponentHealth.unhealthy(COMPONENT_NAME, "sidecar link disconnected")
        return ComponentHealth(name=COMPONENT_NAME, state=self._state, detail=self._detail)

    def own_participant(self) -> ParticipantRef | None:
        """The identity the bot joined as, once the roster has reported it.

        Arrives asynchronously — the Graph roster lands after the call is established,
        where Zoom's sidecar reports its participant id in ``READY``. ``EchoGuard``'s
        speaking gate is what covers the window in between, which is exactly the
        second defence layer it was built to be.
        """
        return self._own_participant

    def add_participant_listener(self, listener: ParticipantListener) -> None:
        """Register a callback for when the bot's own identity becomes known."""
        self._listeners.append(listener)
        if self._own_participant is not None:
            listener(self._own_participant)

    @property
    def ready(self) -> SidecarReady | None:
        """The negotiated media parameters, once joined."""
        return self._ready

    @property
    def call_state(self) -> CallState | None:
        return self._call_state

    @property
    def is_joined(self) -> bool:
        return self._ready is not None and self._state.is_serving

    @property
    def reconnects(self) -> int:
        return self._reconnects

    @property
    def stats(self) -> dict[str, int]:
        return {
            "dropped_video": self._dropped_video,
            "dropped_audio": self._dropped_audio,
            "reconnects": self._reconnects,
            "roster": len(self._roster),
        }

    # -- join --------------------------------------------------------------

    async def _connect_and_join(self, meeting: MeetingContext) -> None:
        """Open the link and drive the join to ``READY``."""
        self._config.require_configured()
        descriptor = resolve_join_descriptor(
            meeting, tenant_id=self._config.tenant_id, display_name=meeting.display_name
        )

        client = self._client_factory()
        await client.connect()
        self._client = client

        body = {
            "sessionId": self._ctx.session_id,
            "correlationId": self._ctx.correlation_id,
            "join": descriptor.to_wire(),
            # Credentials travel per join rather than living in the Windows image, so
            # rotating a client secret is a bridge-side config change and the sidecar
            # holds nothing durable worth stealing.
            "auth": {
                "tenantId": self._config.tenant_id,
                "clientId": self._config.client_id,
                "clientSecret": self._config.client_secret.get_secret_value(),
            },
            "audio": {
                "sampleRateHz": self._config.publish_audio_format.sample_rate_hz,
                "channels": self._config.publish_audio_format.channels,
                "unmixed": self._config.unmixed_audio,
            },
            "video": {
                "width": self._config.video_format.width,
                "height": self._config.video_format.height,
                "fps": self._config.video_format.fps,
            },
        }
        await client.send_json(TeamsMessageType.CONTROL_JOIN, body)

        message = await client.await_message(
            TeamsMessageType.READY, timeout_s=self._config.sidecar_ready_timeout_s
        )
        ready = SidecarReady.model_validate(message.json())
        self._verify_negotiation(ready)

        self._ready = ready
        self._call_state = CallState.ESTABLISHED
        self._state = ComponentState.HEALTHY
        self._detail = None
        if ready.self_msi is not None:
            self._record_own_participant(
                ParticipantRef(user_id=ready.self_msi, display_name=meeting.display_name)
            )

        logger.info(
            "teams_link.ready",
            call_id=ready.call_id,
            join_mode=descriptor.mode,
            sdk_version=ready.sdk_version,
            unmixed_audio=ready.unmixed_audio,
            negotiated_video=f"{ready.video_width}x{ready.video_height}@{ready.video_fps}",
            negotiated_audio=f"{ready.audio_sample_rate_hz}Hz/{ready.audio_channels}ch",
        )

    def _verify_negotiation(self, ready: SidecarReady) -> None:
        """Reject a join whose negotiated media does not match what we configured.

        A silent mismatch is the expensive failure here: the wrong send rate produces
        chipmunk audio and the wrong geometry produces a garbled frame, both of which
        look like a decoder bug from the bridge's side and cost hours to trace back
        across a host boundary.

        Raises:
            SidecarFatalError: negotiated parameters differ from the request.
        """
        expected_audio = self._config.publish_audio_format
        if ready.audio_sample_rate_hz != expected_audio.sample_rate_hz:
            raise SidecarFatalError(
                "AUDIO_FORMAT_MISMATCH",
                f"requested {expected_audio.sample_rate_hz} Hz, "
                f"sidecar negotiated {ready.audio_sample_rate_hz} Hz",
            )
        video = self._config.video_format
        if (ready.video_width, ready.video_height) != (video.width, video.height):
            raise SidecarFatalError(
                "VIDEO_FORMAT_MISMATCH",
                f"requested {video.width}x{video.height}, "
                f"sidecar negotiated {ready.video_width}x{ready.video_height}",
            )
        if self._config.unmixed_audio and not ready.unmixed_audio:
            # Not fatal: the meeting still works, but EchoGuard loses its identity
            # filter and must lean on the speaking gate alone. Worth a loud warning.
            logger.warning(
                "teams_link.unmixed_audio_unavailable",
                detail="per-participant audio was requested but not granted; "
                "echo suppression falls back to the speaking gate",
            )

    async def _leave_and_close(self) -> None:
        client, self._client = self._client, None
        if client is None:
            return
        if client.is_connected:
            # Best-effort: if the sidecar already died there is nothing to tell it, and
            # failing here would block session teardown.
            with suppress(TeamsSidecarError):
                await client.send_json(
                    TeamsMessageType.CONTROL_LEAVE, {"reason": "session_stop"}
                )
        await client.close()
        self._ready = None

    # -- supervision -------------------------------------------------------

    async def _supervise(self) -> None:
        """Read until the link fails, then rejoin. Repeats until told to stop."""
        while True:
            try:
                await self._read_loop()
                # A clean EOF still means we have stopped hearing and stopped being
                # seen, so it is a failure to recover from, not a reason to exit.
                raise SidecarUnavailableError("sidecar closed the link")
            except asyncio.CancelledError:
                await self._leave_and_close()
                raise
            except SidecarFatalError as exc:
                self._fail(str(exc))
                return
            except (SidecarUnavailableError, SidecarProtocolError, OSError) as exc:
                if not await self._rejoin(exc):
                    return

    async def _rejoin(self, cause: Exception) -> bool:
        """Reconnect and re-create the Graph call, with backoff.

        The retry loop lives here rather than in ``_supervise`` so that a *failed* rejoin
        counts as an attempt and retries the join, instead of falling back into
        ``_read_loop`` on a link that was never established — which would block on a
        socket with nothing behind it and leave the reconnect budget permanently
        unspent, so the leg would sit UNHEALTHY forever without ever being declared
        failed. Same shape as Zoom's ``MeetingPublisher.reconnect``, deliberately.

        Returns:
            True once rejoined, False when the budget is spent or the failure is fatal.
        """
        meeting = self._meeting
        if meeting is None:  # pragma: no cover - start() always sets it
            self._fail("no meeting context to rejoin with")
            return False

        self._state = ComponentState.UNHEALTHY
        self._detail = str(cause)

        attempt = 0
        while True:
            attempt += 1
            if self._policy.exhausted(attempt):
                self._fail(
                    f"reconnect budget exhausted after {attempt - 1} attempts: {cause}"
                )
                return False

            await self._leave_and_close()
            delay = await self._policy.sleep(attempt)
            logger.warning(
                "teams_link.reconnecting",
                attempt=attempt,
                delay_s=round(delay, 3),
                error=str(cause),
            )

            try:
                await self._connect_and_join(meeting)
            except SidecarFatalError as fatal:
                self._fail(str(fatal))
                return False
            except (TeamsSidecarError, OSError) as retry_error:
                self._detail = str(retry_error)
                continue

            # Buffered audio is stale by definition; replaying it would burst.
            self._audio_queue.clear()
            self._reconnects += 1
            if self._metrics is not None:
                self._metrics.increment(
                    MetricName.RECONNECTS_TOTAL, ctx=self._ctx, component=COMPONENT_NAME
                )
            logger.warning(
                "teams_link.reconnected",
                attempts=attempt,
                note="the Graph call was re-created; audio during the gap is lost",
            )
            return True

    async def _read_loop(self) -> None:
        """Demultiplex inbound messages until EOF."""
        client = self._client
        if client is None:
            raise SidecarUnavailableError("Teams sidecar is not connected")

        async for message in client.messages():
            await self._dispatch(message, client)

    async def _dispatch(self, message: TeamsMessage, client: TeamsSidecarClient) -> None:
        match message.msg_type:
            case TeamsMessageType.AUDIO_PCM:
                self._on_audio(message)
            case TeamsMessageType.ROSTER:
                self._on_roster(message)
            case TeamsMessageType.CALL_STATE:
                self._on_call_state(message)
            case TeamsMessageType.HEARTBEAT:
                body = message.json()
                await client.send_json(
                    TeamsMessageType.HEARTBEAT, {"sent_at_us": body.get("sent_at_us", 0)}
                )
            case TeamsMessageType.ERROR:
                self._on_error(message)
            case TeamsMessageType.READY:
                # A second READY means the sidecar re-established the call on its own
                # side. Adopt the new parameters rather than ignoring them.
                self._ready = SidecarReady.model_validate(message.json())
                logger.info("teams_link.re_ready", call_id=self._ready.call_id)
            case _:
                # VIDEO_I420 and the control messages we send are not expected back.
                logger.warning("teams_link.unexpected_message", msg_type=message.msg_type.name)

    def _on_audio(self, message: TeamsMessage) -> None:
        try:
            frame = to_audio_frame(
                message, ctx=self._ctx, clock=self._clock, roster=self._roster
            )
        except SidecarProtocolError as exc:
            # One malformed frame is not worth tearing the call down for; a persistent
            # fault will show up as silence and as this counter climbing.
            self._dropped_audio += 1
            if self._metrics is not None:
                self._metrics.increment(
                    MetricName.FRAMES_DROPPED_TOTAL,
                    ctx=self._ctx,
                    stage="teams_ingest",
                    reason="malformed",
                )
            logger.warning("teams_link.audio_dropped", error=str(exc))
            return

        self._audio_queue.put(frame, ctx=frame.ctx, reason="ingest_overflow")
        if self._metrics is not None:
            self._metrics.increment(
                MetricName.FRAMES_RECEIVED_TOTAL, ctx=frame.ctx, kind="audio"
            )

    def _on_roster(self, message: TeamsMessage) -> None:
        body = message.json()
        entries = body.get("participants") or []
        roster: dict[int, ParticipantRef] = {}
        own: ParticipantRef | None = None

        for raw in entries:
            if not isinstance(raw, dict):
                continue
            info = ParticipantInfo.model_validate(raw)
            ref = to_participant_ref(info)
            roster[info.msi] = ref
            if info.is_self:
                own = ref

        self._roster = roster
        if own is not None:
            self._record_own_participant(own)
        logger.debug("teams_link.roster", participants=len(roster))

    def _record_own_participant(self, participant: ParticipantRef) -> None:
        if self._own_participant == participant:
            return
        self._own_participant = participant
        logger.info("teams_link.own_participant_known", msi=participant.user_id)
        for listener in self._listeners:
            listener(participant)

    def _on_call_state(self, message: TeamsMessage) -> None:
        body = message.json()
        raw = body.get("state")
        try:
            state = CallState(int(raw))
        except (TypeError, ValueError):
            logger.warning("teams_link.unknown_call_state", state=raw)
            return

        self._call_state = state
        logger.info("teams_link.call_state", state=state.name, reason=body.get("reason"))

        if state is CallState.TERMINATED:
            # The service ended the call — the meeting is over or we were removed.
            # Surfacing it as degraded lets the supervisor's grace window decide,
            # rather than this component unilaterally failing a session that an
            # operator may be about to stop anyway.
            self._degrade(f"call terminated: {body.get('reason') or 'no reason given'}")

    def _on_error(self, message: TeamsMessage) -> None:
        error = SidecarError.model_validate(message.json())
        if error.fatal:
            self._fail(f"[{error.code}] {error.message}")
            self._audio_queue.close()
            return
        logger.warning("teams_link.error", code=error.code, message=error.message)
        self._degrade(f"[{error.code}] {error.message}")

    # -- state helpers -----------------------------------------------------

    def _degrade(self, detail: str) -> None:
        """Mark impaired-but-serving, without clobbering a hard failure."""
        if self._state is ComponentState.UNHEALTHY:
            return
        self._state = ComponentState.DEGRADED
        self._detail = detail

    def _fail(self, detail: str) -> None:
        self._state = ComponentState.UNHEALTHY
        self._detail = detail
        logger.error("teams_link.failed", detail=detail)

    def _count_drop(self, *, frame_ctx: FrameContext, kind: str, reason: str) -> None:
        if kind == "audio":
            self._dropped_audio += 1
        if self._metrics is not None:
            self._metrics.increment(
                MetricName.FRAMES_DROPPED_TOTAL,
                ctx=frame_ctx,
                stage=f"teams_{kind}",
                reason=reason,
            )

    def _observe_ipc(
        self, started_us: int, frame: AudioFrame | VideoFrame, *, kind: str
    ) -> None:
        if self._metrics is not None:
            self._metrics.observe(
                MetricName.SIDECAR_IPC_US,
                self._clock.now_us() - started_us,
                ctx=frame.ctx,
                kind=kind,
            )

    def _default_client_factory(self) -> TeamsSidecarClient:
        ssl_context = (
            build_ssl_context(
                ca_file=self._config.sidecar_ca_file,
                client_cert_file=self._config.sidecar_client_cert_file,
                client_key_file=self._config.sidecar_client_key_file,
            )
            if self._config.sidecar_tls_enabled
            else None
        )
        return TeamsSidecarClient(
            host=self._config.sidecar_host,
            port=self._config.sidecar_port,
            connect_timeout_s=self._config.sidecar_connect_timeout_s,
            ssl_context=ssl_context,
        )
