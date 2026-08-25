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
from contextlib import suppress
from typing import Any

from pydantic import ValidationError

from src.connectors.zoom.exceptions import (
    KeepAliveTimeoutError,
    RtmsConnectionError,
    RtmsHandshakeError,
    RtmsProtocolError,
)
from src.connectors.zoom.rtms.enums import (
    MediaDataType,
    RtmsEventType,
    RtmsMessageType,
    RtmsStatusCode,
)
from src.connectors.zoom.rtms.keepalive import KeepAliveWatchdog
from src.connectors.zoom.rtms.mapping import (
    build_audio_params,
    negotiated_audio_format,
    to_audio_frame,
    to_chat_message,
    to_participant_events,
    to_speaker_event,
    to_transcript_line,
)
from src.connectors.zoom.rtms.models import (
    ClientReadyAck,
    DataHandshakeRequest,
    DataHandshakeResponse,
    EventSubscriptionItem,
    EventSubscriptionRequest,
    EventUpdate,
    KeepAliveRequest,
    KeepAliveResponse,
    MediaDataAudio,
    MediaDataText,
    MediaParams,
    MediaServerUrls,
    SignalingHandshakeRequest,
    SignalingHandshakeResponse,
    TextMediaParams,
)
from src.connectors.zoom.rtms.observations import MeetingObserver
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


SUBSCRIBED_EVENTS: tuple[RtmsEventType, ...] = (
    RtmsEventType.ACTIVE_SPEAKER_CHANGE,
    RtmsEventType.PARTICIPANT_JOIN,
    RtmsEventType.PARTICIPANT_LEAVE,
)
"""The three events an avatar can act on, and no others.

Sharing start/stop and video on/off are real events that change nothing about what the
avatar says or hears, and every subscription is traffic on the socket that also carries
audio. These three answer *who is here* and *who is talking*, which are the two questions
the connector was previously unable to answer at all."""


class RtmsService:
    """One RTMS attachment: signaling socket, media socket, and audio translation."""

    __slots__ = (
        "_audio_format",
        "_audio_params",
        "_chat",
        "_clock",
        "_ctx",
        "_events",
        "_keepalive_timeout_s",
        "_media",
        "_media_watchdog",
        "_meeting_uuid",
        "_metrics",
        "_observer",
        "_queue",
        "_server_urls",
        "_signaling",
        "_signaling_watchdog",
        "_signature",
        "_stream_id",
        "_text_degraded",
        "_text_sockets",
        "_transcript",
        "_transport_factory",
        "_unknown_events",
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
        observer: MeetingObserver | None = None,
        subscribe_transcript: bool = False,
        subscribe_chat: bool = False,
        subscribe_events: bool = False,
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

        # Every one of these defaults to off, and that is what keeps the SDK-based Zoom
        # connector — which wants none of it — running the exact handshake it ran before
        # this code existed. Only the browser connector turns them on.
        self._observer = observer
        self._transcript = subscribe_transcript
        self._chat = subscribe_chat
        self._events = subscribe_events
        self._text_degraded: str | None = None
        # One socket per text stream, keyed by name so a failure can be reported against
        # the stream it belongs to rather than as an anonymous connection.
        self._text_sockets: dict[str, JsonWebSocket] = {}
        # Filled by the signaling handshake. Zoom returns a *map* of per-media-type urls,
        # which is the structure that says one media type per connection.
        self._server_urls: MediaServerUrls | None = None
        self._unknown_events = 0

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

    @property
    def text_degraded(self) -> str | None:
        """Why the text streams are not subscribed, or ``None`` when they are (or were never
        wanted).

        Surfaced rather than kept private because the observable symptom of a rejected text
        subscription is an avatar that hears the meeting perfectly and cannot say who asked
        it anything — which looks like a bug in the ledger rather than like a handshake that
        was refused thirty seconds earlier. ``RtmsAudioSource`` folds this into its health
        detail so the reason appears next to the effect.
        """
        return self._text_degraded

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
        await self._subscribe_events()
        # **Last, and on connections of their own.** See ``_attach_text_streams``: the audio
        # leg is fully established and acknowledged before anything optional is attempted,
        # so nothing below this line can cost the avatar its hearing.
        await self._attach_text_streams()

        logger.info(
            "rtms.attached",
            meeting_uuid=self._meeting_uuid,
            audio_format=str(self._audio_format),
            send_rate_ms=self._audio_params.send_rate,
            per_participant=self._audio_params.data_opt,
            transcript=self._transcript,
            chat=self._chat,
            events=self._events,
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

        self._server_urls = response.server_urls()
        media_url = response.media_url()
        if not media_url:
            raise RtmsProtocolError("signaling handshake returned no media server url")
        return media_url

    async def _media_handshake(self) -> None:
        """Subscribe to audio. Exactly the handshake this connector has always sent.

        **Audio-only, and nothing optional is folded in here.** An earlier version asked
        for audio, transcript and chat in this one message, and it was wrong twice over:
        ``media_type`` is not a bitmask — Zoom validated ``AUDIO|TRANSCRIPT|CHAT`` as an
        enum member, found no such member, and rejected the handshake with status 14
        "Media type invalid value" — and the rejection then killed the connection carrying
        the meeting's audio, so the avatar went deaf in a meeting it had already joined.

        The retry that was supposed to save it could not: Zoom stops serving a media socket
        whose handshake it rejected, so the second attempt was sent into a connection that
        would never answer, and it hung until the socket died ninety seconds later. Zoom's
        ``meeting.rtms_stopped`` webhook then tore the session down.

        The lesson is structural rather than a matter of ordering: **an optional
        subscription must not share a socket with a mandatory one.** Transcript and chat now
        attach on connections of their own (``_attach_text_streams``), where a refusal
        cannot reach this one.
        """
        await self._send_media_handshake(
            self._media,
            media_type=MediaDataType.AUDIO,
            params=MediaParams(audio=self._audio_params),
        )

    async def _send_media_handshake(
        self,
        socket: JsonWebSocket | None,
        *,
        media_type: MediaDataType,
        params: MediaParams,
    ) -> None:
        """One data handshake on one socket, and the check on its reply.

        Raises:
            RtmsHandshakeError: Zoom refused the subscription.
            RtmsProtocolError: the reply was not a data handshake response.
        """
        if socket is None:
            raise RtmsConnectionError("media handshake attempted with no socket")

        request = DataHandshakeRequest(
            meeting_uuid=self._meeting_uuid,
            rtms_stream_id=self._stream_id,
            signature=self._signature,
            media_type=int(media_type),
            media_params=params,
        )
        await socket.send_json(request.wire())

        raw = await socket.recv_json()
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

    async def _attach_text_streams(self) -> None:
        """Open a media connection per text stream. Best-effort, always, per stream.

        **One media type per connection**, which is what Zoom's own signaling response has
        been saying all along: it returns ``server_urls`` as a map with separate ``audio``,
        ``video`` and ``transcript`` entries, and ``media_type`` is validated as a single
        enum member rather than as a bitmask. Asking for three on one socket produced status
        14, "Media type invalid value" — see ``_media_handshake`` for what that cost.

        **Best-effort per stream, and that is the property that matters**, not the fact that
        it retries nothing. Each of these is its own socket, so:

        * a Zoom account without RTMS transcription enabled refuses *that* handshake, on
          *that* connection, and the audio leg never learns of it;
        * chat can succeed while transcript is refused, and each says so independently;
        * a text socket dying mid-meeting does not take the meeting's audio with it.

        None of that was true when they shared the audio connection, and no amount of
        ordering or retrying would have made it true.
        """
        for wanted, media_type, params, name, setting in (
            (
                self._transcript,
                MediaDataType.TRANSCRIPT,
                MediaParams(audio=self._audio_params, transcript=TextMediaParams()),
                "transcript",
                "MC_ZOOM_WEB__RTMS_TRANSCRIPT_ENABLED",
            ),
            (
                self._chat,
                MediaDataType.CHAT,
                MediaParams(audio=self._audio_params, chat=TextMediaParams()),
                "chat",
                "MC_ZOOM_WEB__RTMS_CHAT_ENABLED",
            ),
        ):
            if not wanted:
                continue
            await self._attach_text_stream(
                media_type=media_type, params=params, name=name, setting=setting
            )

    async def _attach_text_stream(
        self,
        *,
        media_type: MediaDataType,
        params: MediaParams,
        name: str,
        setting: str,
    ) -> None:
        """Attach one text stream, recording why rather than raising if it will not."""
        url = self._server_urls.for_media(name) if self._server_urls is not None else None
        if not url:
            self._note_degraded(name, f"Zoom offered no {name} media url", setting)
            return

        socket: JsonWebSocket | None = None
        try:
            socket = await self._transport_factory(url)
            await self._send_media_handshake(socket, media_type=media_type, params=params)
        except Exception as exc:
            # Everything, not just a handshake rejection: a text stream is a convenience
            # and the audio leg is already carrying the meeting. Nothing this can do to
            # itself is worth propagating into a session that is otherwise fine.
            if socket is not None:
                with suppress(Exception):
                    await socket.close()
            if name == "transcript":
                self._transcript = False
            else:
                self._chat = False
            self._note_degraded(name, f"Zoom refused the {name} subscription ({exc})", setting)
            return

        self._text_sockets[name] = socket
        logger.info("rtms.text_stream_attached", stream=name, url=url)

    def _note_degraded(self, name: str, reason: str, setting: str) -> None:
        """Record a text stream that is not running, and why.

        Accumulated rather than overwritten, because the two streams fail independently and
        an operator reading one message needs to know whether the other is also off.
        """
        detail = (
            f"{reason}. The avatar still hears the meeting; it will not be able to say who "
            f"said what from this stream. Enable RTMS {name} for this app in the Zoom "
            f"Marketplace, or set {setting}=false to stop asking."
        )
        self._text_degraded = f"{self._text_degraded} {detail}" if self._text_degraded else detail
        logger.warning("rtms.text_subscription_refused", stream=name, detail=detail)

    async def _subscribe_events(self) -> None:
        """Ask for join, leave and active-speaker events. Best-effort, always.

        **Never allowed to fail an attach**, and the reason is not caution for its own sake:
        some accounts deliver ``EVENT_UPDATE`` unsolicited, so a subscription that errors is
        not evidence that the events will not arrive — and the handler is unconditional
        either way. Trading a working audio leg for a rejected *optional* subscription would
        be the tail wagging the dog.
        """
        if not self._events or self._signaling is None:
            return
        request = EventSubscriptionRequest(
            rtms_stream_id=self._stream_id,
            events=[
                EventSubscriptionItem(event_type=int(event)) for event in SUBSCRIBED_EVENTS
            ],
        )
        try:
            await self._signaling.send_json(request.model_dump())
        except Exception as exc:
            logger.warning(
                "rtms.event_subscription_failed",
                error=str(exc),
                note="participant and speaker events may still arrive unsolicited",
            )
            return
        logger.info(
            "rtms.event_subscription_sent",
            events=[event.name for event in SUBSCRIBED_EVENTS],
        )

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
            for name, socket in self._text_sockets.items():
                group.create_task(self._pump_text(name, socket), name=f"rtms-{name}")

    async def _pump_text(self, name: str, socket: JsonWebSocket) -> None:
        """Read one text stream until it ends. Never fails the task group.

        **Returning rather than raising is the whole point of the separate socket.** These
        share a task group with the audio pump, so an exception escaping here would cancel
        it — which would put the text streams back in a position to take the meeting's
        audio down, by a different route than the shared handshake did. A text stream that
        dies is a feature going quiet; the ingest leg is unaffected and says so in health.

        It also gets no keep-alive watchdog. These are silent for minutes at a time by
        nature — nobody is typing, nobody is speaking — so "no traffic" is the normal case
        here where on the audio socket it is a fault.
        """
        try:
            async for message in socket.messages():
                # **On this socket, not the audio one.** ``_handle_media`` used to answer
                # every keep-alive on ``self._media`` whatever connection had asked, so the
                # text sockets never answered theirs — and RTMS hangs up on a connection
                # that goes unanswered for about a minute. Observed exactly that way: both
                # streams attached, then closed 65 seconds later, so a chat message typed
                # after that was never delivered and the @mention appeared to do nothing.
                await self._handle_media(message, socket=socket)
        except Exception as exc:
            logger.warning("rtms.text_stream_ended", stream=name, error=str(exc))
            return
        logger.info("rtms.text_stream_closed", stream=name)

    async def _pump_signaling(self) -> None:
        assert self._signaling is not None
        async for message in self._signaling.messages():
            self._signaling_watchdog.note_activity()
            await self._handle_signaling(message)

    async def _pump_media(self) -> None:
        assert self._media is not None
        async for message in self._media.messages():
            self._media_watchdog.note_activity()
            await self._handle_media(message, socket=self._media, watchdog=self._media_watchdog)

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
            await self._answer_keepalive(
                self._signaling, self._signaling_watchdog, message
            )
        elif msg_type == RtmsMessageType.EVENT_UPDATE:
            self._handle_event(message)
        elif msg_type in (
            RtmsMessageType.STREAM_STATE_UPDATE,
            RtmsMessageType.SESSION_STATE_UPDATE,
        ):
            logger.info("rtms.state_update", msg_type=msg_type, state=message.get("state"))

    async def _handle_media(
        self,
        message: dict[str, Any],
        *,
        socket: JsonWebSocket | None = None,
        watchdog: KeepAliveWatchdog | None = None,
    ) -> None:
        """Handle one message from a media connection.

        ``socket`` is the connection it arrived on, because the reply to a keep-alive has
        to go back the same way — see ``_pump_text``. ``watchdog`` is ``None`` for the text
        streams: they are silent for minutes at a time by nature, so "no traffic" is the
        normal case there where on the audio socket it is a fault.
        """
        msg_type = message.get("msg_type")

        if msg_type == RtmsMessageType.MEDIA_DATA_AUDIO:
            self._enqueue_audio(message)
        elif msg_type == RtmsMessageType.KEEP_ALIVE_REQ:
            await self._answer_keepalive(socket or self._media, watchdog, message)
        elif msg_type == RtmsMessageType.EVENT_UPDATE:
            self._handle_event(message)
        elif msg_type == RtmsMessageType.MEDIA_DATA_TRANSCRIPT:
            self._handle_transcript(message)
        elif msg_type == RtmsMessageType.MEDIA_DATA_CHAT:
            self._handle_chat(message)

    def _enqueue_audio(self, message: dict[str, Any]) -> None:
        # ``model_validate`` used to sit outside this block, and a ``ValidationError``
        # is a ``ValueError`` rather than an ``RtmsProtocolError`` — so an unexpected
        # payload shape did not merely drop a frame, it escaped the media pump and
        # unwound the task group, killing a live connection. Validation belongs on the
        # same footing as decoding: lossy input, never a reason to hang up.
        try:
            wire = MediaDataAudio.model_validate(message)
            frame = to_audio_frame(
                wire, audio_format=self._audio_format, ctx=self._ctx, clock=self._clock
            )
        except (RtmsProtocolError, ValidationError) as exc:
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
        watchdog: KeepAliveWatchdog | None,
        message: dict[str, Any],
    ) -> None:
        if socket is None:
            return
        request = KeepAliveRequest.model_validate(message)
        await socket.send_json(KeepAliveResponse(timestamp=request.timestamp).model_dump())
        if watchdog is not None:
            watchdog.note_request()

    def _handle_event(self, message: dict[str, Any]) -> None:
        """Log an ``EVENT_UPDATE`` and hand the three we act on to the observer.

        Logging first and unconditionally, because it is what it always did and because an
        event type nobody consumes is still the fastest way to see what Zoom is sending.

        Handled on **both** sockets. Zoom has been observed delivering ``EVENT_UPDATE`` on
        the media socket as well as the signaling one, and a handler on only one of them
        produces a connector that works on some accounts and silently knows nothing about
        the roster on others. Duplicate delivery is harmless: a join the ledger already has
        updates a timestamp, and a repeated active speaker is the same person still holding
        the floor.
        """
        try:
            update = EventUpdate.model_validate(message)
        except ValidationError as exc:
            logger.warning("rtms.event.malformed", error=str(exc))
            return

        # **Read from the nested ``event`` object, which is where Zoom puts it.** Reading
        # the top level found nothing on every single event of a live meeting.
        raw_type = update.resolved_event_type()
        try:
            event_type: RtmsEventType | int = RtmsEventType(raw_type)
        except ValueError:
            event_type = raw_type if isinstance(raw_type, int) else -1
        name = event_type.name if isinstance(event_type, RtmsEventType) else str(event_type)
        user_id, user_name = update.participant()
        logger.info("rtms.event", event_type=name, user_id=user_id, user_name=user_name)

        if not isinstance(event_type, RtmsEventType):
            # **An event we cannot read is a feature silently doing nothing**, and it is
            # invisible from the outside: attendance stays empty, nobody is ever the current
            # speaker, and voice barge-in never fires — all of which look exactly like a quiet
            # meeting. Observed live as thirty of these in a row while a participant talked.
            #
            # So the payload itself is logged, because the field names are the only thing that
            # can say *why*. Bounded hard: this runs on the pump that carries the meeting's
            # audio, and an unrecognised event that repeats is precisely the case that would
            # otherwise fill a disk. Truncated for the same reason it is capped.
            self._unknown_events += 1
            if self._unknown_events <= 3:
                logger.warning(
                    "rtms.event.unrecognised",
                    raw=repr(message)[:600],
                    keys=sorted(message.keys()),
                    seen=self._unknown_events,
                    note="participant, speaker and barge-in events are not being decoded; "
                    "this payload is what Zoom actually sent",
                )
            return

        if self._observer is None:
            return

        # Plural: a join carries the whole roster after RTMS attaches mid-meeting, which
        # is the only way this connector learns about people who were already there.
        participants = to_participant_events(update, clock=self._clock)
        if participants:
            for participant in participants:
                self._notify("on_participant", participant)
            return
        speaker = to_speaker_event(update, clock=self._clock)
        if speaker is not None:
            self._notify("on_speaker", speaker)

    def _handle_transcript(self, message: dict[str, Any]) -> None:
        """One line of Zoom's live transcription, attributed."""
        if self._observer is None:
            return
        line = self._decode_text(message, kind="transcript")
        if line is None:
            return
        observation = to_transcript_line(line, clock=self._clock)
        if observation is not None:
            self._notify("on_transcript", observation)

    def _handle_chat(self, message: dict[str, Any]) -> None:
        """One message typed into the meeting's chat."""
        if self._observer is None:
            return
        line = self._decode_text(message, kind="chat")
        if line is None:
            return
        observation = to_chat_message(line, clock=self._clock)
        if observation is not None:
            self._notify("on_chat", observation)

    def _decode_text(self, message: dict[str, Any], *, kind: str) -> MediaDataText | None:
        """Validate a transcript or chat message, counting a bad one rather than raising.

        Lossy input, exactly like ``_enqueue_audio``'s: a malformed line must cost that line
        and never the connection. The lesson is written down in ``_enqueue_audio`` and this
        is the same lesson applied to the streams that arrived after it.
        """
        try:
            return MediaDataText.model_validate(message)
        except ValidationError as exc:
            logger.warning("rtms.text.malformed", kind=kind, error=str(exc))
            return None

    def _notify(self, method: str, payload: object) -> None:
        """Call one observer method, swallowing whatever it does.

        **Contained here rather than trusted to the observer**, even though every observer
        in this repository is written not to raise. This runs on the media pump: an
        exception escaping it unwinds the task group and drops a live RTMS connection, so
        the meeting's audio would stop because a bookkeeping listener had a bad day. The
        cost of being wrong in the other direction is one lost roster update.
        """
        handler = getattr(self._observer, method, None)
        if handler is None:
            return
        try:
            handler(payload)
        except Exception as exc:
            logger.warning("rtms.observer_failed", method=method, error=str(exc))

    # -- teardown ----------------------------------------------------------

    async def detach(self) -> None:
        """Close every socket. Idempotent.

        The text sockets first and each guarded, so one that is already gone cannot stop
        the two that carry the meeting from being closed — the same reason
        ``ZoomWebSession.stop`` guards each of its teardown steps.
        """
        text, self._text_sockets = self._text_sockets, {}
        for name, socket in text.items():
            try:
                await socket.close()
            except Exception as exc:
                logger.warning("rtms.text_stream_close_failed", stream=name, error=str(exc))
        for socket in (self._media, self._signaling):
            if socket is not None:
                await socket.close()
        self._media = None
        self._signaling = None
