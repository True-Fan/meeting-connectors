"""RtmsAudioSource — the ``AudioSource`` port over RTMS.

Owns retry and health; ``RtmsService`` owns the protocol. Two separate reasons to
change, so two objects.

**The join/RTMS race** (doc 003 §3.1): a session can be created before Zoom's
``meeting.rtms_started`` webhook binds the meeting context. This source tolerates
that — ``start()`` returns immediately either way, attaching right away if the
context is already bound, or polling until it is (doc 003 §3.1). Meeting a session
before its webhook arrives must not fail the session; only running out the wait
budget does.

**RTMS sessions cannot be resumed** (doc 001 §10). A reconnect re-handshakes from
scratch and audio spoken during the gap is permanently lost — so the gap duration is
logged explicitly rather than being silently papered over. That number is the honest
measure of what a reconnect cost.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

from pydantic import SecretStr

from src.connectors.zoom.auth.rtms_signature import build_signature
from src.connectors.zoom.exceptions import (
    RtmsError,
    RtmsHandshakeError,
    RtmsProtocolError,
)
from src.connectors.zoom.rtms.mapping import (
    build_audio_params,
    negotiated_audio_format,
    rtms_attachment,
)
from src.connectors.zoom.rtms.service import RtmsService, TransportFactory
from src.domain.context import FrameContext
from src.domain.health import ComponentHealth, ComponentState
from src.domain.media import AudioFormat, AudioFrame
from src.domain.session import SessionContext
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

DEFAULT_ATTACH_WAIT_POLL_S = 0.5
"""How often to recheck the meeting context for a binding while waiting."""

DEFAULT_ATTACH_WAIT_TIMEOUT_S = 300.0
"""How long to wait for ``meeting.rtms_started`` before failing this component.

Matches ``SessionRegistry.DEFAULT_PENDING_TTL_S`` — there is no point waiting past
the point a parked binding on the other side of the race would itself expire.
"""


class RtmsAudioSource:
    """Live Zoom participant audio, with reconnect."""

    __slots__ = (
        "_attach_wait_poll_s",
        "_attach_wait_timeout_s",
        "_client_id",
        "_client_secret",
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
        "_session",
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
        session: SessionContext,
        client_id: str,
        client_secret: SecretStr,
        ctx: FrameContext,
        clock: MediaClock,
        queue_size: int = 50,
        send_rate_ms: int = 20,
        per_participant_audio: bool = True,
        policy: ReconnectPolicy | None = None,
        metrics: MetricsCollector | None = None,
        transport_factory: TransportFactory | None = None,
        attach_wait_poll_s: float = DEFAULT_ATTACH_WAIT_POLL_S,
        attach_wait_timeout_s: float = DEFAULT_ATTACH_WAIT_TIMEOUT_S,
    ) -> None:
        # The RTMS attachment (meeting_uuid, stream id, signaling url, signature) is
        # deliberately *not* a constructor argument: it may not exist yet. ``session``
        # is the live handle this source rereads until ``meeting.rtms_started`` fills
        # it in (doc 003 §3.1) — see ``_resolve_attachment``.
        self._session = session
        self._client_id = client_id
        self._client_secret = client_secret
        self._meeting_uuid: str | None = None
        self._stream_id: str | None = None
        self._signaling_url: str | None = None
        self._signature: str | None = None

        self._ctx = ctx
        self._clock = clock
        self._send_rate_ms = send_rate_ms
        self._per_participant_audio = per_participant_audio
        self._policy = policy or ReconnectPolicy()
        self._metrics = metrics
        self._transport_factory = transport_factory
        self._attach_wait_poll_s = attach_wait_poll_s
        self._attach_wait_timeout_s = attach_wait_timeout_s

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
        """Attach and begin streaming, or wait for a binding to attach with.

        Returns as soon as either:

        * the meeting context is already bound (webhook-first race, or a session
          created after the webhook was parked) — the first attach has then
          succeeded, same as before; or
        * it isn't yet (session-first race) — a background task is left running
          that polls the meeting context and attaches the moment
          ``meeting.rtms_started`` binds it, or fails this *component* (not the
          session) if nothing arrives within ``attach_wait_timeout_s``.

        Either way this never raises for "not bound yet" and never blocks the
        caller on a webhook Zoom, not us, controls (doc 003 §3.1).

        Raises:
            RtmsError: the context was already bound and that first attach failed
                unrecoverably (bad signature, rejected handshake).
        """
        if self._task is not None:
            return

        attachment = self._resolve_attachment()
        if attachment is None:
            self._state = ComponentState.UNKNOWN
            self._detail = "waiting for meeting.rtms_started webhook"
            logger.info(
                "rtms.attach_deferred",
                meeting_number=self._session.meeting.meeting_number,
            )
            self._task = asyncio.create_task(self._wait_and_run(), name="rtms-source")
            return

        self._bind_attachment(attachment)
        service = self._build_service()
        await service.attach(self._signaling_url)  # type: ignore[arg-type]
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
            #
            # Anything *else* it raises is a failure that already happened — the task
            # died earlier and has been holding the exception since. Re-raising it
            # here attributes someone else's crash to whoever called ``stop``, and
            # aborts their teardown half-done: a ``DELETE /sessions/{id}`` that leaves
            # the avatar sitting in the meeting, because the caller never reached the
            # line that closes the browser. Report it and finish releasing.
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning("rtms.ingest_task_failed", error=str(exc))
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
        # The negotiated format comes entirely from our own send-rate/channel config
        # and the avatar's fixed input contract — never from the attachment — so it
        # is known even before a binding exists.
        params = build_audio_params(
            send_rate_ms=self._send_rate_ms, per_participant=self._per_participant_audio
        )
        return negotiated_audio_format(params)

    @property
    def reconnects(self) -> int:
        return self._reconnects

    def _resolve_attachment(self) -> tuple[str, str, str] | None:
        """Read back whatever RTMS attachment the meeting context currently carries.

        Returns ``None`` before ``meeting.rtms_started`` has bound it — that is the
        normal "not yet" state of the race, not an error (doc 003 §3.1).
        """
        try:
            return rtms_attachment(self._session.meeting)
        except RtmsProtocolError:
            return None

    def _bind_attachment(self, attachment: tuple[str, str, str]) -> None:
        """Record a resolved attachment and derive its handshake signature."""
        meeting_uuid, stream_id, signaling_url = attachment
        self._meeting_uuid = meeting_uuid
        self._stream_id = stream_id
        self._signaling_url = signaling_url
        self._signature = build_signature(
            client_id=self._client_id,
            client_secret=self._client_secret,
            meeting_uuid=meeting_uuid,
            rtms_stream_id=stream_id,
        )

    async def _wait_and_run(self) -> None:
        """Poll for the attachment, attach once it lands, then supervise.

        Runs as this source's own task (assigned to ``self._task`` by ``start()``)
        so a session-first race never holds up ``ZoomMeetingSession.start`` — the
        avatar publishes and idles while this waits (doc 003 §3.1).
        """
        loop = asyncio.get_running_loop()
        deadline = (
            loop.time() + self._attach_wait_timeout_s
            if self._attach_wait_timeout_s is not None
            else None
        )

        attachment = None
        while attachment is None:
            attachment = self._resolve_attachment()
            if attachment is not None:
                break
            if deadline is not None and loop.time() >= deadline:
                self._fail(
                    f"no rtms_started webhook bound within "
                    f"{self._attach_wait_timeout_s:.0f}s"
                )
                return
            await asyncio.sleep(self._attach_wait_poll_s)

        self._bind_attachment(attachment)
        try:
            service = self._build_service()
            await service.attach(self._signaling_url)  # type: ignore[arg-type]
        except (RtmsError, OSError) as exc:
            self._fail(f"attach failed once bound: {exc}")
            return

        self._current = service
        self._state = ComponentState.HEALTHY
        self._detail = None
        logger.info("rtms.attached_after_wait", meeting_uuid=self._meeting_uuid)
        await self._supervise(service)

    def _build_service(self) -> RtmsService:
        assert self._meeting_uuid is not None
        assert self._stream_id is not None
        assert self._signature is not None
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
                    await current.attach(self._signaling_url)  # type: ignore[arg-type]
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
