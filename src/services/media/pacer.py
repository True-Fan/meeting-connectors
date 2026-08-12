"""Pacer — releases frames on the shared media clock, and never stops.

Two responsibilities that must live together because they share one timeline:

**1. Pacing.** Frames are released when their PTS arrives, not when they finish
decoding. Zoom's send-audio and send-video are separate paths with documented desync
risk (doc 001 §7.1); publishing "as decoded" guarantees drift because decode
completion time is unrelated to presentation time. One clock, two streams.

**2. Idle continuity.** The video loop runs for the session's whole life. When the
avatar has nothing to say, idle frames and silence go out instead — otherwise the
camera freezes and the avatar stops looking like a person (doc 003 §1.4).

Late frames are **dropped, not burst**. Bursting is what turns a brief hiccup into
visible desync that never recovers, because every subsequent frame inherits the
backlog. Audio gaps are filled with silence rather than by shifting later audio
earlier, which would corrupt the timeline permanently.
"""

from __future__ import annotations

import asyncio

from src.domain.context import FrameContext
from src.domain.media import AudioFormat, AudioFrame, VideoFormat, VideoFrame
from src.infrastructure.logging import get_logger
from src.infrastructure.metrics import MetricName, MetricsCollector
from src.protocols.sink import MediaSink
from src.services.media.clock import MediaClock
from src.services.media.echo_guard import EchoGuard
from src.services.media.idle_source import IdleFrameSource
from src.services.media.queues import BoundedFrameQueue, OverflowPolicy

logger = get_logger(__name__)

VIDEO_LATE_TOLERANCE_US = 40_000
"""One frame period at 25 fps. Beyond this a frame is stale and dropped."""

AUDIO_LATE_TOLERANCE_US = 100_000
"""How far the audio *schedule* may slip before it is rebased on the clock.

Applies to the loop's own deadline, never to a queued frame — see ``_take_audio`` for why
those are two different questions and why conflating them destroyed the avatar's voice."""

AUDIO_BACKLOG_TRIM_US = 200_000
"""Backlog beyond which queued **silence** is discarded to catch up.

Speech arrives in bursts — an agent synthesises a whole utterance faster than it is spoken —
so a backlog is normal and is not a fault. It is also latency, and latency between hearing a
question and answering it is the one thing a conversational avatar cannot spend freely.

Silence is what gets spent instead. A discarded silent chunk shortens a pause by 20 ms and
nobody can hear it; a discarded audible chunk is a hole in a word and everybody can. So the
backlog is trimmed during the gaps between utterances, which is exactly where a listener
would not notice, and speech is only ever dropped at the queue's hard bound."""

AUDIO_LOSS_REPORT_US = 10_000_000
"""How often to report accumulated audio loss. Rate limited because the condition that
produces it produces it fifty times a second."""

SILENCE_FLOOR = 512
"""Peak ``|sample|`` at or above which published audio counts as sound, on int16's 32767
scale (≈ -36 dBFS). Comfortably above lossy-decode noise — silence through AAC does not come
back as exact zeros — and far below speech."""

_ENERGY_STRIDE = 8
"""Sample every eighth sample when testing for energy. 120 samples of a 20 ms frame at 48 kHz
is ample to tell speech from silence, and the gate's hangover covers the one frame a
missed onset could delay it by."""


def _is_audible(pcm: bytes, *, floor: int = SILENCE_FLOOR, stride: int = _ENERGY_STRIDE) -> bool:
    """Whether this PCM carries sound rather than silence.

    Not a voice activity detector and not a speech decision — just "did we emit any energy",
    which is the only question the echo gate needs answered. Returns on the first loud
    sample, so speech costs almost nothing to detect.

    Assumes native-endian int16, which ``SampleFormat.S16LE`` is on every supported
    platform.
    """
    usable = len(pcm) - len(pcm) % 2
    if not usable:
        return False
    samples = memoryview(pcm)[:usable].cast("h")
    for index in range(0, len(samples), stride):
        sample = samples[index]
        if sample >= floor or sample <= -floor:
            return True
    return False


class Pacer:
    """Paces decoded and idle media into a ``MediaSink``."""

    __slots__ = (
        "_audio_chunk_us",
        "_audio_format",
        "_audio_queue",
        "_backlog_trim_frames",
        "_clock",
        "_ctx",
        "_drops",
        "_echo_guard",
        "_idle",
        "_idle_audio",
        "_idle_video",
        "_interrupted",
        "_loss_reported_at_us",
        "_loss_reported_total",
        "_metrics",
        "_muted_until_us",
        "_published_audio",
        "_published_video",
        "_sink",
        "_speaking",
        "_video_format",
        "_video_queue",
    )

    def __init__(
        self,
        *,
        ctx: FrameContext,
        clock: MediaClock,
        sink: MediaSink,
        idle: IdleFrameSource,
        video_format: VideoFormat,
        audio_format: AudioFormat,
        echo_guard: EchoGuard | None = None,
        video_queue_size: int = 3,
        audio_queue_size: int = 10,
        audio_chunk_ms: int = 20,
        metrics: MetricsCollector | None = None,
    ) -> None:
        self._ctx = ctx
        self._clock = clock
        self._sink = sink
        self._idle = idle
        self._video_format = video_format
        self._audio_format = audio_format
        self._echo_guard = echo_guard
        self._metrics = metrics
        self._audio_chunk_us = audio_chunk_ms * 1_000

        self._video_queue: BoundedFrameQueue[VideoFrame] = BoundedFrameQueue(
            name="pacer_video",
            maxsize=video_queue_size,
            policy=OverflowPolicy.DROP_OLDEST,
            metrics=metrics,
        )
        self._audio_queue: BoundedFrameQueue[AudioFrame] = BoundedFrameQueue(
            name="pacer_audio",
            maxsize=audio_queue_size,
            policy=OverflowPolicy.DROP_OLDEST,
            metrics=metrics,
        )
        self._speaking = False
        self._published_video = 0
        self._published_audio = 0
        self._idle_video = 0
        self._idle_audio = 0
        self._muted_until_us = 0
        self._interrupted = 0
        # In frames, so the hot path compares two integers rather than doing arithmetic.
        # At least one, because a trim threshold of zero would discard every silent chunk and
        # turn the gaps between utterances into gaps in the timeline.
        self._backlog_trim_frames = max(AUDIO_BACKLOG_TRIM_US // self._audio_chunk_us, 1)
        self._drops: dict[str, int] = {}
        # ``None`` rather than zero, so the *first* loss is reported the moment it happens.
        # Zero meant "reported at session start", which silenced the report for the first ten
        # seconds — precisely the window in which a misconfigured pipeline shows itself.
        self._loss_reported_at_us: int | None = None
        self._loss_reported_total = 0

    # -- submission --------------------------------------------------------

    def submit_video(self, frame: VideoFrame) -> None:
        """Offer a decoded video frame. Never blocks."""
        self._video_queue.put(frame, ctx=frame.ctx, reason="pacer_video_overflow")

    def submit_audio(self, frame: AudioFrame) -> None:
        """Offer a decoded audio frame. Never blocks."""
        self._audio_queue.put(frame, ctx=frame.ctx, reason="pacer_audio_overflow")

    # -- stats -------------------------------------------------------------

    @property
    def stats(self) -> dict[str, int]:
        return {
            "published_video": self._published_video,
            "published_audio": self._published_audio,
            "idle_video": self._idle_video,
            "idle_audio": self._idle_audio,
            "dropped_video": self._video_queue.dropped,
            "dropped_audio": self._audio_queue.dropped,
            "interrupted": self._interrupted,
            "audio_backlog": self._audio_queue.qsize(),
            **self._drops,
        }

    @property
    def is_speaking(self) -> bool:
        """True when real avatar media was published recently."""
        return self._speaking

    @property
    def is_muted(self) -> bool:
        """True while an interrupt is still discarding avatar media."""
        return self._clock.now_us() < self._muted_until_us

    # -- barge-in ----------------------------------------------------------

    def interrupt(self, *, hold_ms: int = 0) -> int:
        """Stop the avatar mid-sentence: drop what is queued, and hold the line briefly.

        Called when somebody in the meeting takes the floor — in Meet, by raising their hand.
        Two effects, because either alone leaves the avatar audibly talking over them:

        * **The queues are emptied.** Everything buffered here is the tail of a sentence
          nobody wants finished, so it is discarded rather than published. That is a few
          hundred milliseconds — the queues are deliberately shallow.
        * **Media decoded during ``hold_ms`` is discarded too.** This is the part that
          matters. The agent's audio is *already in flight* when the hand goes up: streamed
          over the socket, sitting in the decoder, on its way to these queues. Emptying the
          queues without a hold buys one queue-depth of silence and then the same sentence
          resumes, which sounds like a glitch rather than like yielding.

        Idle frames fill the gap, so the avatar returns to looking like it is listening —
        which is exactly what it is doing — instead of freezing.

        **Why a window rather than "until the agent's next utterance".** Nothing here can tell
        the agent's new reply from the old sentence: a decoded frame carries no lineage back
        to the chunk it came from, and adding one would mean threading an id through the
        decoder for this alone. A window is the honest approximation, and its size is the
        trade it makes: too short and the old sentence resumes, too long and the *reply*
        ("go ahead") gets clipped. See ``GoogleMeetSettings.hand_raise_mute_ms``.

        Returns the number of frames dropped, for the caller's log line. Safe to call when the
        avatar is silent, where it drops nothing and mutes nothing audible.
        """
        dropped = self._video_queue.qsize() + self._audio_queue.qsize()
        self._video_queue.clear()
        self._audio_queue.clear()

        if hold_ms > 0:
            # Extended, never shortened: a second hand going up during the hold must not cut
            # the window short, and ``max`` is what makes repeated calls monotonic.
            self._muted_until_us = max(
                self._muted_until_us, self._clock.now_us() + hold_ms * 1_000
            )

        # The gate is armed by *publishing* audible audio, and after this we publish silence.
        # Clearing it here rather than waiting for the next publish means "is the avatar
        # speaking" answers correctly to anything that asks in between.
        self._speaking = False
        self._interrupted += 1
        return dropped

    # -- run ---------------------------------------------------------------

    async def run(self) -> None:
        """Pace video and audio until cancelled.

        Both loops run for the session's whole life; neither exits when the avatar
        falls silent.
        """
        async with asyncio.TaskGroup() as group:
            group.create_task(self._run_video(), name="pacer-video")
            group.create_task(self._run_audio(), name="pacer-audio")

    async def _run_video(self) -> None:
        period_us = self._video_format.frame_duration_us
        next_pts = self._clock.now_us()

        while True:
            delay = self._clock.deadline_delay_s(next_pts)
            if delay > 0:
                await asyncio.sleep(delay)

            frame = self._take_video()
            if frame is None:
                frame = self._idle.next_video(next_pts)
                self._idle_video += 1
                is_idle = True
            else:
                self._idle.note_real_frame(frame)
                is_idle = False

            await self._publish_video(frame, is_idle=is_idle)
            next_pts += period_us

            # If we have fallen more than a frame behind, resynchronise to now
            # rather than trying to catch up — catching up is bursting.
            if self._clock.is_late(next_pts, tolerance_us=period_us):
                next_pts = self._clock.now_us()

    async def _run_audio(self) -> None:
        next_pts = self._clock.now_us()

        while True:
            delay = self._clock.deadline_delay_s(next_pts)
            if delay > 0:
                await asyncio.sleep(delay)

            frame = self._take_audio()
            if frame is None:
                # Fill with silence rather than shifting later audio earlier, which
                # would corrupt the timeline for the rest of the session.
                frame = self._idle.next_audio(next_pts)
                self._idle_audio += 1
                is_idle = True
            else:
                is_idle = False

            await self._publish_audio(frame, is_idle=is_idle)
            self._report_audio_loss()
            next_pts += self._audio_chunk_us

            if self._clock.is_late(next_pts, tolerance_us=AUDIO_LATE_TOLERANCE_US):
                next_pts = self._clock.now_us()

    # -- queue draining ----------------------------------------------------

    def _take_video(self) -> VideoFrame | None:
        """Take the freshest video frame that is not already stale."""
        muted = self.is_muted
        while (frame := self._video_queue.get_nowait()) is not None:
            if muted:
                # Video as well as audio, so an interrupted avatar visibly returns to
                # listening rather than mouthing a sentence nobody can hear.
                self._count_drop("video", "interrupted")
                continue
            if self._clock.is_late(frame.pts_us, tolerance_us=VIDEO_LATE_TOLERANCE_US):
                self._count_drop("video", "stale")
                continue
            return frame
        return None

    def _take_audio(self) -> AudioFrame | None:
        """The next chunk to publish, trimming silence when a backlog has built up.

        **This used to discard any frame that had waited more than 100 ms in the queue, and
        that single line was destroying the avatar's voice.** The reasoning looked sound —
        video drops late frames, audio tolerates more — but it rests on a premise that is
        false for this pipeline: it treated ``pts_us`` as a presentation deadline, when
        ``FfmpegDecoder`` stamps it with ``clock.now_us()`` at the moment of *decode*. So the
        test did not measure "is this audio late"; it measured "how long has this chunk been
        queued", and the queue is drained deliberately at one 20 ms chunk per 20 ms.

        The arithmetic follows immediately: the sixth chunk of any burst has waited 100 ms by
        construction, so **no more than five consecutive chunks could ever survive.** An agent
        synthesises a whole utterance faster than it is spoken, and delivery stalls and
        catches up — a live session showed the agent's own ffmpeg pausing ~0.5 s and resuming
        at 1.05x, roughly every ten seconds. Each of those bursts was ~25 chunks, of which the
        queue took 10 and this test then discarded all but ~5. Four hundred milliseconds of
        speech destroyed per burst, counted nowhere anybody was looking, and audible as an
        avatar that stutters and swallows words.

        A late audio chunk is not a stale video frame. Video is a sequence of *alternatives* —
        only the freshest is worth showing, and the one behind it is genuinely worthless.
        Audio is a *continuum*: every chunk is the only copy of that piece of the sentence, and
        playing it 200 ms late is what a jitter buffer is for. Lateness is therefore not a
        reason to discard audio, and this no longer does. The bound on latency is the queue's
        own depth, and the trim below is what keeps it from being reached in ordinary use.
        """
        muted = self.is_muted
        while (frame := self._audio_queue.get_nowait()) is not None:
            if muted:
                self._count_drop("audio", "interrupted")
                continue
            # ``qsize`` is the backlog *behind* this frame, since it has already been taken.
            if self._audio_queue.qsize() >= self._backlog_trim_frames and not _is_audible(
                frame.pcm
            ):
                self._count_drop("audio", "backlog_silence")
                continue
            return frame
        return None

    def _report_audio_loss(self) -> None:
        """Say out loud when the avatar's voice is losing audio, at most every ten seconds.

        **Nothing logged this, and that is why it ran for weeks.** Every drop was counted into
        a metrics collector that no deployment scrapes, so an avatar whose speech was being
        shredded produced a log identical to a healthy one: joined, publishing, healthy. The
        only visible symptom was a human saying it sounded bad.

        Overflow at the queue is the number that matters — it means real speech was discarded
        because the pipeline could not hold it. Trimmed silence is reported alongside it
        precisely so the two are never confused: one is damage, the other is the mechanism
        that avoids damage.
        """
        overflow = self._audio_queue.dropped
        if overflow <= self._loss_reported_total:
            return
        now_us = self._clock.now_us()
        if (
            self._loss_reported_at_us is not None
            and now_us - self._loss_reported_at_us < AUDIO_LOSS_REPORT_US
        ):
            return

        lost = overflow - self._loss_reported_total
        self._loss_reported_total = overflow
        self._loss_reported_at_us = now_us
        logger.warning(
            "pacer.audio_lost",
            chunks=lost,
            ms=lost * self._audio_chunk_us // 1_000,
            total_chunks=overflow,
            backlog=self._audio_queue.qsize(),
            capacity=self._audio_queue.maxsize,
            trimmed_silence=self._drops.get("audio_backlog_silence", 0),
            note="the avatar's speech is arriving faster than it can be published and the "
            "queue is full, so audio is being discarded; raise MC_MEDIA__AUDIO_QUEUE_SIZE",
        )

    def _count_drop(self, kind: str, reason: str) -> None:
        self._drops[f"{kind}_{reason}"] = self._drops.get(f"{kind}_{reason}", 0) + 1
        if self._metrics is not None:
            self._metrics.increment(
                MetricName.FRAMES_DROPPED_TOTAL, ctx=self._ctx, stage=f"pacer_{kind}", reason=reason
            )

    # -- publishing --------------------------------------------------------

    async def _publish_video(self, frame: VideoFrame, *, is_idle: bool) -> None:
        started = self._clock.now_us()
        await self._sink.publish_video(frame)
        self._published_video += 1
        self._record_publish(frame.pts_us, started, kind="video", is_idle=is_idle)

    async def _publish_audio(self, frame: AudioFrame, *, is_idle: bool) -> None:
        started = self._clock.now_us()
        await self._sink.publish_audio(frame)
        self._published_audio += 1

        # Close the echo loop: publishing *audible* avatar audio arms the gate so the
        # mixed-back copy arriving through RTMS is suppressed (doc 003 §3.3).
        #
        # **The audibility test is what makes the gate reopen.** Arming on ``not is_idle``
        # alone assumed the avatar streams only while speaking — but the contract in
        # ``domain.avatar`` is a *continuously* streamed fMP4, so the decoder produces a frame
        # every 20 ms forever, ``is_idle`` is never True, and the gate stayed armed for the
        # rest of the session. Inbound audio was then suppressed permanently: the avatar
        # heard the first couple of seconds of a meeting and nothing ever again. Observed
        # exactly that way against a live agent — the router forwarded 124 frames and then
        # froze there for two minutes while the meeting carried on.
        #
        # Silence cannot echo, so there is nothing to defend against while publishing it.
        # That makes energy the correct trigger rather than mere frame arrival.
        audible = not is_idle and _is_audible(frame.pcm)
        self._speaking = audible
        if audible and self._echo_guard is not None:
            self._echo_guard.note_publishing(self._clock.now_us())

        self._record_publish(frame.pts_us, started, kind="audio", is_idle=is_idle)

    def _record_publish(self, pts_us: int, started_us: int, *, kind: str, is_idle: bool) -> None:
        if self._metrics is None:
            return
        now = self._clock.now_us()
        self._metrics.observe(MetricName.PUBLISH_US, now - started_us, ctx=self._ctx, kind=kind)
        self._metrics.observe(
            MetricName.PACE_WAIT_US, max(started_us - pts_us, 0), ctx=self._ctx, kind=kind
        )
        self._metrics.increment(
            MetricName.IDLE_FRAMES_PUBLISHED_TOTAL
            if is_idle
            else MetricName.FRAMES_PUBLISHED_TOTAL,
            ctx=self._ctx,
            kind=kind,
        )

    def close(self) -> None:
        """Close both queues."""
        self._video_queue.close()
        self._audio_queue.close()
