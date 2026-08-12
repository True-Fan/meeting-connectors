"""A typed message reaches the agent through the real router, and audio survives it.

Unit tests can prove ``send_chat`` builds the right frame; only running ``MediaRouter.run``
proves the chat leg is actually started, that its failures are contained, and that adding it did
not disturb the four media legs. That distinction is the lesson
``test_avatar_leg_startup.py`` records: ``AvatarClient`` had a complete lifecycle API that
nothing called, and no unit test could notice.

So these run the real router with a real avatar client, and assert on what cannot be faked —
that the frame left the process, and that audio kept flowing while it did.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

from src.avatar.client import AvatarClient
from src.domain.avatar import AVATAR_INPUT_FORMAT, AvatarChatMessage
from src.domain.context import FrameContext
from src.domain.health import ComponentHealth
from src.domain.media import AudioFormat, AudioFrame, VideoFormat
from src.domain.meeting import ChatMessage
from src.services.media.clock import MediaClock
from src.services.media.decode_pipeline import DecodePipeline
from src.services.media.echo_guard import EchoGuard
from src.services.media.idle_source import IdleFrameSource
from src.services.media.pacer import Pacer
from src.services.media.router import MediaRouter
from tests.fakes.avatar import FakeAvatarTransport
from tests.fakes.chat import ScriptedChatSource
from tests.fakes.decoder import FakeDecoder

PUBLISH_AUDIO = AudioFormat(sample_rate_hz=48_000, channels=1)
VIDEO = VideoFormat(width=320, height=180, fps=10)
PCM_FRAME = bytes(640)  # 20 ms at 16 kHz mono


class TrickleSource:
    """Ingest that keeps yielding audio, so the inbound leg stays alive."""

    def __init__(self, ctx: FrameContext) -> None:
        self._ctx = ctx
        self.yielded = 0

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    async def frames(self) -> AsyncIterator[AudioFrame]:
        while True:
            self.yielded += 1
            yield AudioFrame(
                pcm=PCM_FRAME, pts_us=0, format=AVATAR_INPUT_FORMAT, ctx=self._ctx
            )
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


def _router(
    ctx: FrameContext, chat: ScriptedChatSource | None
) -> tuple[MediaRouter, AvatarClient, FakeAvatarTransport, TrickleSource]:
    clock = MediaClock()
    transport = FakeAvatarTransport(ctx=ctx)
    avatar = AvatarClient(transport=transport, ctx=ctx)
    source = TrickleSource(ctx)
    pacer = Pacer(
        ctx=ctx,
        clock=clock,
        sink=NullSink(),
        idle=IdleFrameSource(ctx=ctx, video_format=VIDEO, audio_format=PUBLISH_AUDIO),
        video_format=VIDEO,
        audio_format=PUBLISH_AUDIO,
        echo_guard=EchoGuard(per_participant_audio=True),
    )
    router = MediaRouter(
        ctx=ctx,
        clock=clock,
        source=source,
        avatar=avatar,
        decode=DecodePipeline(
            decoder=FakeDecoder(ctx=ctx, video_format=VIDEO, audio_format=PUBLISH_AUDIO),
            ctx=ctx,
        ),
        pacer=pacer,
        echo_guard=EchoGuard(per_participant_audio=True),
        chat=chat,
    )
    return router, avatar, transport, source


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
    """Poll until ``predicate`` holds.

    Named ``deadline_s`` rather than ``timeout`` on purpose: ASYNC109 flags a `timeout`
    parameter on an async function because it usually means the caller is reimplementing
    cancellation instead of using ``asyncio.timeout``. Here the wait is a poll for a
    condition another task will satisfy, which has no coroutine to wrap.
    """
    deadline = asyncio.get_running_loop().time() + deadline_s
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was never met")


class TestTheChatLegRuns:
    async def test_a_typed_message_reaches_the_transport(self, frame_ctx: FrameContext) -> None:
        """The assertion that matters: the frame left the process.

        Nothing short of running the router proves the leg was started — which is exactly the
        class of bug that left the avatar leg unconnected on every platform.
        """
        chat = ScriptedChatSource()
        router, _, transport, _ = _router(frame_ctx, chat)

        async with _running(router):
            chat.push(ChatMessage(text="what is the notice period?", sender="Priya"))
            await _wait_for(lambda: len(transport.sent_control) == 1)

        frame = AvatarChatMessage.model_validate_json(transport.sent_control[0])
        assert frame.text == "what is the notice period?"
        assert frame.sender == "Priya"
        assert router.stats["chat_forwarded"] == 1

    async def test_audio_keeps_flowing_while_chat_is_forwarded(
        self, frame_ctx: FrameContext
    ) -> None:
        """Chat must not be able to interrupt the meeting's audio."""
        chat = ScriptedChatSource()
        router, _, transport, _ = _router(frame_ctx, chat)

        async with _running(router):
            await _wait_for(lambda: len(transport.sent_pcm) > 3)
            before = len(transport.sent_pcm)
            chat.push(ChatMessage(text="still there?", sender="Dev"))
            await _wait_for(lambda: len(transport.sent_control) == 1)
            await _wait_for(lambda: len(transport.sent_pcm) > before + 3)

    async def test_the_avatars_own_message_is_not_forwarded(
        self, frame_ctx: FrameContext
    ) -> None:
        chat = ScriptedChatSource()
        router, _, transport, _ = _router(frame_ctx, chat)

        async with _running(router):
            chat.push(ChatMessage(text="Hello! I am Gunika", sender="AI", is_self=True))
            chat.push(ChatMessage(text="a real question", sender="Dev"))
            await _wait_for(lambda: len(transport.sent_control) == 1)

        frame = AvatarChatMessage.model_validate_json(transport.sent_control[0])
        assert frame.text == "a real question"
        assert router.stats["chat_forwarded"] == 1

    async def test_several_messages_are_forwarded_in_order(
        self, frame_ctx: FrameContext
    ) -> None:
        """A conversation is only coherent if questions keep their order."""
        chat = ScriptedChatSource()
        router, _, transport, _ = _router(frame_ctx, chat)

        async with _running(router):
            for text in ("first", "second", "third"):
                chat.push(ChatMessage(text=text, sender="Dev"))
            await _wait_for(lambda: len(transport.sent_control) == 3)

        texts = [
            AvatarChatMessage.model_validate_json(raw).text for raw in transport.sent_control
        ]
        assert texts == ["first", "second", "third"]


class TestChatCannotBreakTheSession:
    async def test_a_failing_send_does_not_kill_the_router(
        self, frame_ctx: FrameContext
    ) -> None:
        """The chat leg lives in the router's task group, so an escaping exception would
        cancel the media legs and take the meeting's audio down with it."""
        chat = ScriptedChatSource()
        router, _, transport, _ = _router(frame_ctx, chat)

        async def explode(payload: str) -> None:
            raise RuntimeError("agent socket went away")

        transport.send_control = explode  # type: ignore[method-assign]

        async with _running(router):
            await _wait_for(lambda: len(transport.sent_pcm) > 3)
            chat.push(ChatMessage(text="this will fail to send", sender="Dev"))
            await asyncio.sleep(0.1)

            # The router is still routing audio, which is the whole point.
            before = len(transport.sent_pcm)
            await _wait_for(lambda: len(transport.sent_pcm) > before + 3)
        assert router.stats["chat_forwarded"] == 0

    async def test_a_session_without_chat_runs_unchanged(
        self, frame_ctx: FrameContext
    ) -> None:
        """Zoom and Teams pass no chat source. Absence is 'this platform has no chat', not a
        fault, and must not start a leg or change the media path."""
        router, _, transport, _ = _router(frame_ctx, None)

        async with _running(router):
            await _wait_for(lambda: len(transport.sent_pcm) > 3)

        assert transport.sent_control == []
        assert router.stats["chat_forwarded"] == 0
