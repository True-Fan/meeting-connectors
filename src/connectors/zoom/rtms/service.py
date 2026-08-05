"""RTMSService — the two-socket RTMS attach sequence.

Sequence (doc 003 §3.2), all of it Zoom-specific and none of it visible outside this
package::

    signaling: connect  -> msg 1 (signature)   <- msg 2 (media url)
    media:     connect  -> msg 3 (media_params) <- msg 4 (status 0)
    signaling: -> msg 7 CLIENT_READY_ACK
    media:     <- msg 14 audio frames ...

Both sockets answer ``KEEP_ALIVE_REQ (12)`` with ``KEEP_ALIVE_RESP (13)``.

The service owns connection state and translation; it does **not** own retry. Retry
belongs to ``RtmsAudioSource``, which is the component the supervisor watches — that
keeps "how to attach" and "when to try again" as separate reasons to change.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any

from src.connectors.zoom.exceptions import (
    KeepAliveTimeoutError,
    RtmsConnectionError,
    RtmsHandshakeError,
    RtmsProtocolError,
)
from src.connectors.zoom.rtms.enums import RtmsEventType, RtmsMessageType, RtmsStatusCode
from src.connectors.zoom.rtms.keepalive import KeepAliveWatchdog
from src.connectors.zoom.rtms.mapping import (
    build_audio_params,
    negotiated_audio_format,
    to_audio_frame,
)
from src.connectors.zoom.rtms.models import (
    ClientReadyAck,
    DataHandshakeRequest,
    DataHandshakeResponse,
    KeepAliveRequest,
    KeepAliveResponse,
    MediaDataAudio,
    MediaParams,
    SignalingHandshakeRequest,
    SignalingHandshakeResponse,
)
from src.connectors.zoom.rtms.transport import JsonWebSocket, WebSocketTransport
from src.domain.context import FrameContext
from src.domain.media import AudioFormat, AudioFrame
from src.infrastructure.logging import get_logger
from src.infrastructure.metrics import MetricName, MetricsCollector
from src.services.media.clock import MediaClock
from src.services.media.queues import BoundedFrameQueue

logger = get_logger(__name__)

TransportFactory = Any
"""``async (url: str) -> JsonWebSocket``. Injected so tests can supply an in-memory
transport and drive the real handshake without a socket."""


async def _default_transport_factory(url: str) -> JsonWebSocket:
    return await WebSocketTransport.open(url)


class RtmsService:
    """One RTMS attachment: signaling socket, media socket, and audio translation."""

    __slots__ = (
        "_audio_format",
        "_audio_params",
        "_clock",
        "_ctx",
        "_keepalive_timeout_s",
        "_media",
        "_media_watchdog",
        "_meeting_uuid",
        "_metrics",
        "_queue",
        "_signaling",
        "_signaling_watchdog",
        "_signature",
        "_stream_id",
        "_transport_factory",
    )

    def __init__(
        self,
        *,
        meeting_uuid: str,
        rtms_stream_id: str,
        signature: str,
        ctx: FrameContext,
        clock: MediaClock,
        queue: BoundedFrameQueue[AudioFrame],
        send_rate_ms: int = 20,
        per_participant_audio: bool = True,
        metrics: MetricsCollector | None = None,
        keepalive_timeout_s: float | None = None,
        transport_factory: TransportFactory | None = None,
    ) -> None:
        self._meeting_uuid = meeting_uuid
        self._stream_id = rtms_stream_id
        self._signature = signature
        self._ctx = ctx
        self._clock = clock
        self._queue = queue
        self._metrics = metrics
        self._transport_factory = transport_factory or _default_transport_factory
        self._keepalive_timeout_s = keepalive_timeout_s

        self._audio_params = build_audio_params(
            send_rate_ms=send_rate_ms, per_participant=per_participant_audio
        )
        self._audio_format = negotiated_audio_format(self._audio_params)

        self._signaling: JsonWebSocket | None = None
        self._media: JsonWebSocket | None = None
        self._signaling_watchdog = self._new_watchdog("signaling")
        self._media_watchdog = self._new_watchdog("media")

    def _new_watchdog(self, name: str) -> KeepAliveWatchdog:
        if self._keepalive_timeout_s is None:
            return KeepAliveWatchdog(name=name)
        return KeepAliveWatchdog(name=name, timeout_s=self._keepalive_timeout_s)

    @property
    def audio_format(self) -> AudioFormat:
        """The format RTMS was asked to deliver, as a domain type."""
        return self._audio_format

    @property
    def is_attached(self) -> bool:
        return self._signaling is not None and self._media is not None

    # -- attach ------------------------------------------------------------

    async def attach(self, signaling_url: str) -> None:
        """Run the full attach sequence.

        Raises:
            RtmsHandshakeError: a handshake was rejected.
            RtmsConnectionError: a socket failed.
            RtmsProtocolError: a reply was malformed.
        """
        self._signaling = await self._transport_factory(signaling_url)
        media_url = await self._signaling_handshake()
        self._media = await self._transport_factory(media_url)
        await self._media_handshake()
        await self._send_client_ready()

        logger.info(
            "rtms.attached",
            meeting_uuid=self._meeting_uuid,
            audio_format=str(self._audio_format),
            send_rate_ms=self._audio_params.send_rate,
            per_participant=self._audio_params.data_opt,
        )

    async def _signaling_handshake(self) -> str:
        assert self._signaling is not None
        request = SignalingHandshakeRequest(
            meeting_uuid=self._meeting_uuid,
            rtms_stream_id=self._stream_id,
            signature=self._signature,
            # Zoom's samples use a random sequence; it is echoed, not validated.
            sequence=random.randint(1, 2**31 - 1),
        )
        await self._signaling.send_json(request.model_dump())

        raw = await self._signaling.recv_json()
        response = SignalingHandshakeResponse.model_validate(raw)

        if response.msg_type != RtmsMessageType.SIGNALING_HAND_SHAKE_RESP:
            raise RtmsProtocolError(
                f"expected msg_type {RtmsMessageType.SIGNALING_HAND_SHAKE_RESP}, "
                f"got {response.msg_type}"
            )
        if response.status_code != RtmsStatusCode.OK:
            raise RtmsHandshakeError("signaling", response.status_code, response.reason)

        media_url = response.media_url()
        if not media_url:
            raise RtmsProtocolError("signaling handshake returned no media server url")
        return media_url

    async def _media_handshake(self) -> None:
        assert self._media is not None
        request = DataHandshakeRequest(
            meeting_uuid=self._meeting_uuid,
            rtms_stream_id=self._stream_id,
            signature=self._signature,
            media_params=MediaParams(audio=self._audio_params),
        )
        await self._media.send_json(request.model_dump())

        raw = await self._media.recv_json()
        response = DataHandshakeResponse.model_validate(raw)

        if response.msg_type != RtmsMessageType.DATA_HAND_SHAKE_RESP:
            raise RtmsProtocolError(
                f"expected msg_type {RtmsMessageType.DATA_HAND_SHAKE_RESP}, "
                f"got {response.msg_type}"
            )
        if response.status_code != RtmsStatusCode.OK:
            raise RtmsHandshakeError("media", response.status_code, response.reason)

    async def _send_client_ready(self) -> None:
        assert self._signaling is not None
        ack = ClientReadyAck(rtms_stream_id=self._stream_id)
        await self._signaling.send_json(ack.model_dump())

    # -- run ---------------------------------------------------------------

    async def run(self) -> None:
        """Pump both sockets until one fails or the service is detached.

        Raises:
            RtmsConnectionError | KeepAliveTimeoutError | RtmsProtocolError:
                propagated so the owner can reconnect.
        """
        if self._signaling is None or self._media is None:
            raise RtmsConnectionError("run() called before attach()")

        async with asyncio.TaskGroup() as group:
            group.create_task(self._pump_signaling(), name="rtms-signaling")
            group.create_task(self._pump_media(), name="rtms-media")
            group.create_task(self._watch_keepalive(), name="rtms-keepalive")

    async def _pump_signaling(self) -> None:
        assert self._signaling is not None
        async for message in self._signaling.messages():
            self._signaling_watchdog.note_activity()
            await self._handle_signaling(message)

    async def _pump_media(self) -> None:
        assert self._media is not None
        async for message in self._media.messages():
            self._media_watchdog.note_activity()
            await self._handle_media(message)

    async def _watch_keepalive(self) -> None:
        """Fail the connection when a socket goes silent.

        Polls rather than arming timers per message: at 50 messages/second a
        rescheduled timer per frame is pure overhead, and one-second resolution is
        ample against a 60-second window.
        """
        while True:
            await asyncio.sleep(1.0)
            for watchdog in (self._signaling_watchdog, self._media_watchdog):
                if watchdog.is_expired():
                    raise KeepAliveTimeoutError(
                        f"no RTMS traffic for {watchdog.seconds_since_activity():.1f}s"
                    )

    # -- message handling --------------------------------------------------

    async def _handle_signaling(self, message: dict[str, Any]) -> None:
        msg_type = message.get("msg_type")

        if msg_type == RtmsMessageType.KEEP_ALIVE_REQ:
            await self._answer_keepalive(self._signaling, self._signaling_watchdog, message)
        elif msg_type == RtmsMessageType.EVENT_UPDATE:
            self._log_event(message)
        elif msg_type in (
            RtmsMessageType.STREAM_STATE_UPDATE,
            RtmsMessageType.SESSION_STATE_UPDATE,
        ):
            logger.info("rtms.state_update", msg_type=msg_type, state=message.get("state"))

    async def _handle_media(self, message: dict[str, Any]) -> None:
        msg_type = message.get("msg_type")

        if msg_type == RtmsMessageType.MEDIA_DATA_AUDIO:
            self._enqueue_audio(message)
        elif msg_type == RtmsMessageType.KEEP_ALIVE_REQ:
            await self._answer_keepalive(self._media, self._media_watchdog, message)
        elif msg_type == RtmsMessageType.EVENT_UPDATE:
            self._log_event(message)

    def _enqueue_audio(self, message: dict[str, Any]) -> None:
        wire = MediaDataAudio.model_validate(message)
        try:
            frame = to_audio_frame(
                wire, audio_format=self._audio_format, ctx=self._ctx, clock=self._clock
            )
        except RtmsProtocolError as exc:
            # One malformed frame must not tear down a live meeting. Count it and
            # keep going — this is lossy input, not a broken contract.
            logger.warning("rtms.audio.malformed", error=str(exc))
            if self._metrics is not None:
                self._metrics.increment(
                    MetricName.FRAMES_DROPPED_TOTAL,
                    ctx=self._ctx,
                    stage="rtms_decode",
                    reason="malformed",
                )
            return

        self._queue.put(frame, ctx=self._ctx, reason="ingest_overflow")
        if self._metrics is not None:
            self._metrics.increment(
                MetricName.FRAMES_RECEIVED_TOTAL, ctx=self._ctx, kind="audio"
            )

    async def _answer_keepalive(
        self,
        socket: JsonWebSocket | None,
        watchdog: KeepAliveWatchdog,
        message: dict[str, Any],
    ) -> None:
        if socket is None:
            return
        request = KeepAliveRequest.model_validate(message)
        await socket.send_json(KeepAliveResponse(timestamp=request.timestamp).model_dump())
        watchdog.note_request()

    def _log_event(self, message: dict[str, Any]) -> None:
        raw_type = message.get("event_type")
        try:
            event_type: RtmsEventType | int = RtmsEventType(raw_type)
        except ValueError:
            event_type = raw_type if isinstance(raw_type, int) else -1
        name = event_type.name if isinstance(event_type, RtmsEventType) else str(event_type)
        logger.info(
            "rtms.event",
            event_type=name,
            user_id=message.get("user_id"),
            user_name=message.get("user_name"),
        )

    # -- teardown ----------------------------------------------------------

    async def detach(self) -> None:
        """Close both sockets. Idempotent."""
        for socket in (self._media, self._signaling):
            if socket is not None:
                await socket.close()
        self._media = None
        self._signaling = None
