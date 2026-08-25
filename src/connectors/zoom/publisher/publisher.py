"""MeetingPublisher — the ``MediaSink`` port over the Meeting SDK sidecar.

RTMS cannot publish media (doc 001 §1.2), so this is the entire outbound half of the
bridge. It owns the Python side: JWT minting, join handshake, frame encoding, reconnect,
and health. The sidecar owns exactly one thing — handing frames to the Zoom SDK.

Three behaviours worth calling out:

**Secrets stay in Python.** The short-lived JWT is minted here and handed over in
``CONTROL_JOIN``, so the C++ binary never holds a long-lived credential (spec §5.3).

**A missing raw-data licence fails loudly at join.** ``READY`` reports the
``HasRawdataLicense()`` probe; ``false`` raises rather than letting the session run and
publish nothing (doc 003 §7.1).

**Video is droppable, audio is not.** Under socket backpressure video frames are
discarded and counted; audio always waits for the drain (spec §6).
"""

from __future__ import annotations

from contextlib import suppress

from src.connectors.zoom.auth.sdk_jwt import SdkJwtFactory
from src.connectors.zoom.exceptions import (
    SidecarFatalError,
    SidecarUnavailableError,
)
from src.connectors.zoom.publisher.protocol import (
    SidecarMessageType,
    encode_audio,
    encode_video,
)
from src.connectors.zoom.publisher.uds_client import SidecarUdsClient
from src.domain.context import FrameContext
from src.domain.health import ComponentHealth, ComponentState
from src.domain.media import AudioFormat, AudioFrame, VideoFormat, VideoFrame
from src.domain.meeting import MeetingContext, ParticipantRef
from src.infrastructure.logging import get_logger
from src.infrastructure.metrics import MetricName, MetricsCollector
from src.infrastructure.reconnect import ReconnectPolicy
from src.services.media.clock import MediaClock

logger = get_logger(__name__)

COMPONENT_NAME = "meeting_publisher"

VIDEO_BACKPRESSURE_BYTES = 4 * 1024 * 1024
"""Above this queued-write depth, video frames are dropped rather than queued.
Roughly one 1080p frame of slack — enough to absorb a hiccup, not a backlog."""


class MeetingPublisher:
    """Publishes the avatar's audio and video into a Zoom meeting."""

    __slots__ = (
        "_audio_format",
        "_audio_seq",
        "_client",
        "_clock",
        "_ctx",
        "_detail",
        "_dropped_video",
        "_jwt_factory",
        "_meeting",
        "_metrics",
        "_own_participant",
        "_policy",
        "_ready_timeout_s",
        "_state",
        "_video_format",
        "_video_seq",
    )

    def __init__(
        self,
        *,
        client: SidecarUdsClient,
        jwt_factory: SdkJwtFactory,
        ctx: FrameContext,
        clock: MediaClock,
        video_format: VideoFormat,
        audio_format: AudioFormat,
        ready_timeout_s: float = 30.0,
        policy: ReconnectPolicy | None = None,
        metrics: MetricsCollector | None = None,
    ) -> None:
        self._client = client
        self._jwt_factory = jwt_factory
        self._ctx = ctx
        self._clock = clock
        self._video_format = video_format
        self._audio_format = audio_format
        self._ready_timeout_s = ready_timeout_s
        self._policy = policy or ReconnectPolicy()
        self._metrics = metrics

        self._meeting: MeetingContext | None = None
        self._own_participant: ParticipantRef | None = None
        self._state = ComponentState.UNKNOWN
        self._detail: str | None = None
        self._video_seq = 0
        self._audio_seq = 0
        self._dropped_video = 0

    # -- MediaSink ---------------------------------------------------------

    async def start(self, meeting: MeetingContext) -> None:
        """Connect to the sidecar and join the meeting.

        Raises:
            SidecarFatalError: unrecoverable — e.g. no raw-data licence, join rejected.
            SidecarUnavailableError: recoverable transport failure.
        """
        self._meeting = meeting
        await self._client.connect()
        await self._join(meeting)

    async def _join(self, meeting: MeetingContext) -> None:
        jwt = self._jwt_factory.mint(meeting_number=meeting.meeting_number)
        body = {
            "session_id": self._ctx.session_id,
            "correlation_id": self._ctx.correlation_id,
            "meeting_number": meeting.meeting_number,
            "passcode": meeting.passcode or "",
            "display_name": meeting.display_name,
            "sdk_jwt": jwt.token,
            "video": {
                "width": self._video_format.width,
                "height": self._video_format.height,
                "fps": self._video_format.fps,
            },
            "audio": {
                "sample_rate_hz": self._audio_format.sample_rate_hz,
                "channels": self._audio_format.channels,
            },
        }
        await self._client.send_json(SidecarMessageType.CONTROL_JOIN, body)

        ready = await self._client.await_message(
            SidecarMessageType.READY, timeout_s=self._ready_timeout_s
        )
        payload = ready.json()

        if not bool(payload.get("has_raw_data_license", False)):
            raise SidecarFatalError(
                "NO_RAW_DATA_LICENSE",
                "HasRawdataLicense() returned false; the account cannot send raw "
                "audio/video. Failing the join rather than publishing nothing.",
            )

        participant_id = payload.get("participant_id")
        if isinstance(participant_id, int):
            display_name = self._meeting.display_name if self._meeting else None
            self._own_participant = ParticipantRef(
                user_id=participant_id, display_name=display_name
            )

        self._state = ComponentState.HEALTHY
        self._detail = None
        logger.info(
            "publisher.ready",
            sdk_version=payload.get("sdk_version"),
            participant_id=participant_id,
            negotiated_video=payload.get("video"),
            negotiated_audio=payload.get("audio"),
        )

    async def stop(self) -> None:
        """Leave the meeting and disconnect. Idempotent."""
        if self._client.is_connected:
            # Leaving is best-effort: if the sidecar already died there is nothing to
            # tell it, and failing here would block session teardown.
            with suppress(SidecarUnavailableError):
                await self._client.send_json(
                    SidecarMessageType.CONTROL_LEAVE, {"reason": "session_stop"}
                )
        await self._client.close()
        self._state = ComponentState.UNKNOWN
        self._detail = "stopped"
        self._own_participant = None

    async def publish_video(self, frame: VideoFrame) -> None:
        """Publish one I420 frame, dropping it under backpressure."""
        if self._client.write_buffer_size() > VIDEO_BACKPRESSURE_BYTES:
            self._dropped_video += 1
            self._state = ComponentState.DEGRADED
            self._detail = "dropping video under socket backpressure"
            if self._metrics is not None:
                self._metrics.increment(
                    MetricName.FRAMES_DROPPED_TOTAL,
                    ctx=frame.ctx,
                    stage="sidecar_video",
                    reason="backpressure",
                )
            return

        started = self._clock.now_us()
        payload = encode_video(frame, seq=self._video_seq)
        self._video_seq = (self._video_seq + 1) % (2**32)
        # drain=False: a video frame must not stall the pacer's audio loop.
        await self._client.send_raw(payload, drain=False)
        self._observe_ipc(started, frame.ctx, kind="video")

    async def publish_audio(self, frame: AudioFrame) -> None:
        """Publish one PCM frame. Always drains — audio gaps are audible."""
        started = self._clock.now_us()
        payload = encode_audio(frame, seq=self._audio_seq)
        self._audio_seq = (self._audio_seq + 1) % (2**32)
        await self._client.send_raw(payload, drain=True)
        self._observe_ipc(started, frame.ctx, kind="audio")

    def health(self) -> ComponentHealth:
        if not self._client.is_connected and self._state is ComponentState.HEALTHY:
            return ComponentHealth.unhealthy(COMPONENT_NAME, "sidecar disconnected")
        return ComponentHealth(name=COMPONENT_NAME, state=self._state, detail=self._detail)

    def own_participant(self) -> ParticipantRef | None:
        """The identity the bot joined as, once ``READY`` has reported it.

        ``EchoGuard`` uses this to filter the avatar's own audio out of ingest.
        """
        return self._own_participant

    # -- recovery ----------------------------------------------------------

    async def reconnect(self) -> bool:
        """Reconnect to the sidecar and rejoin with a fresh JWT.

        The old token may well have expired during the outage, which is exactly why
        tokens are minted per join rather than per session.

        Returns:
            True on success, False when the budget is exhausted or the failure is fatal.
        """
        meeting = self._meeting
        if meeting is None:
            return False

        await self._client.close()
        self._state = ComponentState.UNHEALTHY

        attempt = 0
        while True:
            attempt += 1
            if self._policy.exhausted(attempt):
                self._detail = f"reconnect budget exhausted after {attempt - 1} attempts"
                logger.error("publisher.reconnect_exhausted", attempts=attempt - 1)
                return False

            delay = await self._policy.sleep(attempt)
            try:
                await self._client.connect()
                await self._join(meeting)
            except SidecarFatalError as exc:
                self._detail = str(exc)
                logger.error("publisher.reconnect_fatal", error=str(exc))
                return False
            except SidecarUnavailableError as exc:
                logger.warning(
                    "publisher.reconnect_failed",
                    attempt=attempt,
                    delay_s=round(delay, 3),
                    error=str(exc),
                )
                continue

            if self._metrics is not None:
                self._metrics.increment(
                    MetricName.RECONNECTS_TOTAL, ctx=self._ctx, component=COMPONENT_NAME
                )
            logger.info("publisher.reconnected", attempts=attempt)
            return True

    # -- internals ---------------------------------------------------------

    def _observe_ipc(self, started_us: int, ctx: FrameContext, *, kind: str) -> None:
        if self._metrics is not None:
            self._metrics.observe(
                MetricName.SIDECAR_IPC_US, self._clock.now_us() - started_us, ctx=ctx, kind=kind
            )

    @property
    def dropped_video(self) -> int:
        return self._dropped_video
