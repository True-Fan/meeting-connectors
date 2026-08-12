"""Avatar agent doubles.

``FakeAvatarTransport`` is the second implementation that justifies the
``AvatarTransport`` port (doc 003 §0): it exercises the client's protocol logic —
handshake validation, init-segment caching, backpressure accounting — with no socket
and no avatar service.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from src.avatar.framing import Fmp4Framer
from src.domain.avatar import (
    AVATAR_PROTOCOL_VERSION,
    AvatarClientHello,
    AvatarServerHello,
)
from src.domain.context import FrameContext
from src.domain.health import ComponentHealth, ComponentState
from src.domain.media import MediaChunk
from tests.fakes import mp4


class FakeAvatarTransport:
    """In-memory ``AvatarTransport``.

    Args:
        reply: Handshake reply to return. Defaults to a compatible one.
        response: fMP4 bytes to stream back after the first PCM arrives.
        fail_on_connect: Raise from ``connect`` to exercise reconnect paths.
        echo_after: Emit the response only once this many PCM frames have arrived.
    """

    def __init__(
        self,
        *,
        ctx: FrameContext,
        reply: AvatarServerHello | None = None,
        response: bytes | None = None,
        fail_on_connect: Exception | None = None,
        echo_after: int = 1,
    ) -> None:
        self._ctx = ctx
        # The bridge's own version by default, so the fake stands in for a *current* agent and
        # negotiates every feature. Pass an explicit older reply to exercise the paths where a
        # feature must be withheld.
        self._reply = reply or AvatarServerHello(protocol_version=str(AVATAR_PROTOCOL_VERSION))
        self._response = response if response is not None else mp4.stream(3)
        self._fail_on_connect = fail_on_connect
        self._echo_after = echo_after

        self.sent_pcm: list[bytes] = []
        self.sent_control: list[str] = []
        """JSON control frames — chat, so far. Kept as raw strings so a test asserts on what
        actually went over the wire rather than on a re-parsed object."""
        self.hellos: list[AvatarClientHello] = []
        self.connect_calls = 0
        self.closed = False

        self._framer = Fmp4Framer(ctx=ctx)
        self._chunks: asyncio.Queue[MediaChunk | None] = asyncio.Queue()
        self._state = ComponentState.UNKNOWN

    async def connect(self, hello: AvatarClientHello) -> AvatarServerHello:
        self.connect_calls += 1
        if self._fail_on_connect is not None:
            raise self._fail_on_connect
        self.hellos.append(hello)
        self.closed = False
        self._state = ComponentState.HEALTHY
        return self._reply

    async def close(self) -> None:
        self.closed = True
        self._state = ComponentState.UNKNOWN
        await self._chunks.put(None)

    async def send_pcm(self, pcm: bytes) -> None:
        self.sent_pcm.append(pcm)
        if len(self.sent_pcm) == self._echo_after:
            self.emit(self._response)

    async def send_control(self, payload: str) -> None:
        self.sent_control.append(payload)

    def emit(self, data: bytes) -> None:
        """Push raw fMP4 bytes through the framer and queue the resulting chunks."""
        for chunk in self._framer.feed(data, received_at_us=0):
            self._chunks.put_nowait(chunk)

    def finish(self) -> None:
        """Signal end of stream."""
        self._chunks.put_nowait(None)

    async def chunks(self) -> AsyncIterator[MediaChunk]:
        while True:
            chunk = await self._chunks.get()
            if chunk is None:
                return
            yield chunk

    def fail(self, detail: str) -> None:
        """Report unhealthy, the way the real transport does when its agent goes away.

        Added for the Google Meet leg-state regression: a session must degrade when the avatar
        dies, and the only way to assert that is to be able to kill the avatar.
        """
        self._state = ComponentState.UNHEALTHY
        self._detail = detail

    def health(self) -> ComponentHealth:
        return ComponentHealth(
            name="fake_avatar_transport",
            state=self._state,
            detail=getattr(self, "_detail", None),
        )
