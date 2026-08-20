"""Speaking takes the floor exactly as raising a hand does.

**The behaviour this pins, and why it is expressed as "same as a hand".** A live session
showed the two halves are both load-bearing: an interruption that only stopped the pacer went
quiet for the length of the hold and then carried on talking, because the agent had never been
told and was still generating the rest of its sentence. A raised hand stopped it for good,
because it does both — drop what is queued, *and* tell the agent, which is what makes the
avatar say "ok, go ahead". So a voice runs that same handover rather than a second mechanism
that has to be kept in step with it.

Two levels, because the trigger and the handover fail differently:

* the detector must not fire on a cough and must not let go between syllables — a false
  trigger stops the avatar for nothing, a premature release hands the floor back mid-question;
* the router must produce the identical two actions for a voice that it does for a hand.
"""

from __future__ import annotations

import asyncio
import contextlib
import struct
from collections.abc import AsyncIterator

from src.avatar.client import AvatarClient
from src.domain.avatar import AVATAR_INPUT_FORMAT
from src.domain.context import FrameContext
from src.domain.health import ComponentHealth
from src.domain.media import AudioFormat, AudioFrame, VideoFormat
from src.domain.meeting import HandRaise
from src.services.media.clock import MediaClock
from src.services.media.decode_pipeline import DecodePipeline
from src.services.media.echo_guard import EchoGuard
from src.services.media.idle_source import IdleFrameSource
from src.services.media.pacer import Pacer
from src.services.media.router import MediaRouter
from src.services.media.speech_detector import (
    DEFAULT_RMS_THRESHOLD,
    MIN_SPEECH_MS,
    SpeechDetector,
    rms,
)
from tests.fakes.avatar import FakeAvatarTransport
from tests.fakes.decoder import FakeDecoder
from tests.fakes.hand_raise import ScriptedHandRaiseSource

PUBLISH_AUDIO = AudioFormat(sample_rate_hz=48_000, channels=1)
VIDEO = VideoFormat(width=320, height=180, fps=10)

SAMPLES = 320  # 20 ms at the avatar's fixed 16 kHz input rate
SILENCE = bytes(SAMPLES * 2)
ROOM_TONE = struct.pack(f"<{SAMPLES}h", *([120, -120] * (SAMPLES // 2)))
"""A real microphone in a real room: never digitally silent, never speech."""
SPEECH = struct.pack(f"<{SAMPLES}h", *([4_000, -4_000] * (SAMPLES // 2)))
PROMPT = "Someone raised their hand and wants to say something. Stop talking and say ok, go ahead."
"""Quote-free on purpose: the frame is JSON, so a prompt containing double quotes reaches the
wire escaped and a naive substring assertion would fail on the escaping rather than on the
behaviour."""


def _frame(pcm: bytes, ctx: FrameContext) -> AudioFrame:
    return AudioFrame(pcm=pcm, pts_us=0, format=AVATAR_INPUT_FORMAT, ctx=ctx)


def _feed(detector: SpeechDetector, pcm: bytes, ctx: FrameContext, *, ms: int) -> list[bool]:
    """Push ``ms`` of ``pcm`` through in 20 ms frames, returning the rising edges."""
    return [detector.observe(_frame(pcm, ctx)) for _ in range(ms // 20)]


class TestDetection:
    def test_room_tone_never_takes_the_floor(self, frame_ctx: FrameContext) -> None:
        """Why this measures a level rather than reusing the pacer's "any energy at all"."""
        assert rms(SILENCE) == 0.0
        assert rms(ROOM_TONE) < DEFAULT_RMS_THRESHOLD < rms(SPEECH)

        detector = SpeechDetector()
        assert not any(_feed(detector, ROOM_TONE, frame_ctx, ms=3_000))
        assert not detector.is_speaking

    def test_a_cough_is_too_short(self, frame_ctx: FrameContext) -> None:
        """Transients are as loud as speech; the duration is what separates them."""
        detector = SpeechDetector()

        edges = _feed(detector, SPEECH, frame_ctx, ms=MIN_SPEECH_MS - 20)
        edges += _feed(detector, SILENCE, frame_ctx, ms=600)

        assert not any(edges)
        assert not detector.is_speaking

    def test_speech_fires_once_and_only_once(self, frame_ctx: FrameContext) -> None:
        """One handover per utterance. Fifty a second would be fifty interruptions."""
        detector = SpeechDetector()

        edges = _feed(detector, SPEECH, frame_ctx, ms=2_000)

        assert edges.count(True) == 1
        assert edges.index(True) == MIN_SPEECH_MS // 20 - 1  # the frame that completes it
        assert detector.is_speaking

    def test_the_dips_inside_speech_do_not_release_the_floor(
        self, frame_ctx: FrameContext
    ) -> None:
        """Hysteresis. Without it the detector flaps mid-sentence and the avatar resumes
        between somebody's words, then stops again."""
        detector = SpeechDetector(rms_threshold=1_000)
        dip = struct.pack(f"<{SAMPLES}h", *([700, -700] * (SAMPLES // 2)))
        assert rms(dip) < 1_000, "the dip must not reach the trigger, or this proves nothing"

        assert not any(_feed(detector, dip, frame_ctx, ms=600)), "a dip cannot start a turn"
        _feed(detector, SPEECH, frame_ctx, ms=400)

        assert not any(_feed(detector, dip, frame_ctx, ms=600)), "and must not restart one"
        assert detector.is_speaking, "but it must keep the floor"

    def test_the_turn_ends_and_the_next_one_is_new(self, frame_ctx: FrameContext) -> None:
        detector = SpeechDetector()
        _feed(detector, SPEECH, frame_ctx, ms=400)

        _feed(detector, SILENCE, frame_ctx, ms=600)
        assert not detector.is_speaking

        assert any(_feed(detector, SPEECH, frame_ctx, ms=400))

    def test_a_noisy_room_raises_its_own_bar(self, frame_ctx: FrameContext) -> None:
        """A fixed threshold is wrong in every room but the one it was tuned in, and being
        too high fails intermittently — which is indistinguishable from being broken."""
        detector = SpeechDetector(rms_threshold=350)
        assert detector.trigger_level == 350  # nothing learned yet: the floor stands

        # Below the absolute floor, so it is never speech — but loud enough that the room it
        # describes should demand more of a real voice than a silent room would.
        hum = struct.pack(f"<{SAMPLES}h", *([200, -200] * (SAMPLES // 2)))
        assert not any(_feed(detector, hum, frame_ctx, ms=4_000))

        assert detector.noise_floor > 150
        assert detector.trigger_level > 500, "a noisy room must raise its own bar"

        # ...and a voice that would have cleared the bare floor no longer clears this room's.
        borderline = struct.pack(f"<{SAMPLES}h", *([400, -400] * (SAMPLES // 2)))
        assert not any(_feed(detector, borderline, frame_ctx, ms=400))

    def test_a_quiet_voice_in_a_quiet_room_is_still_speech(
        self, frame_ctx: FrameContext
    ) -> None:
        """The other side of the same coin: soft speech far above a silent room."""
        quiet_voice = struct.pack(f"<{SAMPLES}h", *([500, -500] * (SAMPLES // 2)))
        detector = SpeechDetector()
        _feed(detector, SILENCE, frame_ctx, ms=2_000)

        assert any(_feed(detector, quiet_voice, frame_ctx, ms=400))


# --------------------------------------------------------------------------- #
# Through the real router
# --------------------------------------------------------------------------- #


class SpeakingSource:
    """Ingest that delivers speech, or room tone, forever."""

    def __init__(self, ctx: FrameContext, *, pcm: bytes = SPEECH) -> None:
        self._ctx = ctx
        self._pcm = pcm

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    async def frames(self) -> AsyncIterator[AudioFrame]:
        while True:
            yield _frame(self._pcm, self._ctx)
            await asyncio.sleep(0.005)

    def health(self) -> ComponentHealth:
        return ComponentHealth.healthy("speaking-source")


class NullSink:
    async def publish_audio(self, frame: object) -> None: ...
    async def publish_video(self, frame: object) -> None: ...

    def health(self) -> ComponentHealth:
        return ComponentHealth.healthy("null")


def _router(
    ctx: FrameContext,
    *,
    speech: SpeechDetector | None,
    pcm: bytes = SPEECH,
    hands: ScriptedHandRaiseSource | None = None,
    speaker_provider: object | None = None,
    voice_prompt_template: str = "",
) -> tuple[MediaRouter, Pacer, FakeAvatarTransport]:
    clock = MediaClock()
    pacer = Pacer(
        ctx=ctx,
        clock=clock,
        sink=NullSink(),
        idle=IdleFrameSource(ctx=ctx, video_format=VIDEO, audio_format=PUBLISH_AUDIO),
        video_format=VIDEO,
        audio_format=PUBLISH_AUDIO,
        echo_guard=EchoGuard(per_participant_audio=False, gate_enabled=False),
    )
    # ``echo_after`` beyond anything a test sends is how an avatar that never speaks of its
    # own accord is expressed: no fMP4 arrives, so the decoder never starts.
    transport = FakeAvatarTransport(ctx=ctx, echo_after=10_000)
    router = MediaRouter(
        ctx=ctx,
        clock=clock,
        source=SpeakingSource(ctx, pcm=pcm),
        avatar=AvatarClient(transport=transport, ctx=ctx),
        decode=DecodePipeline(
            decoder=FakeDecoder(ctx=ctx, video_format=VIDEO, audio_format=PUBLISH_AUDIO),
            ctx=ctx,
        ),
        pacer=pacer,
        echo_guard=EchoGuard(per_participant_audio=False, gate_enabled=False),
        hands=hands,
        hand_raise_mute_ms=800,
        speech=speech,
        voice_prompt=PROMPT,
        speaker_provider=speaker_provider,  # type: ignore[arg-type]
        voice_prompt_template=voice_prompt_template,
    )
    return router, pacer, transport


@contextlib.asynccontextmanager
async def _running(router: MediaRouter) -> AsyncIterator[None]:
    task = asyncio.create_task(router.run(), name="router-under-test")
    try:
        await asyncio.sleep(0.05)  # let the legs start
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, BaseExceptionGroup):
            await task


@contextlib.asynccontextmanager
async def _avatar_talking(pacer: Pacer, ctx: FrameContext) -> AsyncIterator[None]:
    """Keep the avatar mid-sentence for the body of the test.

    A single submitted frame would not do it: ``is_speaking`` reflects the *last published*
    frame, so one frame makes the avatar audible for 20 ms — long before the detector has
    heard the speech it needs. A real avatar streams continuously, which is precisely why the
    interrupt has to drop what is queued.
    """
    loud = struct.pack("<960h", *([8_000] * 960))  # 20 ms at 48 kHz, unmistakably audible

    async def stream() -> None:
        while True:
            pacer.submit_audio(AudioFrame(pcm=loud, pts_us=0, format=PUBLISH_AUDIO, ctx=ctx))
            await asyncio.sleep(0.005)

    task = asyncio.create_task(stream(), name="avatar-talking")
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def _wait_for(predicate, *, deadline_s: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + deadline_s
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)


class TestTheHandover:
    async def test_speaking_stops_the_avatar_and_tells_the_agent(
        self, frame_ctx: FrameContext
    ) -> None:
        """Both halves, which is the whole point.

        The mute is what stops the sentence already in flight; the frame to the agent is what
        stops it *generating* the rest, and is why the avatar says "ok, go ahead" rather than
        falling silent for 800 ms and then carrying on.
        """
        router, pacer, transport = _router(frame_ctx, speech=SpeechDetector())
        async with _avatar_talking(pacer, frame_ctx), _running(router):
            await _wait_for(lambda: len(transport.sent_control) > 0)

            assert pacer.is_muted
            assert pacer.stats["interrupted"] >= 1
            assert PROMPT in transport.sent_control[0]
            assert router.stats["hand_raises_forwarded"] == 1

    async def test_it_is_the_same_handover_a_raised_hand_gets(
        self, frame_ctx: FrameContext
    ) -> None:
        """Voice and hand must be indistinguishable downstream — that is the design.

        Asserted by running the hand path in the same harness and comparing what came out:
        the same frame kind on the same channel, the same mute. If the two ever diverge, this
        is where it shows up, and divergence is what the first attempt got wrong.
        """
        hands = ScriptedHandRaiseSource(
            [HandRaise(participant="Priya", prompt=PROMPT, raised_at_us=0)]
        )
        by_hand, hand_pacer, hand_transport = _router(
            frame_ctx, speech=None, pcm=ROOM_TONE, hands=hands
        )
        async with _avatar_talking(hand_pacer, frame_ctx), _running(by_hand):
            await _wait_for(lambda: len(hand_transport.sent_control) > 0)

        by_voice, voice_pacer, voice_transport = _router(frame_ctx, speech=SpeechDetector())
        async with _avatar_talking(voice_pacer, frame_ctx), _running(by_voice):
            await _wait_for(lambda: len(voice_transport.sent_control) > 0)

        assert PROMPT in hand_transport.sent_control[0]
        assert PROMPT in voice_transport.sent_control[0]
        assert by_hand.stats["hand_raises_forwarded"] == 1
        assert by_voice.stats["hand_raises_forwarded"] == 1
        assert hand_pacer.is_muted and voice_pacer.is_muted

    async def test_the_floor_is_held_while_they_keep_talking(
        self, frame_ctx: FrameContext
    ) -> None:
        """The hold is renewed frame by frame, so it lasts as long as the *question* does.

        Without this the 800 ms window expires mid-sentence and whatever the agent has
        produced since is published on top of the person still speaking.
        """
        router, pacer, _ = _router(frame_ctx, speech=SpeechDetector())
        async with _avatar_talking(pacer, frame_ctx), _running(router):
            await _wait_for(lambda: pacer.stats["interrupted"] >= 1)
            await asyncio.sleep(1.0)  # well past the 800 ms hold

            assert pacer.is_muted, "the hold lapsed while somebody was still speaking"
            assert pacer.stats["audio_interrupted"] > 0

    async def test_one_handover_per_utterance(self, frame_ctx: FrameContext) -> None:
        """Continuous speech is one interruption, not fifty a second."""
        router, pacer, transport = _router(frame_ctx, speech=SpeechDetector())
        async with _avatar_talking(pacer, frame_ctx), _running(router):
            await _wait_for(lambda: len(transport.sent_control) > 0)
            await asyncio.sleep(0.4)

            assert len(transport.sent_control) == 1

    async def test_a_silent_avatar_is_not_interrupted_by_somebody_talking_to_it(
        self, frame_ctx: FrameContext
    ) -> None:
        """**Somebody speaking to a silent avatar is a question, not an interruption.**

        Note that every other test in this class wraps its body in ``_avatar_talking``: the
        intent was always that a voice interrupts a *speaking* avatar, and the check was
        simply never written. A live Zoom-web meeting is what surfaced it, because browser
        ingest is the first connector whose echo gate is open enough for this leg to fire at
        all.

        What it looked like: ``router.floor_yielded ... trigger=voice was_speaking=False`` on
        every utterance, so the agent was handed "somebody wants to say something, reply
        briefly like ok, go ahead" *in front of* the question it was being asked. It dutifully
        said "Ok, go ahead." and then answered — on every single turn.

        The rule is the one doc 008 §4 already states for a hand versus a voice: a hand
        interrupts a silent avatar, a voice does not.
        """
        router, pacer, transport = _router(frame_ctx, speech=SpeechDetector())
        async with _running(router):  # deliberately no `_avatar_talking`
            await _wait_for(lambda: router.stats["forwarded"] > 20)

            assert transport.sent_control == [], "nothing was being said to interrupt"
            assert not pacer.is_muted
            assert router.stats["forwarded"] > 20, "and the speech still reached the agent"

    async def test_the_detector_stays_calibrated_while_the_avatar_is_silent(
        self, frame_ctx: FrameContext
    ) -> None:
        """The gate must not starve the detector of the quiet it learns from.

        Applying it by skipping ``observe`` would leave the noise floor at its initial value
        through the whole silent stretch, so the first frame after the avatar started talking
        would read as a barge-in — turning the fix into a differently-shaped version of the
        same bug.
        """
        detector = SpeechDetector()
        router, _pacer, transport = _router(frame_ctx, speech=detector, pcm=ROOM_TONE)
        async with _running(router):
            await _wait_for(lambda: router.stats["forwarded"] > 30)

            assert detector.noise_floor > 0, "the room was never measured"
            assert transport.sent_control == []

    async def test_a_quiet_meeting_interrupts_nothing(self, frame_ctx: FrameContext) -> None:
        router, pacer, transport = _router(frame_ctx, speech=SpeechDetector(), pcm=ROOM_TONE)
        async with _avatar_talking(pacer, frame_ctx), _running(router):
            await asyncio.sleep(0.4)

            assert transport.sent_control == []
            assert not pacer.is_muted

    async def test_without_a_detector_the_inbound_leg_is_unchanged(
        self, frame_ctx: FrameContext
    ) -> None:
        """Zoom and Teams pass nothing, so speech routes exactly as it always did."""
        router, pacer, transport = _router(frame_ctx, speech=None)
        async with _avatar_talking(pacer, frame_ctx), _running(router):
            await _wait_for(lambda: router.stats["forwarded"] > 5)

            assert transport.sent_control == []
            assert not pacer.is_muted
            assert router.stats["forwarded"] > 5


NAMED_TEMPLATE = "{name} wants to say something. Stop talking and let them speak."


class TestWhoInterrupted:
    """Naming the person who took the floor by talking.

    The inbound mix carries no attribution on any connector, so this leg has always reported
    "Someone" — true, and the least useful thing an interruption can say to an agent that is
    about to answer whoever just spoke. A connector able to identify the speaker *beside* the
    media path may now supply the name, and the important half of that sentence is "beside":
    nothing here changes what is on the audio path, and the lookup happens on the frame that
    already triggered the barge-in.
    """

    async def test_the_speaker_is_named_when_the_connector_knows_them(
        self, frame_ctx: FrameContext
    ) -> None:
        router, pacer, transport = _router(
            frame_ctx,
            speech=SpeechDetector(),
            speaker_provider=lambda: "Priya Menon",
            voice_prompt_template=NAMED_TEMPLATE,
        )
        async with _avatar_talking(pacer, frame_ctx), _running(router):
            await _wait_for(lambda: len(transport.sent_control) > 0)

            frame = transport.sent_control[0]
            assert "Priya Menon wants to say something" in frame
            assert "Someone" not in frame

    async def test_an_unknown_speaker_still_takes_the_floor(
        self, frame_ctx: FrameContext
    ) -> None:
        """A miss costs the name and never the handover — the two are decided independently,
        which is what stops attribution from ever delaying the avatar falling silent."""
        router, pacer, transport = _router(
            frame_ctx,
            speech=SpeechDetector(),
            speaker_provider=lambda: None,
            voice_prompt_template=NAMED_TEMPLATE,
        )
        async with _avatar_talking(pacer, frame_ctx), _running(router):
            await _wait_for(lambda: len(transport.sent_control) > 0)

            assert PROMPT in transport.sent_control[0]
            assert pacer.stats["interrupted"] >= 1

    async def test_a_provider_that_raises_costs_the_name_and_nothing_else(
        self, frame_ctx: FrameContext
    ) -> None:
        """This runs on the inbound audio leg. An exception escaping it would take the leg
        down, and with it the meeting's audio in both directions — for a display name."""

        def broken() -> str:
            raise RuntimeError("the tracker is unhappy")

        router, pacer, transport = _router(
            frame_ctx,
            speech=SpeechDetector(),
            speaker_provider=broken,
            voice_prompt_template=NAMED_TEMPLATE,
        )
        async with _avatar_talking(pacer, frame_ctx), _running(router):
            await _wait_for(lambda: len(transport.sent_control) > 0)

            assert PROMPT in transport.sent_control[0]
            assert router.stats["forwarded"] > 0, "the inbound leg kept running"

    async def test_a_broken_template_costs_the_wording_rather_than_the_feature(
        self, frame_ctx: FrameContext
    ) -> None:
        """The template is operator-supplied, so a stray brace must not reach the agent as an
        exception on the audio leg — the same trade ``render_prompt`` makes."""
        router, pacer, transport = _router(
            frame_ctx,
            speech=SpeechDetector(),
            speaker_provider=lambda: "Priya Menon",
            voice_prompt_template="{nome} said {",
        )
        async with _avatar_talking(pacer, frame_ctx), _running(router):
            await _wait_for(lambda: len(transport.sent_control) > 0)

            assert PROMPT in transport.sent_control[0]
