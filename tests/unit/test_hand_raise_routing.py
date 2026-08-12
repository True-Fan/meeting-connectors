"""A raised hand stops the avatar mid-sentence, through the real router.

Unit tests can prove ``send_interrupt`` builds the right frame and that ``Pacer.interrupt``
empties a queue. Only running ``MediaRouter.run`` proves the leg is actually started, that it
does *both* halves of a barge-in, and that adding it did not disturb the media legs — the same
distinction ``test_avatar_leg_startup.py`` records, where a complete lifecycle API was never
called and no unit test could notice.

The two halves are the point, and neither is sufficient:

* stopping locally without telling the agent means the sentence resumes the moment the hold
  lapses, because nothing told it to stop generating;
* telling the agent without stopping locally means the avatar talks over the person for a
  network round trip plus however long that agent takes to react.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

from src.avatar.client import AvatarClient
from src.domain.avatar import AVATAR_INPUT_FORMAT, AvatarChatMessage
from src.domain.context import FrameContext
from src.domain.health import ComponentHealth
from src.domain.media import AudioFormat, AudioFrame, VideoFormat, VideoFrame
from src.domain.meeting import HandRaise
from src.services.media.clock import MediaClock
from src.services.media.decode_pipeline import DecodePipeline
from src.services.media.echo_guard import EchoGuard
from src.services.media.idle_source import IdleFrameSource
from src.services.media.pacer import Pacer
from src.services.media.router import MediaRouter
from tests.fakes.avatar import FakeAvatarTransport
from tests.fakes.decoder import FakeDecoder
from tests.fakes.hand_raise import ScriptedHandRaiseSource

PUBLISH_AUDIO = AudioFormat(sample_rate_hz=48_000, channels=1)
VIDEO = VideoFormat(width=320, height=180, fps=10)
PCM_FRAME = bytes(640)  # 20 ms at 16 kHz mono

LOUD_PCM = (b"\x00\x40" * 480)  # 20 ms at 48 kHz, well above the pacer's silence floor


class TrickleSource:
    """Ingest that keeps yielding audio, so the inbound leg stays alive."""

    def __init__(self, ctx: FrameContext) -> None:
        self._ctx = ctx

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    async def frames(self) -> AsyncIterator[AudioFrame]:
        while True:
            yield AudioFrame(pcm=PCM_FRAME, pts_us=0, format=AVATAR_INPUT_FORMAT, ctx=self._ctx)
            await asyncio.sleep(0.005)

    def health(self) -> ComponentHealth:
        return ComponentHealth.healthy("trickle")


class NullSink:
    def __init__(self) -> None:
        self.audio = 0
        self.video = 0

    async def publish_audio(self, frame: object) -> None:
        self.audio += 1

    async def publish_video(self, frame: object) -> None:
        self.video += 1

    def health(self) -> ComponentHealth:
        return ComponentHealth.healthy("null")


def _pacer(ctx: FrameContext, clock: MediaClock) -> Pacer:
    return Pacer(
        ctx=ctx,
        clock=clock,
        sink=NullSink(),
        idle=IdleFrameSource(ctx=ctx, video_format=VIDEO, audio_format=PUBLISH_AUDIO),
        video_format=VIDEO,
        audio_format=PUBLISH_AUDIO,
        echo_guard=EchoGuard(per_participant_audio=True),
    )


def _router(
    ctx: FrameContext,
    hands: ScriptedHandRaiseSource | None,
    *,
    mute_ms: int = 800,
    echo_after: int = 1,
) -> tuple[MediaRouter, Pacer, FakeAvatarTransport]:
    clock = MediaClock()
    # ``echo_after`` beyond anything a test sends is how an avatar that never speaks is
    # expressed: no fMP4 arrives, so the decoder never starts and the pacer publishes idle.
    transport = FakeAvatarTransport(ctx=ctx, echo_after=echo_after)
    avatar = AvatarClient(transport=transport, ctx=ctx)
    pacer = _pacer(ctx, clock)
    router = MediaRouter(
        ctx=ctx,
        clock=clock,
        source=TrickleSource(ctx),
        avatar=avatar,
        decode=DecodePipeline(
            decoder=FakeDecoder(ctx=ctx, video_format=VIDEO, audio_format=PUBLISH_AUDIO),
            ctx=ctx,
        ),
        pacer=pacer,
        echo_guard=EchoGuard(per_participant_audio=True),
        hands=hands,
        hand_raise_mute_ms=mute_ms,
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


async def _wait_for(predicate, *, deadline_s: float = 2.0) -> None:
    """Poll until ``predicate`` holds. ``deadline_s`` rather than ``timeout`` — see
    ``test_chat_routing`` for why ASYNC109 makes that the right name here."""
    deadline = asyncio.get_running_loop().time() + deadline_s
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was never met")


class TestPacerBargeIn:
    """The local half, tested without the router: what is queued does not get published."""

    def test_queued_avatar_media_is_dropped(self, frame_ctx: FrameContext) -> None:
        clock = MediaClock()
        pacer = _pacer(frame_ctx, clock)
        now = clock.now_us()
        for index in range(3):
            pacer.submit_audio(
                AudioFrame(
                    pcm=LOUD_PCM,
                    pts_us=now + index * 20_000,
                    format=PUBLISH_AUDIO,
                    ctx=frame_ctx,
                )
            )

        assert pacer.interrupt() == 3
        assert pacer._take_audio() is None

    def test_media_decoded_during_the_hold_is_dropped_too(
        self, frame_ctx: FrameContext
    ) -> None:
        """The half that makes barge-in audible. The agent's speech is already in flight when
        the hand goes up, so emptying the queue alone buys one queue-depth of silence and then
        the same sentence resumes."""
        clock = MediaClock()
        pacer = _pacer(frame_ctx, clock)
        pacer.interrupt(hold_ms=5_000)

        assert pacer.is_muted is True
        pacer.submit_audio(
            AudioFrame(pcm=LOUD_PCM, pts_us=clock.now_us(), format=PUBLISH_AUDIO, ctx=frame_ctx)
        )
        pacer.submit_video(
            VideoFrame(
                planes=bytes(VIDEO.width * VIDEO.height * 3 // 2),
                pts_us=clock.now_us(),
                format=VIDEO,
                ctx=frame_ctx,
            )
        )

        assert pacer._take_audio() is None
        assert pacer._take_video() is None

    def test_a_hold_of_zero_only_drops_what_is_queued(self, frame_ctx: FrameContext) -> None:
        clock = MediaClock()
        pacer = _pacer(frame_ctx, clock)
        pacer.interrupt(hold_ms=0)

        assert pacer.is_muted is False
        frame = AudioFrame(
            pcm=LOUD_PCM, pts_us=clock.now_us(), format=PUBLISH_AUDIO, ctx=frame_ctx
        )
        pacer.submit_audio(frame)
        assert pacer._take_audio() is frame

    def test_a_second_hand_extends_the_hold_and_never_shortens_it(
        self, frame_ctx: FrameContext
    ) -> None:
        clock = MediaClock()
        pacer = _pacer(frame_ctx, clock)
        pacer.interrupt(hold_ms=5_000)
        pacer.interrupt(hold_ms=1)

        assert pacer.is_muted is True

    def test_interrupting_a_silent_avatar_is_harmless(self, frame_ctx: FrameContext) -> None:
        """A hand goes up whether or not the avatar happens to be mid-sentence, and the
        request is the same either way."""
        pacer = _pacer(frame_ctx, MediaClock())
        assert pacer.interrupt(hold_ms=100) == 0
        assert pacer.is_speaking is False


class TestTheHandRaiseLegRuns:
    async def test_a_raised_hand_reaches_the_agent(self, frame_ctx: FrameContext) -> None:
        """The assertion that matters: the frame left the process. Nothing short of running
        the router proves the leg was started."""
        hands = ScriptedHandRaiseSource()
        router, _, transport = _router(frame_ctx, hands)

        async with _running(router):
            hands.push(HandRaise(participant="Priya", prompt="Priya wants to speak."))
            await _wait_for(lambda: len(transport.sent_control) == 1)

        frame = AvatarChatMessage.model_validate_json(transport.sent_control[0])
        # A chat frame, byte for byte what a typed question produces — which is the whole
        # reason a raised hand reaches an agent nobody modified for it.
        assert frame.kind == "chat"
        assert frame.sender == "Priya"
        assert frame.text == "Priya wants to speak."
        assert router.stats["hand_raises_forwarded"] == 1

    async def test_the_avatar_is_stopped_locally_as_well_as_told(
        self, frame_ctx: FrameContext
    ) -> None:
        """Both halves, and the local one is not merely an optimisation: the round trip to the
        agent is a network hop, while the queues drain in real time regardless."""
        hands = ScriptedHandRaiseSource()
        router, pacer, transport = _router(frame_ctx, hands, mute_ms=5_000)

        async with _running(router):
            hands.push(HandRaise(participant="Dev", prompt="Dev wants to speak."))
            await _wait_for(lambda: len(transport.sent_control) == 1)
            assert pacer.is_muted is True
            assert pacer.is_speaking is False
        assert pacer.stats["interrupted"] == 1

    async def test_audio_keeps_flowing_while_the_floor_changes_hands(
        self, frame_ctx: FrameContext
    ) -> None:
        """Interrupting the avatar must not interrupt the meeting: the person who raised their
        hand is about to speak, and the agent has to hear them."""
        hands = ScriptedHandRaiseSource()
        router, _, transport = _router(frame_ctx, hands)

        async with _running(router):
            await _wait_for(lambda: len(transport.sent_pcm) > 3)
            before = len(transport.sent_pcm)
            hands.push(HandRaise(participant="Dev", prompt="Dev wants to speak."))
            await _wait_for(lambda: len(transport.sent_control) == 1)
            await _wait_for(lambda: len(transport.sent_pcm) > before + 3)

    async def test_a_silent_avatar_still_yields_the_floor(
        self, frame_ctx: FrameContext
    ) -> None:
        """"Stop talking" and "say go ahead" are one request. An avatar that only reacted when
        it happened to be mid-sentence would ignore anybody who waited for a pause."""
        hands = ScriptedHandRaiseSource()
        router, pacer, transport = _router(frame_ctx, hands, echo_after=10**9)

        async with _running(router):
            assert pacer.is_speaking is False
            hands.push(HandRaise(participant="Priya", prompt="Priya wants to speak."))
            await _wait_for(lambda: len(transport.sent_control) == 1)

        assert router.stats["hand_raises_forwarded"] == 1


class TestHandRaisesCannotBreakTheSession:
    async def test_a_failing_send_does_not_kill_the_router(
        self, frame_ctx: FrameContext
    ) -> None:
        """The leg lives in the router's task group, so an escaping exception would cancel the
        media legs and take the meeting's audio down with it."""
        hands = ScriptedHandRaiseSource()
        router, _, transport = _router(frame_ctx, hands)

        async def explode(payload: str) -> None:
            raise RuntimeError("agent socket went away")

        transport.send_control = explode  # type: ignore[method-assign]

        async with _running(router):
            await _wait_for(lambda: len(transport.sent_pcm) > 3)
            hands.push(HandRaise(participant="Dev", prompt="Dev wants to speak."))
            await asyncio.sleep(0.1)

            before = len(transport.sent_pcm)
            await _wait_for(lambda: len(transport.sent_pcm) > before + 3)
        assert router.stats["hand_raises_forwarded"] == 0

    async def test_a_session_without_the_feature_runs_unchanged(
        self, frame_ctx: FrameContext
    ) -> None:
        """Zoom and Teams pass no source. Absence is "this platform does not report raised
        hands", not a fault, and must not start a leg or change the media path."""
        router, pacer, transport = _router(frame_ctx, None)

        async with _running(router):
            await _wait_for(lambda: len(transport.sent_pcm) > 3)

        assert transport.sent_control == []
        assert router.stats["hand_raises_forwarded"] == 0
        assert pacer.stats["interrupted"] == 0
