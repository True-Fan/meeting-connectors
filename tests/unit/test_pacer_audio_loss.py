"""Regression: the avatar's speech must survive the queue it is paced through.

**The bug this file exists for made the avatar sound broken and logged nothing.**
``Pacer._take_audio`` discarded any queued chunk whose ``pts_us`` was more than 100 ms old,
reasoning that late audio is stale audio. But ``FfmpegDecoder`` stamps ``pts_us`` with
``clock.now_us()`` at the moment of *decode*, so the test measured queue residency, not
lateness — and the queue is drained deliberately at one 20 ms chunk per 20 ms of wall clock.
The sixth chunk of any burst has therefore waited 100 ms *by construction*.

Five chunks. That was the ceiling on how much of any burst could ever reach the meeting.

It mattered because an agent does not deliver speech in real time: it synthesises an utterance
faster than it is spoken, and delivery stalls and catches up. A live session showed the agent's
own ffmpeg pausing ~0.5 s and resuming at 1.05x, roughly every ten seconds — bursts of ~25
chunks, of which a 10-deep queue kept 10 and this test then threw away half. The result was an
avatar that stuttered and swallowed words while every health check read green.

The distinction the fix rests on: **video frames are alternatives, audio chunks are a
continuum.** A late video frame is worthless because a newer one is already available; a late
audio chunk is the only copy of that piece of the sentence, and playing it late is what a
jitter buffer is *for*.
"""

from __future__ import annotations

import time

import pytest

from src.config.settings import Environment, ObservabilitySettings
from src.domain.context import FrameContext
from src.domain.media import AudioFormat, AudioFrame, VideoFormat
from src.infrastructure.logging import configure_logging
from src.services.media.clock import MediaClock
from src.services.media.idle_source import IdleFrameSource
from src.services.media.pacer import AUDIO_BACKLOG_TRIM_US, Pacer

PUBLISH_AUDIO = AudioFormat(sample_rate_hz=48_000, channels=1)
VIDEO = VideoFormat(width=320, height=180, fps=10)

CHUNK_MS = 20
TRIM_FRAMES = AUDIO_BACKLOG_TRIM_US // (CHUNK_MS * 1_000)
SPEECH = b"\x00\x40" * 480  # 20 ms of tone at 48 kHz, well above the silence floor
SILENCE = bytes(1920)  # 20 ms of digital silence


def _aged_clock(seconds: float = 5.0) -> MediaClock:
    """A clock that has already been running, so a pts in the past is still non-negative.

    ``AudioFrame`` rejects a negative pts, and a freshly built ``MediaClock`` reads near zero
    — which would make "decoded a second ago", the whole condition under test, unconstructible.
    """
    return MediaClock(origin_ns=time.monotonic_ns() - int(seconds * 1_000_000_000))


class NullSink:
    async def publish_audio(self, frame: object) -> None: ...
    async def publish_video(self, frame: object) -> None: ...

    def health(self) -> object:  # pragma: no cover - never called here
        raise NotImplementedError


def _pacer(ctx: FrameContext, clock: MediaClock, *, audio_queue_size: int = 50) -> Pacer:
    return Pacer(
        ctx=ctx,
        clock=clock,
        sink=NullSink(),  # type: ignore[arg-type]
        idle=IdleFrameSource(ctx=ctx, video_format=VIDEO, audio_format=PUBLISH_AUDIO),
        video_format=VIDEO,
        audio_format=PUBLISH_AUDIO,
        audio_queue_size=audio_queue_size,
        audio_chunk_ms=CHUNK_MS,
    )


def _submit(pacer: Pacer, ctx: FrameContext, pcm: bytes, *, pts_us: int) -> None:
    pacer.submit_audio(AudioFrame(pcm=pcm, pts_us=pts_us, format=PUBLISH_AUDIO, ctx=ctx))


def _drain(pacer: Pacer) -> list[AudioFrame]:
    """Everything the pacer would publish, taken as fast as its loop would take it."""
    taken: list[AudioFrame] = []
    while (frame := pacer._take_audio()) is not None:
        taken.append(frame)
    return taken


class TestABurstOfSpeechSurvives:
    def test_a_whole_utterance_is_published_however_long_it_waited(
        self, frame_ctx: FrameContext
    ) -> None:
        """The regression itself, with the arithmetic that used to kill it.

        Twenty-five chunks — half a second of speech — decoded in one burst, every one of
        them stamped a full second in the past. Under the old rule all but the first handful
        were discarded as "stale". Every one of them is somebody's sentence.
        """
        clock = _aged_clock()
        pacer = _pacer(frame_ctx, clock)
        long_ago = clock.now_us() - 1_000_000

        for index in range(25):
            _submit(pacer, frame_ctx, SPEECH, pts_us=long_ago + index * 20_000)

        taken = _drain(pacer)

        assert len(taken) == 25, "audio was discarded for having waited its turn"
        assert all(frame.pcm == SPEECH for frame in taken)

    def test_the_order_of_the_sentence_is_preserved(self, frame_ctx: FrameContext) -> None:
        """A jitter buffer that reorders is worse than one that drops."""
        clock = _aged_clock()
        pacer = _pacer(frame_ctx, clock)
        for index in range(10):
            _submit(pacer, frame_ctx, bytes([index, 0]) * 960, pts_us=clock.now_us())

        taken = _drain(pacer)

        assert [frame.pcm[0] for frame in taken] == list(range(10))

    def test_a_burst_larger_than_the_queue_still_loses_the_oldest_only(
        self, frame_ctx: FrameContext
    ) -> None:
        """The hard bound stays where it was — at the queue, which is bounded on purpose.

        What changed is that it is now the *only* place speech is lost, and it is reported.
        """
        clock = _aged_clock()
        pacer = _pacer(frame_ctx, clock, audio_queue_size=4)
        for index in range(10):
            _submit(pacer, frame_ctx, bytes([index, 0]) * 960, pts_us=clock.now_us())

        taken = _drain(pacer)

        assert [frame.pcm[0] for frame in taken] == [6, 7, 8, 9]
        assert pacer.stats["dropped_audio"] == 6


class TestTheBacklogIsTrimmedWithSilence:
    """Latency is given back during the pauses, never out of the middle of a word."""

    def test_silence_is_discarded_while_a_backlog_exists(
        self, frame_ctx: FrameContext
    ) -> None:
        """A pause that has already happened is not worth spending latency on.

        The trim brings the backlog down *to* the threshold rather than to zero: below it
        there is nothing to catch up on, and a jitter buffer with no jitter left in it
        underruns on the next hiccup.
        """
        clock = _aged_clock()
        pacer = _pacer(frame_ctx, clock)
        for _ in range(TRIM_FRAMES + 10):
            _submit(pacer, frame_ctx, SILENCE, pts_us=clock.now_us())
        _submit(pacer, frame_ctx, SPEECH, pts_us=clock.now_us())

        taken = _drain(pacer)

        assert pacer.stats["audio_backlog_silence"] >= 10, "the pause was not shortened"
        # The words are all still there, and they arrive sooner than the silence would
        # have let them.
        assert taken[-1].pcm == SPEECH
        assert len(taken) <= TRIM_FRAMES + 1

    def test_silence_is_kept_when_there_is_no_backlog(self, frame_ctx: FrameContext) -> None:
        """Gaps between utterances are part of the timeline. Dropping them with an empty
        queue would compress the pause into nothing and run the words together."""
        clock = _aged_clock()
        pacer = _pacer(frame_ctx, clock)
        _submit(pacer, frame_ctx, SILENCE, pts_us=clock.now_us())

        frame = pacer._take_audio()

        assert frame is not None
        assert frame.pcm == SILENCE
        assert "audio_backlog_silence" not in pacer.stats

    def test_speech_is_never_trimmed_however_deep_the_backlog(
        self, frame_ctx: FrameContext
    ) -> None:
        """The whole point of trimming silence is that speech does not have to be touched."""
        clock = _aged_clock()
        pacer = _pacer(frame_ctx, clock)
        for _ in range(40):
            _submit(pacer, frame_ctx, SPEECH, pts_us=clock.now_us())

        taken = _drain(pacer)

        assert len(taken) == 40
        assert "audio_backlog_silence" not in pacer.stats


class TestTheLossIsVisible:
    """It ran undiagnosed because every drop went to a metrics collector nobody scrapes.

    ``capsys`` rather than ``caplog``: this project's structlog configuration writes to
    stdout, and a test that asserted on the logging module's records would pass while the
    operator's terminal stayed empty — which is the exact failure being fixed.
    """

    @pytest.fixture
    def warning_logging(self):
        configure_logging(
            ObservabilitySettings(log_level="WARNING", json_logs=True), env=Environment.LOCAL
        )
        yield
        configure_logging(
            ObservabilitySettings(log_level="WARNING", json_logs=True), env=Environment.LOCAL
        )

    def test_overflow_is_reported_with_the_setting_that_fixes_it(
        self, frame_ctx: FrameContext, warning_logging: None, capsys
    ) -> None:
        clock = _aged_clock()
        pacer = _pacer(frame_ctx, clock, audio_queue_size=2)
        for index in range(20):
            _submit(pacer, frame_ctx, bytes([index, 0]) * 960, pts_us=clock.now_us())

        pacer._report_audio_loss()

        out = capsys.readouterr().out
        assert "pacer.audio_lost" in out
        assert "MC_MEDIA__AUDIO_QUEUE_SIZE" in out

    def test_a_healthy_pipeline_says_nothing(
        self, frame_ctx: FrameContext, warning_logging: None, capsys
    ) -> None:
        clock = _aged_clock()
        pacer = _pacer(frame_ctx, clock)
        _submit(pacer, frame_ctx, SPEECH, pts_us=clock.now_us())

        pacer._report_audio_loss()

        assert capsys.readouterr().out == ""

    def test_the_report_is_rate_limited(
        self, frame_ctx: FrameContext, warning_logging: None, capsys
    ) -> None:
        """The condition that produces loss produces it fifty times a second."""
        clock = _aged_clock()
        pacer = _pacer(frame_ctx, clock, audio_queue_size=2)

        for _ in range(5):
            for index in range(20):
                _submit(pacer, frame_ctx, bytes([index, 0]) * 960, pts_us=clock.now_us())
            pacer._report_audio_loss()

        assert capsys.readouterr().out.count("pacer.audio_lost") == 1

    def test_the_backlog_is_exposed_for_a_health_read(self, frame_ctx: FrameContext) -> None:
        clock = _aged_clock()
        pacer = _pacer(frame_ctx, clock)
        _submit(pacer, frame_ctx, SPEECH, pts_us=clock.now_us())

        assert pacer.stats["audio_backlog"] == 1
