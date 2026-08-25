"""``PageAudioSource`` — the ``AudioSource`` port over the page's audio tap.

A view, not an owner: ``TeamsWebSession`` owns the browser and the page channel, and this exists
so that ``MediaRouter`` — shared, platform-blind, already shipped for Zoom, Teams and Meet — can
consume browser-tapped Teams audio through the identical port it consumes everything else
through.

**Deliberately thin.** ``RtmsAudioSource`` is a large object — its own WebSocket, handshake,
keep-alive and reconnect loop — because RTMS genuinely is an independent integration that can
fail and heal without the browser noticing. Here ingest and egress are the *same page channel*:
the socket the avatar's voice travels out on is the socket the meeting's audio travels in on.
Giving this its own lifecycle would mean inventing an independence it does not have, and health
would then be telling the operator something untrue.

**One mixed stream, no attribution.** The tap sits at playout, which is where the meeting has
already been mixed down to what a human would hear, so every frame arrives with
``participant=None``. Against the Graph connector — which receives up to four dominant speakers
with a source id — that is a real loss, and it is not recoverable here at any price: the
individual streams do not exist by that point in the graph. Who is talking comes from the DOM
instead, on a separate path, which is the same division of labour the Google Meet connector has
always used.

**The avatar's own voice is not in it**, and that is what the whole barge-in design rests on.
Teams does not play a participant their own microphone, and the synthetic microphone is built in
a separate ``AudioContext`` that terminates at a ``MediaStreamDestination`` the tap never
watches — so nothing the avatar says reaches here. ``EchoGuard`` can therefore run as a backstop
rather than as the only defence, and energy-based barge-in works.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from src.connectors.teams_web.page.server import PageAudioServer
from src.domain.avatar import AVATAR_INPUT_FORMAT
from src.domain.context import FrameContext
from src.domain.health import ComponentHealth, ComponentState
from src.domain.media import AudioFormat, AudioFrame
from src.infrastructure.logging import get_logger
from src.infrastructure.metrics import MetricName, MetricsCollector
from src.services.media.clock import MediaClock
from src.services.media.queues import BoundedFrameQueue, OverflowPolicy, QueueClosedError

logger = get_logger(__name__)

COMPONENT_NAME = "teams_web_ingest"


class PageAudioSource:
    """Live Teams conference audio, tapped in the page and delivered over the page channel."""

    __slots__ = ("_clock", "_ctx", "_frames", "_metrics", "_queue", "_server", "_started")

    def __init__(
        self,
        *,
        server: PageAudioServer,
        ctx: FrameContext,
        clock: MediaClock,
        queue_size: int,
        metrics: MetricsCollector | None = None,
    ) -> None:
        self._server = server
        self._ctx = ctx
        self._clock = clock
        self._metrics = metrics
        self._frames = 0
        self._started = False
        self._queue: BoundedFrameQueue[AudioFrame] = BoundedFrameQueue(
            name="teams_web_ingest",
            maxsize=max(queue_size, 1),
            # Realtime: a stale frame of a conversation has no value, and the alternative is
            # refusing new audio while holding audio nobody will hear in time.
            policy=OverflowPolicy.DROP_OLDEST,
            metrics=metrics,
        )

    async def start(self) -> None:
        """Register for tapped audio. Idempotent.

        There is nothing to *open*: the page channel is already bound and the page is already
        tapping. Registering the handler is the whole of it, and doing it here rather than at
        construction means a session that never starts never receives a frame it would have to
        discard.
        """
        if self._started:
            return
        self._started = True
        self._server.set_audio_handler(self._on_pcm)

    async def stop(self) -> None:
        """Deregister and close the queue. Idempotent.

        **The page server is not stopped here**, deliberately: it is shared with the publisher,
        and stopping it from the ingest leg would take the avatar's voice down as a side effect
        of ingest shutting up. ``TeamsWebSession`` stops it once, for both.
        """
        if not self._started:
            return
        self._started = False
        self._server.set_audio_handler(None)
        self._queue.close()

    def _on_pcm(self, pcm: bytes) -> None:
        """One tapped buffer, from the page server's read loop.

        **Synchronous, non-blocking and total**, which is a hard requirement rather than a
        style: this runs on the loop that also carries the avatar's voice *into* the page, so
        blocking here would make the avatar stutter, and raising would drop the socket. The
        bounded queue is what makes a non-blocking put possible — a router that has stopped
        pulling costs counted drops rather than backpressure onto a socket that cannot take it.

        A frame that is not a whole number of samples is dropped rather than raising.
        ``AudioFrame`` would reject it, and the page is responsible for frame boundaries —
        ``capture_worklet.js`` only ever posts complete frames, so this is a skew check rather
        than an expected path.
        """
        if len(pcm) % AVATAR_INPUT_FORMAT.bytes_per_frame:
            return
        frame = AudioFrame(
            pcm=pcm,
            # **Our media clock, not the page's.** The header carries the page's
            # ``AudioContext.currentTime``, which runs on the audio device's timeline: an
            # arbitrary origin that drifts against the monotonic clock the rest of the pipeline
            # is paced on. Mixing the two corrupts the single-clock invariant A/V sync depends
            # on.
            pts_us=self._clock.now_us(),
            # Asserted rather than converted: the capture ``AudioContext`` is constructed at
            # 16 kHz so Web Audio resamples in native code before the worklet sees a sample. A
            # page running a stale script that built a 48 kHz context would produce a chipmunk
            # voice three services downstream; this is where that is caught.
            format=AVATAR_INPUT_FORMAT,
            ctx=self._ctx,
            # No attribution is possible from a mixed tap. See the module docstring.
            participant=None,
        )
        self._queue.put(frame, ctx=self._ctx, reason="ingest_overflow")

    async def frames(self) -> AsyncIterator[AudioFrame]:
        """Yield conference audio until the session stops."""
        while True:
            try:
                frame = await self._queue.get()
            except (QueueClosedError, asyncio.CancelledError):
                return
            self._frames += 1
            if self._metrics is not None:
                self._metrics.increment(
                    MetricName.FRAMES_RECEIVED_TOTAL, ctx=frame.ctx, kind="audio"
                )
            yield frame

    def health(self) -> ComponentHealth:
        """Healthy once the tap has produced anything; degraded until it has.

        **Not unhealthy while silent**, and the distinction is the whole value of this report. A
        meeting in which nobody has spoken yet produces no frames and is perfectly fine. A tap
        that never found the page's audio graph also produces no frames and is completely
        broken. They are indistinguishable from here, so this reports the fact (``tapped=0``)
        and refuses to editorialise — ``page_event`` lines carrying ``audioTapped`` are what
        settle it, and they are logged the moment a source is wired.
        """
        tapped = self._server.audio_received
        detail = (
            f"tapped={tapped} delivered={self._frames} "
            f"dropped={self._queue.dropped} pages={self._server.attached_pages}"
        )
        if not self._started:
            # ``UNKNOWN`` rather than ``UNHEALTHY``: not-yet-started is what that state is for,
            # and calling it unhealthy would make every session look broken for the window
            # between construction and ``start``.
            return ComponentHealth(
                name=COMPONENT_NAME, state=ComponentState.UNKNOWN, detail="not started"
            )
        if tapped == 0:
            return ComponentHealth(
                name=COMPONENT_NAME,
                state=ComponentState.DEGRADED,
                detail=(
                    "no audio has been tapped from the page yet; this is either a silent "
                    f"meeting or a tap that never attached to Teams' playout graph — {detail}"
                ),
            )
        return ComponentHealth(
            name=COMPONENT_NAME, state=ComponentState.HEALTHY, detail=detail
        )

    @property
    def frames_received(self) -> int:
        return self._frames

    @property
    def audio_format(self) -> AudioFormat:
        """What this source yields. Always ``AVATAR_INPUT_FORMAT`` — see ``_on_pcm``."""
        return AVATAR_INPUT_FORMAT
