"""Chat and PCM share one socket: both must arrive, intact and in order.

``FakeAvatarTransport`` records ``send_control`` into a list, which proves the client builds the
right frame and nothing about the transport. These run against a real socket
(``tests/fakes/avatar_server.py``) to assert what a double cannot show: that a text frame is
delivered as text rather than mistaken for media, that a burst of chat during continuous audio
leaves every PCM frame byte-for-byte intact and every chat frame parseable and ordered, and that
an undeliverable message is swallowed rather than raised into a live session.

**On the send lock in ``ws_transport``.** Two producers write to this socket — the writer task
draining the PCM queue, and ``send_control`` writing directly because chat must not be dropped.
The lock serialises them. Measured honestly: these tests **also pass with the lock removed**,
because ``websockets`` 15 serialises non-fragmented sends internally, so the lock is a guard
against a documented hazard rather than a fix for an observed corruption. It is kept because it
costs nothing and because ``send()`` *does* reject concurrent use for fragmented messages — the
moment anything here sends an iterable, the hazard becomes real. No test below should be read as
evidence that the lock is load-bearing today.
"""

from __future__ import annotations

import asyncio

import orjson
import pytest

from src.avatar.ws_transport import WebSocketAvatarTransport
from src.domain.avatar import AvatarChatMessage, AvatarClientHello
from src.domain.context import FrameContext
from src.services.media.clock import MediaClock
from tests.fakes.avatar_server import StubAvatarServer

PCM_FRAME = bytes(range(256)) * 2  # 512 bytes, recognisable content


@pytest.fixture
async def server():
    stub = StubAvatarServer()
    await stub.start()
    yield stub
    await stub.stop()


async def _connected(server: StubAvatarServer, ctx: FrameContext) -> WebSocketAvatarTransport:
    transport = WebSocketAvatarTransport(
        url=server.url, ctx=ctx, clock=MediaClock(), open_timeout_s=5.0
    )
    await transport.connect(
        AvatarClientHello(session_id=ctx.session_id, correlation_id=ctx.correlation_id)
    )
    return transport


class TestControlFramesOverARealSocket:
    async def test_a_chat_frame_arrives_as_text(
        self, server: StubAvatarServer, frame_ctx: FrameContext
    ) -> None:
        transport = await _connected(server, frame_ctx)
        try:
            frame = AvatarChatMessage(text="what is the notice period?", sender="Priya")
            await transport.send_control(frame.model_dump_json())
            await asyncio.sleep(0.1)
        finally:
            await transport.close()

        # The stub records binary as PCM; a text frame must not land there.
        assert server.received_pcm == []
        assert len(server.received_text) == 1
        decoded = orjson.loads(server.received_text[0])
        assert decoded["kind"] == "chat"
        assert decoded["text"] == "what is the notice period?"
        assert decoded["sender"] == "Priya"

    async def test_pcm_and_chat_interleave_without_corruption(
        self, server: StubAvatarServer, frame_ctx: FrameContext
    ) -> None:
        """Audio streams continuously while chat is written from another task.

        Every PCM frame must arrive byte-for-byte and every chat frame must be parseable and in
        order. This is the end-to-end integrity check for two producers on one socket; see the
        module docstring for why it does not, on its own, justify the send lock.
        """
        transport = await _connected(server, frame_ctx)
        try:
            async def stream_audio() -> None:
                for _ in range(60):
                    await transport.send_pcm(PCM_FRAME)
                    await asyncio.sleep(0.002)

            async def stream_chat() -> None:
                for index in range(15):
                    await transport.send_control(
                        AvatarChatMessage(text=f"question {index}", sender="Dev").model_dump_json()
                    )
                    await asyncio.sleep(0.005)

            await asyncio.gather(stream_audio(), stream_chat())
            await asyncio.sleep(0.2)
        finally:
            await transport.close()

        assert server.received_pcm, "no audio arrived at all"
        assert all(payload == PCM_FRAME for payload in server.received_pcm), (
            "a PCM frame arrived corrupted — writes interleaved on the socket"
        )

        assert len(server.received_text) == 15
        texts = []
        for raw in server.received_text:
            texts.append(orjson.loads(raw)["text"])  # raises if a frame was torn
        assert texts == [f"question {index}" for index in range(15)], (
            "chat frames arrived out of order or incomplete"
        )

    async def test_chat_before_connecting_is_dropped_not_raised(
        self, frame_ctx: FrameContext
    ) -> None:
        """A message that cannot be delivered must not take down a session."""
        transport = WebSocketAvatarTransport(
            url="ws://127.0.0.1:1/stream", ctx=frame_ctx, clock=MediaClock()
        )
        await transport.send_control('{"kind":"chat","text":"hi"}')

    async def test_a_dead_socket_does_not_raise(
        self, server: StubAvatarServer, frame_ctx: FrameContext
    ) -> None:
        transport = await _connected(server, frame_ctx)
        await server.stop()
        await asyncio.sleep(0.05)

        # Swallowed and logged: the session may still be carrying a conversation.
        await transport.send_control('{"kind":"chat","text":"after close"}')
        await transport.close()
