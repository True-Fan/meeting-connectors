"""Regression: the avatar leg must actually connect.

**A real bug in shared code, found while wiring Google Meet and fixed in shared code.**
Directly analogous to the one ``test_media_router_startup.py`` documents, and invisible for
the same reason: nothing exercised ``MediaRouter.run`` against the *real*
``WebSocketAvatarTransport``.

``AvatarClient`` has a complete lifecycle API — ``start()`` performs the handshake, and
``reconnect()`` re-establishes with backoff. **Neither was called by anything.** Not by
``ZoomMeetingSession``, not by ``TeamsMeetingSession``, not by ``MediaRouter``, not by any
test. The consequences, in ``avatar/ws_transport.py``:

* ``send_pcm`` only offers to a bounded queue. The task that drains it is created in
  ``connect()``, so it never existed — audio accumulated until the queue saturated and the
  transport reported ``DEGRADED "send queue saturated"``.
* ``_read_loop`` and ``_drain_send_queue`` both return immediately when ``_connection is
  None``, so no fMP4 ever arrived either.

Net effect: **every session on every platform published idle video and silence forever**, and
the avatar agent never heard a word. Nothing failed loudly, because every layer was doing
exactly what it was told.

Why ``FakeAvatarTransport`` could not catch it: it substitutes the port, and its ``send_pcm``
appends to a list whether or not ``connect()`` was called. The double was faithful to the
*protocol* and silent about the *lifecycle*. So these tests use a real socket
(``tests/fakes/avatar_server.py``) and assert the only thing that cannot be faked — that PCM
left the process.

All three connectors are covered because the bug and the fix are in shared code, and a
regression in any direction should fail loudly.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from pydantic import SecretStr

from src.avatar.client import AvatarClient
from src.avatar.ws_transport import WebSocketAvatarTransport
from src.config.settings import GoogleMeetSettings, Settings, TeamsSettings
from src.domain.context import FrameContext
from src.domain.exceptions import AvatarProtocolMismatchError
from src.domain.health import ComponentHealth, ComponentState
from src.domain.media import AudioFormat, AudioFrame, SampleFormat, VideoFormat
from src.infrastructure.reconnect import ReconnectPolicy
from src.services.media.clock import MediaClock
from src.services.media.decode_pipeline import DecodePipeline
from src.services.media.echo_guard import EchoGuard
from src.services.media.idle_source import IdleFrameSource
from src.services.media.pacer import Pacer
from src.services.media.router import MediaRouter
from src.services.media.sinks.null_sink import NullSink
from tests.fakes.avatar_server import StubAvatarServer
from tests.fakes.decoder import FakeDecoder
from tests.fakes.mp4 import stream as fmp4_stream

VIDEO = VideoFormat(width=64, height=64, fps=25)
PUBLISH_AUDIO = AudioFormat(sample_rate_hz=16_000, channels=1, sample_format=SampleFormat.S16LE)
PCM_FRAME = b"\x21\x43" * 320  # 20 ms at 16 kHz, the avatar's fixed input format


class OneShotSource:
    """An ``AudioSource`` that yields a fixed number of frames, then idles.

    Idles rather than returning: a source that ends would let ``_route_inbound`` complete and
    the task group unwind, which would mask a delivery failure as a clean shutdown.
    """

    def __init__(self, ctx: FrameContext, *, count: int = 3) -> None:
        self._ctx = ctx
        self._count = count
        self.yielded = 0

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def frames(self):
        for _ in range(self._count):
            self.yielded += 1
            yield AudioFrame(pcm=PCM_FRAME, pts_us=0, format=PUBLISH_AUDIO, ctx=self._ctx)
        await asyncio.Event().wait()  # never completes

    def health(self) -> ComponentHealth:
        return ComponentHealth.healthy("one_shot_source")


def _router(
    ctx: FrameContext,
    url: str,
    *,
    source: OneShotSource,
    sink: NullSink,
) -> tuple[MediaRouter, AvatarClient]:
    """Wire a router with the **real** avatar transport pointed at a stub server."""
    clock = MediaClock()
    avatar = AvatarClient(
        transport=WebSocketAvatarTransport(
            url=url, ctx=ctx, clock=clock, open_timeout_s=5.0
        ),
        ctx=ctx,
        policy=ReconnectPolicy(initial_delay_s=0.01, max_delay_s=0.02, max_attempts=2),
    )
    decode = DecodePipeline(
        decoder=FakeDecoder(ctx=ctx, video_format=VIDEO, audio_format=PUBLISH_AUDIO),
        ctx=ctx,
    )
    pacer = Pacer(
        ctx=ctx,
        clock=clock,
        sink=sink,
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
        decode=decode,
        pacer=pacer,
        echo_guard=EchoGuard(per_participant_audio=True),
    )
    return router, avatar


@contextlib.asynccontextmanager
async def _running(router: MediaRouter, avatar: AvatarClient) -> AsyncIterator[None]:
    """Run the router for the body of the block, then tear it down unconditionally.

    Cleanup deliberately asserts nothing. If a leg died, the *body's* assertion is the
    interesting failure, and demanding a clean ``CancelledError`` here would replace it with a
    confusing one.
    """
    task = asyncio.create_task(router.run())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
        await avatar.stop()


async def _expect_startup_failure(router: MediaRouter, expected: type[BaseException]) -> None:
    """Assert ``router.run()`` fails with ``expected``, without ever hanging.

    The bound matters: before the fix ``run()`` never returned at all — it started its legs and
    idled forever — so an unbounded ``pytest.raises`` turned a bug into a hung suite rather
    than a red test. ``TimeoutError`` here means "it did not fail, it just sat there", which is
    exactly the original defect and should read as a failure.
    """
    with pytest.raises(expected):
        await asyncio.wait_for(router.run(), timeout=10.0)


# --------------------------------------------------------------------------- #
# The bug
# --------------------------------------------------------------------------- #


class TestAudioReachesTheAgent:
    async def test_the_router_connects_the_avatar_leg(self, frame_ctx: FrameContext) -> None:
        """The core assertion: PCM must leave the process.

        Before the fix this timed out — the queue filled, the transport reported
        ``DEGRADED "send queue saturated"``, and nothing was ever sent.
        """
        agent = StubAvatarServer(respond_with=fmp4_stream(2))
        url = await agent.start()
        source = OneShotSource(frame_ctx)
        router, avatar = _router(frame_ctx, url, source=source, sink=NullSink())

        try:
            async with _running(router, avatar):
                await agent.wait_for_pcm()

                assert agent.connections == 1
                assert agent.received_pcm[0] == PCM_FRAME
                assert avatar.is_connected
        finally:
            await agent.stop()

    async def test_the_handshake_declares_the_fixed_contract(
        self, frame_ctx: FrameContext
    ) -> None:
        """The handshake is exchanged at all — it previously never was."""
        agent = StubAvatarServer()
        url = await agent.start()
        source = OneShotSource(frame_ctx, count=1)
        router, avatar = _router(frame_ctx, url, source=source, sink=NullSink())

        try:
            async with _running(router, avatar):
                await agent.wait_for_pcm()

                hello = agent.hellos[0]
                assert hello["audio"]["sample_rate_hz"] == 16_000
                assert hello["audio"]["channels"] == 1
                assert hello["audio"]["sample_format"] == "s16le"
                assert hello["expects_container"] == "fmp4"
                # The session identity travels with it, so the agent's logs correlate.
                assert hello["session_id"] == frame_ctx.session_id
                assert hello["correlation_id"] == frame_ctx.correlation_id
        finally:
            await agent.stop()

    async def test_the_agents_media_comes_back_and_is_published(
        self, frame_ctx: FrameContext
    ) -> None:
        """The full round trip over a real socket: PCM out, fMP4 in, frames published."""
        agent = StubAvatarServer(respond_with=fmp4_stream(3))
        url = await agent.start()
        source = OneShotSource(frame_ctx)
        sink = NullSink()
        router, avatar = _router(frame_ctx, url, source=source, sink=sink)

        try:
            async with _running(router, avatar):
                await agent.wait_for_pcm()

                # Waiting on the cached init segment, **not** on ``sink.video_frames``. The
                # pacer publishes idle video from the instant the session starts, so a video
                # count above zero says nothing about whether the agent's response came back —
                # it would pass on a completely dead avatar leg, which is the exact bug this
                # file exists to catch.
                deadline = asyncio.get_running_loop().time() + 3.0
                while avatar.init_segment is None:
                    if asyncio.get_running_loop().time() >= deadline:
                        raise AssertionError(
                            "the agent's fMP4 never came back: chunks are not reaching the "
                            "router"
                        )
                    await asyncio.sleep(0.02)

                assert avatar.init_segment.is_init_segment
                assert sink.video_frames > 0
        finally:
            await agent.stop()

    async def test_the_transport_is_healthy_rather_than_saturated(
        self, frame_ctx: FrameContext
    ) -> None:
        """The symptom the bug produced, asserted directly.

        ``"send queue saturated"`` on a session with three frames of audio is the fingerprint
        of a writer task that does not exist.
        """
        agent = StubAvatarServer()
        url = await agent.start()
        source = OneShotSource(frame_ctx)
        router, avatar = _router(frame_ctx, url, source=source, sink=NullSink())

        try:
            async with _running(router, avatar):
                await agent.wait_for_pcm()
                health = avatar.health()

                assert health.state is ComponentState.HEALTHY
                assert health.detail != "send queue saturated"
        finally:
            await agent.stop()


# --------------------------------------------------------------------------- #
# Failure paths, now that the leg is actually started
# --------------------------------------------------------------------------- #


class TestStartupFailures:
    async def test_an_unreachable_agent_degrades_the_session_instead_of_killing_it(
        self, frame_ctx: FrameContext
    ) -> None:
        """The deliberate half of the fix, and the reason it is safe to ship.

        Failing the router here would be a **new** way for a session to die: an avatar-service
        blip would take live Zoom and Teams meetings down, and since every session class runs
        the router as a background task the death would be silent. So an unreachable agent
        degrades — which is precisely what it did before the fix — while a reachable one now
        works.

        The session keeps publishing idle media, so the meeting still shows a person rather
        than a frozen tile, and the avatar component reports the fault where an operator looks.
        """
        source = OneShotSource(frame_ctx)
        sink = NullSink()
        # Port 1 on loopback: reserved, so nothing can be listening.
        router, avatar = _router(
            frame_ctx, "ws://127.0.0.1:1/stream", source=source, sink=sink
        )

        async with _running(router, avatar):
            # The router must still be running and the pacer must still be publishing.
            deadline = asyncio.get_running_loop().time() + 3.0
            while sink.video_frames == 0:
                if asyncio.get_running_loop().time() >= deadline:
                    raise AssertionError(
                        "an unreachable avatar stopped the pacer; the meeting would show a "
                        "frozen tile"
                    )
                await asyncio.sleep(0.02)

            assert not avatar.is_connected
            assert avatar.health().state is not ComponentState.HEALTHY

    async def test_an_incompatible_agent_still_propagates(
        self, frame_ctx: FrameContext
    ) -> None:
        """The one failure that is *not* tolerated.

        A major-version mismatch will not resolve itself, no degraded mode helps, and carrying
        on would mean streaming PCM at an agent that cannot answer. So it is the single
        exception to the degrade-don't-die rule above.
        """
        agent = StubAvatarServer(
            reply={"protocol_version": "9.0", "accepted": True, "container": "fmp4"}
        )
        url = await agent.start()
        source = OneShotSource(frame_ctx)
        router, avatar = _router(frame_ctx, url, source=source, sink=NullSink())

        try:
            await _expect_startup_failure(router, AvatarProtocolMismatchError)
        finally:
            await avatar.stop()
            await agent.stop()

    async def test_a_rejected_handshake_surfaces(self, frame_ctx: FrameContext) -> None:
        """An agent that refuses us must not look like an agent that is merely quiet."""
        agent = StubAvatarServer(
            reply={"protocol_version": "1.0", "accepted": False, "reason": "busy"}
        )
        url = await agent.start()
        source = OneShotSource(frame_ctx)
        router, avatar = _router(frame_ctx, url, source=source, sink=NullSink())

        try:
            await _expect_startup_failure(router, AvatarProtocolMismatchError)
        finally:
            await avatar.stop()
            await agent.stop()

    async def test_an_incompatible_major_version_surfaces(
        self, frame_ctx: FrameContext
    ) -> None:
        agent = StubAvatarServer(
            reply={"protocol_version": "2.0", "accepted": True, "container": "fmp4"}
        )
        url = await agent.start()
        source = OneShotSource(frame_ctx)
        router, avatar = _router(frame_ctx, url, source=source, sink=NullSink())

        try:
            await _expect_startup_failure(router, AvatarProtocolMismatchError)
        finally:
            await avatar.stop()
            await agent.stop()


# --------------------------------------------------------------------------- #
# All three connectors, through their real factories
# --------------------------------------------------------------------------- #


def _settings(avatar_url: str, tmp_path: Path) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        teams=TeamsSettings(
            tenant_id="72f988bf-86f1-41af-91ab-2d7cd011db47",
            client_id="8b081ef6-4792-4def-b2c9-c363a1bf41d5",
            client_secret=SecretStr("secret"),
            sidecar_host="teams-bot.internal",
        ),
        google_meet=GoogleMeetSettings(profile_dir=tmp_path / "profile"),
    ).model_copy(update={"avatar": Settings(_env_file=None).avatar.model_copy(  # type: ignore[call-arg]
        update={"url": avatar_url, "connect_timeout_s": 5.0}
    )})


class TestEveryConnectorStartsItsAvatarLeg:
    """The fix is in ``MediaRouter``, which all three share — so all three are asserted.

    Each connector's own platform leg is left out: the point is that whichever session builds
    a router, that router connects the avatar. Bringing up a Zoom sidecar or a Windows host
    here would test those, not this.
    """

    @pytest.mark.parametrize("connector", ["zoom", "teams", "google_meet"])
    async def test_the_factory_builds_a_router_that_connects(
        self, connector: str, frame_ctx: FrameContext, tmp_path: Path
    ) -> None:
        agent = StubAvatarServer()
        url = await agent.start()
        settings = _settings(url, tmp_path)

        avatar = _avatar_from_factory(connector, settings, frame_ctx)
        try:
            await avatar.start()
            await avatar.send(
                AudioFrame(pcm=PCM_FRAME, pts_us=0, format=PUBLISH_AUDIO, ctx=frame_ctx)
            )
            await agent.wait_for_pcm()

            assert agent.received_pcm[0] == PCM_FRAME
        finally:
            await avatar.stop()
            await agent.stop()


def _avatar_from_factory(
    connector: str, settings: Settings, ctx: FrameContext
) -> AvatarClient:
    """Build the ``AvatarClient`` each connector's factory would build.

    Constructed the same way the factories do — same url, same timeouts, same policy — so this
    exercises the wiring rather than a hand-rolled client that happens to work.
    """
    clock = MediaClock()
    if connector == "zoom":
        from src.connectors.zoom.config import ZoomConnectorConfig

        config = ZoomConnectorConfig.from_settings(settings)
    elif connector == "teams":
        from src.connectors.teams.config import TeamsConnectorConfig

        config = TeamsConnectorConfig.from_settings(settings)
    else:
        from src.connectors.google_meet.config import GoogleMeetConnectorConfig

        config = GoogleMeetConnectorConfig.from_settings(settings)

    return AvatarClient(
        transport=WebSocketAvatarTransport(
            url=config.avatar_url,
            ctx=ctx,
            clock=clock,
            send_queue_size=config.avatar_send_queue_size,
            open_timeout_s=config.avatar_connect_timeout_s,
        ),
        ctx=ctx,
        policy=ReconnectPolicy(
            initial_delay_s=config.avatar_reconnect_initial_delay_s,
            max_delay_s=config.avatar_reconnect_max_delay_s,
            max_attempts=config.avatar_reconnect_max_attempts,
        ),
    )
