"""The Chromium bridge, end to end over a real socket.

Only the *browser* is faked. The WebSocket server, the token check, the wire codec, the
handshake, the framing and the backpressure path are all the real thing — because those are
where a Python/JavaScript disagreement would be invisible until a live meeting.

The properties under test, in rough order of how expensive getting them wrong would be:

1. **Ordering.** The server must be bound before the browser launches (its port is baked
   into an init script), and the page channel must be captured *after* joining, because
   navigating to Meet replaces the socket the sign-in probe opened.
2. **Not retrying a refusal.** A denied or ejected join is terminal.
3. **Devices are turned on.** Meet does not publish tracks it was handed until told to, and
   nothing else in the system can observe that it did not.
4. **Format enforcement at the boundary.** A page sending 48 kHz must fail loudly rather
   than feeding the avatar audio it cannot use.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.config.settings import GoogleMeetSettings, Settings
from src.connectors.google_meet.bridge.chromium_bridge import ChromiumBridge
from src.connectors.google_meet.browser.profile import ProfileManager
from src.connectors.google_meet.config import GoogleMeetConnectorConfig
from src.connectors.google_meet.exceptions import (
    MeetConfigurationError,
    MeetUrlError,
)
from src.connectors.google_meet.meeting.hand_raise import MeetHandRaiseSource
from src.connectors.google_meet.websocket.protocol import MeetState, encode_audio, encode_video
from src.domain.context import FrameContext
from src.domain.health import ComponentState
from src.domain.media import (
    AudioFormat,
    AudioFrame,
    PixelFormat,
    SampleFormat,
    VideoFormat,
    VideoFrame,
)
from src.domain.meeting import MeetingContext, MeetingPlatform
from src.infrastructure.reconnect import ReconnectPolicy
from src.services.media.clock import MediaClock
from tests.fakes.meet_page import (
    CAM_ON_SELECTOR,
    MIC_ON_SELECTOR,
    FakeBrowserDriver,
    joined_driver,
)

CODE = "abc-defg-hij"


@pytest.fixture
def meet_config(tmp_path: Path) -> GoogleMeetConnectorConfig:
    template = tmp_path / "profile"
    (template / "Default").mkdir(parents=True)
    (template / "Default" / "Cookies").write_bytes(b"cookie")
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        google_meet=GoogleMeetSettings(
            profile_dir=template,
            video_width=320,
            video_height=180,
            video_fps=25,
            publish_sample_rate_hz=48_000,
            bridge_ready_timeout_s=5.0,
            join_timeout_s=2.0,
            lobby_timeout_s=2.0,
        ),
    )
    return GoogleMeetConnectorConfig.from_settings(settings)


@pytest.fixture
def meeting() -> MeetingContext:
    return MeetingContext(
        meeting_number=CODE,
        display_name="AI Avatar",
        platform=MeetingPlatform.GOOGLE_MEET,
    )


def _bridge(
    config: GoogleMeetConnectorConfig,
    driver: FakeBrowserDriver,
    ctx: FrameContext,
    **kwargs: object,
) -> ChromiumBridge:
    return ChromiumBridge(
        config=config,
        ctx=ctx,
        clock=MediaClock(),
        driver_factory=lambda: driver,
        profiles=ProfileManager(template=config.require_configured()),
        **kwargs,  # type: ignore[arg-type]
    )


async def _wait_for(predicate, *, timeout_s: float = 2.0) -> None:
    """Poll until ``predicate()`` is true, or fail the test.

    The bridge's read loop is a separate task, so anything it causes — a queued frame, a
    state change — lands asynchronously. Polling with a deadline is what keeps these tests
    deterministic without sleeping for a fixed period and hoping.
    """
    deadline = asyncio.get_running_loop().time() + timeout_s
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition was never met")
        await asyncio.sleep(0.01)


# --------------------------------------------------------------------------- #
# Startup
# --------------------------------------------------------------------------- #


class TestStart:
    async def test_a_full_join_reaches_a_healthy_bridge(
        self, meet_config, meeting, frame_ctx
    ) -> None:
        driver = joined_driver(auto_page=True)
        bridge = _bridge(meet_config, driver, frame_ctx)
        try:
            await bridge.start(meeting)

            assert bridge.is_joined
            assert bridge.health().state is ComponentState.HEALTHY
            assert driver.page is not None
            assert driver.page.config is not None
        finally:
            await bridge.stop()

    async def test_the_page_is_told_the_configured_media_formats(
        self, meet_config, meeting, frame_ctx
    ) -> None:
        """Pushed rather than hardcoded in the asset, so geometry is a settings change."""
        driver = joined_driver(auto_page=True)
        bridge = _bridge(meet_config, driver, frame_ctx)
        try:
            await bridge.start(meeting)
            config = driver.page.config  # type: ignore[union-attr]

            assert config["videoWidth"] == 320
            assert config["videoHeight"] == 180
            assert config["publishSampleRateHz"] == 48_000
            # 16 kHz is the avatar's fixed input rate, and building the capture context at
            # that rate is what removes the need for a resampler anywhere.
            assert config["captureSampleRateHz"] == 16_000
            assert config["selectors"]["inCall"]
        finally:
            await bridge.stop()

    async def test_the_worklets_are_injected_as_sources_not_urls(
        self, meet_config, meeting, frame_ctx
    ) -> None:
        """No HTTP server has to exist to serve them; bridge.js wraps each in a Blob."""
        driver = joined_driver(auto_page=True)
        bridge = _bridge(meet_config, driver, frame_ctx)
        try:
            await bridge.start(meeting)
            worklets = driver.injected_worklets

            assert "mc-capture" in worklets
            assert "mc-playout" in worklets
        finally:
            await bridge.stop()

    async def test_the_bridge_endpoint_is_loopback_with_an_ephemeral_port(
        self, meet_config, meeting, frame_ctx
    ) -> None:
        driver = joined_driver(auto_page=True)
        bridge = _bridge(meet_config, driver, frame_ctx)
        try:
            await bridge.start(meeting)
            endpoint = driver.bridge_endpoint

            assert endpoint.startswith("ws://127.0.0.1:")
            assert "/bridge/" in endpoint
            # Not the configured 0 — the OS assigned a real port and it was read back.
            port = int(endpoint.split("//")[1].split("/")[0].split(":")[1])
            assert port > 0
        finally:
            await bridge.stop()

    async def test_the_microphone_and_camera_are_turned_on(
        self, meet_config, meeting, frame_ctx
    ) -> None:
        """The one failure nothing else can observe: Meet holding tracks it never published."""
        driver = joined_driver(auto_page=True)
        bridge = _bridge(meet_config, driver, frame_ctx)
        try:
            await bridge.start(meeting)

            assert MIC_ON_SELECTOR in driver.visible
            assert CAM_ON_SELECTOR in driver.visible
        finally:
            await bridge.stop()

    async def test_a_bad_meeting_code_fails_before_a_browser_is_launched(
        self, meet_config, frame_ctx
    ) -> None:
        driver = joined_driver(auto_page=True)
        bridge = _bridge(meet_config, driver, frame_ctx)
        bad = MeetingContext(
            meeting_number="1234567890",
            display_name="AI Avatar",
            platform=MeetingPlatform.GOOGLE_MEET,
        )
        with pytest.raises(MeetUrlError):
            await bridge.start(bad)
        assert driver.started == 0

    async def test_an_unconfigured_profile_fails_before_a_browser_is_launched(
        self, meet_config, meeting, frame_ctx
    ) -> None:
        from dataclasses import replace

        driver = joined_driver(auto_page=True)
        bridge = ChromiumBridge(
            config=replace(meet_config, profile_dir=None),
            ctx=frame_ctx,
            clock=MediaClock(),
            driver_factory=lambda: driver,
        )
        with pytest.raises(MeetConfigurationError, match="MC_GOOGLE_MEET__PROFILE_DIR"):
            await bridge.start(meeting)
        assert driver.started == 0

    async def test_start_is_idempotent(self, meet_config, meeting, frame_ctx) -> None:
        driver = joined_driver(auto_page=True)
        bridge = _bridge(meet_config, driver, frame_ctx)
        try:
            await bridge.start(meeting)
            await bridge.start(meeting)
            assert driver.started == 1
        finally:
            await bridge.stop()


# --------------------------------------------------------------------------- #
# Ingest
# --------------------------------------------------------------------------- #


class TestIngest:
    async def test_conference_audio_reaches_the_queue(
        self, meet_config, meeting, frame_ctx
    ) -> None:
        driver = joined_driver(auto_page=True)
        bridge = _bridge(meet_config, driver, frame_ctx)
        try:
            await bridge.start(meeting)
            await driver.page.send_audio(b"\x01\x02" * 320)  # type: ignore[union-attr]

            await _wait_for(lambda: bridge.audio_queue().qsize() > 0)
            frame = await bridge.audio_queue().get()

            assert frame.format.sample_rate_hz == 16_000
            assert frame.format.channels == 1
            assert len(frame.pcm) == 640
        finally:
            await bridge.stop()

    async def test_the_frame_carries_the_sessions_own_identity_and_clock(
        self, meet_config, meeting, frame_ctx
    ) -> None:
        """PTS comes from our media clock: the browser's audio clock is a different timeline
        and mixing the two would corrupt A/V sync."""
        driver = joined_driver(auto_page=True)
        bridge = _bridge(meet_config, driver, frame_ctx)
        try:
            await bridge.start(meeting)
            await driver.page.send_audio(b"\x00\x00" * 320)  # type: ignore[union-attr]

            await _wait_for(lambda: bridge.audio_queue().qsize() > 0)
            frame = await bridge.audio_queue().get()

            assert frame.ctx is frame_ctx
            assert frame.pts_us >= 0
        finally:
            await bridge.stop()

    async def test_mixed_audio_carries_no_participant(
        self, meet_config, meeting, frame_ctx
    ) -> None:
        """The capture graph sums every remote track, so there is nobody to attribute it to.

        ``None`` is what makes ``EchoGuard`` fall back to its speaking gate — the correct and
        only needed defence here, since the WebRTC tap is inbound-only.
        """
        driver = joined_driver(auto_page=True)
        bridge = _bridge(meet_config, driver, frame_ctx)
        try:
            await bridge.start(meeting)
            await driver.page.send_audio(b"\x01\x02" * 320)  # type: ignore[union-attr]

            await _wait_for(lambda: bridge.audio_queue().qsize() > 0)
            assert (await bridge.audio_queue().get()).participant is None
        finally:
            await bridge.stop()

    async def test_the_wrong_sample_rate_is_dropped_and_counted_not_forwarded(
        self, meet_config, meeting, frame_ctx
    ) -> None:
        """A stale bridge.js building a 48 kHz context must not reach the avatar.

        One malformed frame does not tear a meeting down, so it is counted — a persistent
        fault shows up as silence plus this counter climbing.
        """
        driver = joined_driver(auto_page=True)
        bridge = _bridge(meet_config, driver, frame_ctx)
        try:
            await bridge.start(meeting)
            await driver.page.send_audio_at(  # type: ignore[union-attr]
                b"\x01\x02" * 960, sample_rate_hz=48_000
            )

            await _wait_for(lambda: bridge.stats["malformed_audio"] > 0)
            assert bridge.audio_queue().qsize() == 0
            assert bridge.health().state.is_serving
        finally:
            await bridge.stop()


# --------------------------------------------------------------------------- #
# Egress
# --------------------------------------------------------------------------- #


class TestEgress:
    async def test_video_reaches_the_page_with_its_geometry(
        self, meet_config, meeting, frame_ctx
    ) -> None:
        driver = joined_driver(auto_page=True)
        bridge = _bridge(meet_config, driver, frame_ctx)
        try:
            await bridge.start(meeting)
            fmt = VideoFormat(width=320, height=180, fps=25, pixel_format=PixelFormat.I420)
            frame = VideoFrame(
                planes=b"\x40" * fmt.frame_size_bytes, pts_us=0, format=fmt, ctx=frame_ctx
            )
            assert await bridge.send_video(encode_video(frame)) is True

            await _wait_for(lambda: len(driver.page.received_video) > 0)  # type: ignore[union-attr]
            width, height, size = driver.page.received_video[0]  # type: ignore[union-attr]
            assert (width, height) == (320, 180)
            assert size == fmt.frame_size_bytes
        finally:
            await bridge.stop()

    async def test_audio_reaches_the_page(self, meet_config, meeting, frame_ctx) -> None:
        driver = joined_driver(auto_page=True)
        bridge = _bridge(meet_config, driver, frame_ctx)
        try:
            await bridge.start(meeting)
            fmt = AudioFormat(sample_rate_hz=48_000, channels=1, sample_format=SampleFormat.S16LE)
            frame = AudioFrame(pcm=b"\x11\x22" * 960, pts_us=0, format=fmt, ctx=frame_ctx)
            assert await bridge.send_audio(encode_audio(frame)) is True

            await _wait_for(lambda: len(driver.page.received_audio) > 0)  # type: ignore[union-attr]
            assert driver.page.received_audio[0] == frame.pcm  # type: ignore[union-attr]
        finally:
            await bridge.stop()

    async def test_sending_with_no_page_is_a_false_not_an_exception(
        self, meet_config, frame_ctx
    ) -> None:
        """An exception here would tear down the Pacer's task group mid-rejoin."""
        driver = joined_driver()
        bridge = _bridge(meet_config, driver, frame_ctx)

        assert await bridge.send_audio(b"ignored") is False
        assert await bridge.send_video(b"ignored") is False


# --------------------------------------------------------------------------- #
# Meeting state
# --------------------------------------------------------------------------- #


class TestMeetingState:
    async def test_being_ejected_fails_the_bridge_without_rejoining(
        self, meet_config, meeting, frame_ctx
    ) -> None:
        """A host decided. Retrying is what gets an automated account restricted."""
        driver = joined_driver(auto_page=True)
        bridge = _bridge(meet_config, driver, frame_ctx)
        try:
            await bridge.start(meeting)
            launches = driver.started

            await driver.page.send_state(MeetState.EJECTED)  # type: ignore[union-attr]
            await _wait_for(lambda: bridge.health().state is ComponentState.UNHEALTHY)

            assert bridge.meet_state is MeetState.EJECTED
            await asyncio.sleep(0.1)
            assert driver.started == launches, "the bridge must not relaunch after a refusal"
        finally:
            await bridge.stop()

    async def test_the_meeting_ending_is_terminal(
        self, meet_config, meeting, frame_ctx
    ) -> None:
        driver = joined_driver(auto_page=True)
        bridge = _bridge(meet_config, driver, frame_ctx)
        try:
            await bridge.start(meeting)
            await driver.page.send_state(MeetState.ENDED)  # type: ignore[union-attr]
            await _wait_for(lambda: bridge.health().state is ComponentState.UNHEALTHY)
            assert bridge.meet_state is MeetState.ENDED
        finally:
            await bridge.stop()

    async def test_leaving_degrades_rather_than_fails(
        self, meet_config, meeting, frame_ctx
    ) -> None:
        """Recoverable, so the supervisor's grace window decides — not this component."""
        driver = joined_driver(auto_page=True)
        bridge = _bridge(meet_config, driver, frame_ctx)
        try:
            await bridge.start(meeting)
            await driver.page.send_state(MeetState.LEFT)  # type: ignore[union-attr]
            await _wait_for(lambda: bridge.meet_state is MeetState.LEFT)

            assert bridge.health().state.is_serving
        finally:
            await bridge.stop()

    async def test_state_listeners_are_notified(
        self, meet_config, meeting, frame_ctx
    ) -> None:
        driver = joined_driver(auto_page=True)
        bridge = _bridge(meet_config, driver, frame_ctx)
        seen: list[MeetState] = []
        bridge.add_state_listener(seen.append)
        try:
            await bridge.start(meeting)
            await driver.page.send_state(MeetState.LEFT)  # type: ignore[union-attr]
            await _wait_for(lambda: MeetState.LEFT in seen)
        finally:
            await bridge.stop()

    async def test_a_fatal_page_error_fails_the_bridge(
        self, meet_config, meeting, frame_ctx
    ) -> None:
        driver = joined_driver(auto_page=True)
        bridge = _bridge(meet_config, driver, frame_ctx)
        try:
            await bridge.start(meeting)
            await driver.page.send_error(  # type: ignore[union-attr]
                "GET_USER_MEDIA", "no device", fatal=True
            )
            await _wait_for(lambda: bridge.health().state is ComponentState.UNHEALTHY)
            assert "GET_USER_MEDIA" in (bridge.health().detail or "")
        finally:
            await bridge.stop()

    async def test_a_non_fatal_page_error_only_degrades(
        self, meet_config, meeting, frame_ctx
    ) -> None:
        driver = joined_driver(auto_page=True)
        bridge = _bridge(meet_config, driver, frame_ctx)
        try:
            await bridge.start(meeting)
            await driver.page.send_error("VIDEO_FRAME", "bad layout")  # type: ignore[union-attr]
            await _wait_for(lambda: bridge.health().state is ComponentState.DEGRADED)
            assert bridge.health().state.is_serving
        finally:
            await bridge.stop()


# --------------------------------------------------------------------------- #
# Roster
# --------------------------------------------------------------------------- #


class TestRoster:
    async def test_the_roster_is_observed(self, meet_config, meeting, frame_ctx) -> None:
        driver = joined_driver(auto_page=True)
        bridge = _bridge(meet_config, driver, frame_ctx)
        try:
            await bridge.start(meeting)
            await driver.page.send_participants(  # type: ignore[union-attr]
                ["Alice Smith", "AI Avatar"], self_name="AI Avatar"
            )
            await _wait_for(lambda: bridge.roster.count == 2)

            assert len(bridge.roster.others) == 1
            assert bridge.roster.others[0].display_name == "Alice Smith"
        finally:
            await bridge.stop()

    async def test_roster_listeners_are_notified(
        self, meet_config, meeting, frame_ctx
    ) -> None:
        driver = joined_driver(auto_page=True)
        bridge = _bridge(meet_config, driver, frame_ctx)
        seen: list[int] = []
        bridge.add_roster_listener(lambda r: seen.append(r.count))
        try:
            await bridge.start(meeting)
            await driver.page.send_participants(["Alice"])  # type: ignore[union-attr]
            await _wait_for(lambda: seen == [1])
        finally:
            await bridge.stop()


# --------------------------------------------------------------------------- #
# Raised hands
# --------------------------------------------------------------------------- #


class TestHandRaise:
    """The page → source plumbing, which nothing else covers.

    ``MeetHandRaiseSource`` is unit-tested against payloads and the router leg is tested
    against events. Between the two sits the read loop's dispatch, and a message type that
    never reaches an attached source looks exactly like a meeting where nobody raised a hand.
    """

    async def test_a_reported_hand_reaches_the_attached_source(
        self, meet_config, meeting, frame_ctx
    ) -> None:
        driver = joined_driver(auto_page=True)
        bridge = _bridge(meet_config, driver, frame_ctx)
        hands = MeetHandRaiseSource(clock=MediaClock())
        bridge.attach_hand_raise(hands)
        try:
            await bridge.start(meeting)
            await driver.page.send_hand_raise(name="Priya")  # type: ignore[union-attr]
            await _wait_for(lambda: hands.received == 1)
        finally:
            await bridge.stop()

    async def test_a_hand_with_no_source_attached_is_harmless(
        self, meet_config, meeting, frame_ctx
    ) -> None:
        """A session with the feature disabled attaches nothing, and the read loop is the
        media channel — dropping the message must cost it nothing."""
        driver = joined_driver(auto_page=True)
        bridge = _bridge(meet_config, driver, frame_ctx)
        try:
            await bridge.start(meeting)
            await driver.page.send_hand_raise()  # type: ignore[union-attr]
            await driver.page.send_participants(["Alice"])  # type: ignore[union-attr]
            # The roster still lands, so the read loop survived the unhandled message.
            await _wait_for(lambda: bridge.roster.count == 1)
            assert bridge.health().state is not ComponentState.UNHEALTHY
        finally:
            await bridge.stop()


# --------------------------------------------------------------------------- #
# Teardown and recovery
# --------------------------------------------------------------------------- #


class TestTeardown:
    async def test_stop_leaves_the_call_before_killing_the_browser(
        self, meet_config, meeting, frame_ctx
    ) -> None:
        """Closing without leaving strands the avatar as a frozen tile for minutes."""
        driver = joined_driver(auto_page=True)
        bridge = _bridge(meet_config, driver, frame_ctx)
        await bridge.start(meeting)
        await bridge.stop()

        assert any("Leave call" in selector for selector in driver.clicked)
        assert driver.stopped == 1

    async def test_stop_releases_the_working_profile(
        self, meet_config, meeting, frame_ctx
    ) -> None:
        driver = joined_driver(auto_page=True)
        profiles = ProfileManager(template=meet_config.require_configured())
        bridge = ChromiumBridge(
            config=meet_config,
            ctx=frame_ctx,
            clock=MediaClock(),
            driver_factory=lambda: driver,
            profiles=profiles,
        )
        await bridge.start(meeting)
        working = driver.plan.user_data_dir  # type: ignore[union-attr]
        assert working.exists()

        await bridge.stop()
        assert not working.exists()
        assert meet_config.require_configured().exists(), "the template must survive"

    async def test_stop_is_idempotent(self, meet_config, meeting, frame_ctx) -> None:
        driver = joined_driver(auto_page=True)
        bridge = _bridge(meet_config, driver, frame_ctx)
        await bridge.start(meeting)
        await bridge.stop()
        await bridge.stop()

    async def test_stopping_a_bridge_that_never_started_is_safe(
        self, meet_config, frame_ctx
    ) -> None:
        await _bridge(meet_config, joined_driver(), frame_ctx).stop()

    async def test_a_dropped_page_triggers_a_relaunch(
        self, meet_config, meeting, frame_ctx
    ) -> None:
        """Recovery is a full relaunch: a crashed renderer takes the peer connection,
        both AudioContexts, the canvas and the synthetic tracks with it."""
        drivers: list[FakeBrowserDriver] = []

        def factory() -> FakeBrowserDriver:
            driver = joined_driver(auto_page=True)
            drivers.append(driver)
            return driver

        bridge = ChromiumBridge(
            config=meet_config,
            ctx=frame_ctx,
            clock=MediaClock(),
            driver_factory=factory,
            profiles=ProfileManager(template=meet_config.require_configured()),
            policy=ReconnectPolicy(initial_delay_s=0.01, max_delay_s=0.02, max_attempts=3),
        )
        try:
            await bridge.start(meeting)
            assert len(drivers) == 1

            # The page goes away without the browser being told to stop — a renderer crash.
            await drivers[0].page.close()  # type: ignore[union-attr]

            await _wait_for(lambda: len(drivers) > 1, timeout_s=5.0)
            await _wait_for(lambda: bridge.rejoins >= 1, timeout_s=5.0)
            assert bridge.health().state.is_serving
        finally:
            await bridge.stop()

    async def test_the_rejoin_budget_is_finite(
        self, meet_config, meeting, frame_ctx
    ) -> None:
        """A leg that can never heal must be declared failed, not retried forever."""
        first = joined_driver(auto_page=True)
        drivers: list[FakeBrowserDriver] = [first]

        def factory() -> FakeBrowserDriver:
            if drivers:
                return drivers.pop(0)
            # Every later launch fails, exhausting the budget.
            return FakeBrowserDriver(crash_after_goto=0)

        bridge = ChromiumBridge(
            config=meet_config,
            ctx=frame_ctx,
            clock=MediaClock(),
            driver_factory=factory,
            profiles=ProfileManager(template=meet_config.require_configured()),
            policy=ReconnectPolicy(initial_delay_s=0.01, max_delay_s=0.02, max_attempts=2),
        )
        try:
            await bridge.start(meeting)
            await first.page.close()  # type: ignore[union-attr]

            # Waiting on the *detail*, not on UNHEALTHY. The leg is correctly marked
            # unhealthy for the whole time a rejoin is in flight — it genuinely is not
            # serving — so polling on the state alone would pass on the first attempt and
            # never observe whether the budget is bounded at all.
            await _wait_for(
                lambda: "budget exhausted" in (bridge.health().detail or ""), timeout_s=5.0
            )
            assert bridge.health().state is ComponentState.UNHEALTHY
            assert bridge.rejoins == 0, "no attempt succeeded, so none should be counted"
        finally:
            await bridge.stop()


# --------------------------------------------------------------------------- #
# Security
# --------------------------------------------------------------------------- #


class TestBridgeSecurity:
    async def test_the_wrong_token_is_refused(self, meet_config, meeting, frame_ctx) -> None:
        """Loopback is shared with every other process on the host; binding is not auth."""
        from websockets.asyncio.client import connect
        from websockets.exceptions import InvalidStatus

        driver = joined_driver(auto_page=True)
        bridge = _bridge(meet_config, driver, frame_ctx)
        try:
            await bridge.start(meeting)
            endpoint = driver.bridge_endpoint
            forged = endpoint.rsplit("/", 1)[0] + "/not-the-token"

            with pytest.raises(InvalidStatus):
                await connect(forged)
        finally:
            await bridge.stop()

    async def test_the_token_is_not_in_the_loggable_endpoint(
        self, meet_config, meeting, frame_ctx
    ) -> None:
        from src.connectors.google_meet.websocket.server import PageBridgeServer

        server = PageBridgeServer()
        await server.start()
        try:
            assert server.token in server.endpoint
            assert server.token not in server.endpoint_for_log
            assert "<token>" in server.endpoint_for_log
        finally:
            await server.stop()
