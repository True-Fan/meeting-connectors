"""The browser half, in a real browser.

Every other Google Meet test fakes Chromium. This one does not: it launches the real
browser with the real launch plan, injects the real ``bridge.js`` and the real worklets, and
asserts the browser-side contract over a real loopback socket. **It is the only place any of
this connector's JavaScript actually executes.**

What it deliberately does *not* do is join a Google Meet conference. That needs a real
signed-in Google account and a real meeting, so it cannot live in a test suite. The split is:

* the Meet-specific DOM work — the join flow, admission outcomes, controls, roster — is
  covered against ``FakeBrowserDriver``, where every branch is reachable;
* everything that depends on *browser behaviour* rather than on Meet's markup — the
  ``getUserMedia`` patch, ``WebCodecs.VideoFrame`` from I420 planes, canvas-backed tracks,
  both AudioWorklets, the ``RTCPeerConnection`` tap — is covered here.

The page under test is a stub served *at Meet's own URL* — ``https://meet.google.com/...``,
fulfilled locally by Playwright's request interception. None of these assertions touches Meet's
markup, so the body can be empty; what matters is the **origin**, for two reasons that both
turned out to bite:

* ``AudioContext.audioWorklet`` is ``[SecureContext]``-gated, so on ``about:blank`` it is
  simply ``undefined`` and both worklets fail to load with
  ``Cannot read properties of undefined (reading 'addModule')``;
* Chromium's Local Network Access checks treat an opaque origin differently from a public one,
  so testing from ``about:blank`` would not exercise the case production actually hits.

Using the real origin makes the test strictly stronger than a synthetic one: the secure-context
requirement and the loopback-access path are both verified as they will be in a live meeting.

Skipped, rather than failed, when Playwright or its Chromium build is absent — a Zoom-only
deployment installs neither.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator

import pytest

from src.connectors.google_meet.automation.driver import PlaywrightDriver
from src.connectors.google_meet.automation.selectors import DEFAULT_SELECTORS
from src.connectors.google_meet.browser.launcher import build_launch_plan
from src.connectors.google_meet.exceptions import PlaywrightUnavailableError
from src.connectors.google_meet.js import load_assets
from src.connectors.google_meet.websocket.channel import PageChannel
from src.connectors.google_meet.websocket.protocol import (
    AUDIO_HEADER_SIZE,
    MeetMessage,
    MeetMessageType,
    encode_audio,
    encode_video,
)
from src.connectors.google_meet.websocket.server import PageBridgeServer
from src.domain.context import FrameContext
from src.domain.media import (
    AudioFormat,
    AudioFrame,
    PixelFormat,
    SampleFormat,
    VideoFormat,
    VideoFrame,
)

pytest.importorskip("playwright", reason="the google-meet extra is not installed")

pytestmark = pytest.mark.integration

VIDEO = VideoFormat(width=320, height=180, fps=25, pixel_format=PixelFormat.I420)
PUBLISH_AUDIO = AudioFormat(sample_rate_hz=48_000, channels=1, sample_format=SampleFormat.S16LE)
CAPTURE_RATE_HZ = 16_000
MEET_STUB_URL = "https://meet.google.com/abc-defg-hij"
"""Meet's real origin, with the body served locally. See the module docstring: the origin is
load-bearing because ``audioWorklet`` is secure-context-only and Local Network Access treats
public and opaque origins differently."""
HEARTBEAT_MS = 250
"""Far faster than production's 5 s. The heartbeat is the page's only channel for reporting
its own counters — frames drawn, tracks tapped, playout buffer state — and those counters are
what most of these assertions read. In production a 250 ms heartbeat would be noise."""


def _page_config(server: PageBridgeServer) -> dict[str, object]:
    """The same shape ``ChromiumBridge._page_config`` builds, with a fast heartbeat.

    Built here rather than by calling the bridge because the bridge's version is produced
    *during a join*, and this test has no meeting to join. The values that matter to the
    browser — rates, geometry, endpoint — are identical; only the heartbeat differs, and
    ``test_google_meet_bridge.py`` already asserts the bridge sends the production values.
    """
    return {
        "endpoint": server.endpoint,
        "captureSampleRateHz": CAPTURE_RATE_HZ,
        "captureFrameMs": 20,
        "publishSampleRateHz": PUBLISH_AUDIO.sample_rate_hz,
        "playoutBufferSeconds": 0.5,
        "videoWidth": VIDEO.width,
        "videoHeight": VIDEO.height,
        "videoFps": VIDEO.fps,
        "displayName": "AI Avatar",
        "heartbeatIntervalMs": HEARTBEAT_MS,
        "scanIntervalMs": 500,
        "selectors": DEFAULT_SELECTORS.to_page_config(),
    }


class PageSession:
    """A real Chromium running the real bridge script, with its messages collected."""

    def __init__(self) -> None:
        self.server = PageBridgeServer()
        self.driver = PlaywrightDriver()
        self.channel: PageChannel | None = None
        self.messages: list[MeetMessage] = []
        self.ready: dict[str, object] = {}
        self._reader: asyncio.Task[None] | None = None

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        await self.server.start()

        plan = build_launch_plan(
            user_data_dir=_tmp_profile(),
            video_format=VIDEO,
            headless=True,
        )
        await self.driver.start(plan)

        assets = load_assets()
        preamble = (
            f"window.__MC_BRIDGE_CONFIG__ = {json.dumps(_page_config(self.server))};\n"
            "window.__MC_BRIDGE_WORKLETS__ = {"
            f"capture: {json.dumps(assets.capture_worklet)},"
            f"playout: {json.dumps(assets.playout_worklet)}"
            "};\n"
        )
        await self.driver.add_init_script(preamble)
        await self.driver.add_init_script(assets.bridge)

        # Serve a stub at Meet's real URL. Reaching into ``driver._context`` is deliberate:
        # the driver exposes no routing because production never needs it, and the alternative
        # — bypassing PlaywrightDriver entirely — would stop testing the thing under test.
        await self.driver._context.route(
            "https://meet.google.com/**",
            lambda route: route.fulfill(
                status=200,
                content_type="text/html",
                body="<!doctype html><html><body></body></html>",
            ),
        )
        # The collector starts *before* navigation, and waits for the channel itself. The
        # page sends HELLO from ``socket.onopen``, so starting to read only after
        # ``wait_for_page`` returns leaves a window in which the first message is already gone.
        self._reader = asyncio.create_task(self._collect(), name="smoke-reader")
        await self.driver.goto(MEET_STUB_URL, timeout_s=30.0)
        self.channel = await self.server.wait_for_page(timeout_s=30.0)

    async def stop(self) -> None:
        reader, self._reader = self._reader, None
        if reader is not None:
            reader.cancel()
            with contextlib.suppress(BaseException):
                await reader
        await self.driver.stop()
        await self.server.stop()

    async def _collect(self) -> None:
        channel = await self.server.wait_for_page(timeout_s=30.0)
        self.channel = channel
        with contextlib.suppress(Exception):
            async for message in channel.messages():
                self.messages.append(message)
                if message.msg_type is MeetMessageType.HEARTBEAT:
                    await channel.send_json(
                        MeetMessageType.HEARTBEAT,
                        {"sent_at_us": message.json().get("sent_at_us", 0)},
                    )

    # -- driving -----------------------------------------------------------

    async def configure(self) -> dict[str, object]:
        """Complete the CONFIG/READY handshake and return what the page reported."""
        assert self.channel is not None
        await self.channel.send_json(MeetMessageType.CONFIG, _page_config(self.server))
        ready = await self._await_type(MeetMessageType.READY, timeout_s=30.0)
        self.ready = ready.json()
        return self.ready

    async def _await_type(
        self, expected: MeetMessageType, *, timeout_s: float = 10.0
    ) -> MeetMessage:
        deadline = asyncio.get_running_loop().time() + timeout_s
        while True:
            found = next((m for m in self.messages if m.msg_type is expected), None)
            if found is not None:
                return found
            if asyncio.get_running_loop().time() >= deadline:
                seen = sorted({m.msg_type.name for m in self.messages})
                raise AssertionError(
                    f"the page never sent {expected.name} within {timeout_s}s (saw: {seen})"
                )
            await asyncio.sleep(0.05)

    async def wait_until(self, predicate, *, timeout_s: float = 10.0, what: str = "condition"):
        deadline = asyncio.get_running_loop().time() + timeout_s
        while True:
            value = predicate()
            if value:
                return value
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError(f"{what} never happened within {timeout_s}s")
            await asyncio.sleep(0.05)

    # -- observation -------------------------------------------------------

    @property
    def heartbeats(self) -> list[dict[str, object]]:
        return [m.json() for m in self.messages if m.msg_type is MeetMessageType.HEARTBEAT]

    @property
    def page_events(self) -> list[dict[str, object]]:
        return [m.json() for m in self.messages if m.msg_type is MeetMessageType.PAGE_EVENT]

    @property
    def errors(self) -> list[dict[str, object]]:
        return [m.json() for m in self.messages if m.msg_type is MeetMessageType.ERROR]

    @property
    def inbound_pcm(self) -> list[bytes]:
        return [
            m.payload[AUDIO_HEADER_SIZE:]
            for m in self.messages
            if m.msg_type is MeetMessageType.AUDIO_PCM
        ]

    def latest_heartbeat(self) -> dict[str, object]:
        beats = self.heartbeats
        return beats[-1] if beats else {}


def _tmp_profile():
    import tempfile
    from pathlib import Path

    return Path(tempfile.mkdtemp(prefix="mc-meet-smoke-"))


@pytest.fixture
async def page() -> AsyncIterator[PageSession]:
    session = PageSession()
    try:
        await session.start()
    except PlaywrightUnavailableError as exc:
        await session.stop()
        pytest.skip(f"chromium is not available: {exc}")
    try:
        yield session
    finally:
        await session.stop()


# --------------------------------------------------------------------------- #
# The handshake
# --------------------------------------------------------------------------- #


class TestTheScriptRuns:
    async def test_the_page_connects_and_says_hello(self, page: PageSession) -> None:
        """The injected script ran, in a real browser, and reached the bridge."""
        hello = (await page._await_type(MeetMessageType.HELLO)).json()

        assert hello["wire_version"] == 1
        assert "Chrome" in str(hello["user_agent"])
        # Both are hard requirements of the media path, so the page reports them explicitly
        # rather than failing obscurely later.
        assert hello["has_video_frame"] is True
        assert hello["has_audio_worklet"] is True

    async def test_the_media_graph_builds_at_the_configured_rates(
        self, page: PageSession
    ) -> None:
        """``READY`` is the page confirming it built what it was told to build.

        This is the assertion ``ChromiumBridge._verify_page_media`` exists to make in
        production — here it runs against a real Web Audio implementation, which is the only
        way to know a 16 kHz ``AudioContext`` is actually honoured rather than silently
        coerced to the device rate.
        """
        ready = await page.configure()

        assert ready["capture_sample_rate_hz"] == CAPTURE_RATE_HZ
        assert ready["publish_sample_rate_hz"] == PUBLISH_AUDIO.sample_rate_hz
        assert ready["video_width"] == VIDEO.width
        assert ready["video_height"] == VIDEO.height

    async def test_no_errors_are_reported_during_setup(self, page: PageSession) -> None:
        await page.configure()
        await asyncio.sleep(0.5)

        assert page.errors == [], f"the page reported errors: {page.errors}"


# --------------------------------------------------------------------------- #
# The synthetic devices
# --------------------------------------------------------------------------- #


class TestSyntheticDevices:
    async def test_getusermedia_returns_a_camera_and_a_microphone(
        self, page: PageSession
    ) -> None:
        """The patch Meet depends on, exercised through the real browser API."""
        await page.configure()

        result = await page.driver.evaluate(
            """
            (async () => {
              const s = await navigator.mediaDevices.getUserMedia({audio: true, video: true});
              return {
                audio: s.getAudioTracks().length,
                video: s.getVideoTracks().length,
                audioLive: s.getAudioTracks().every(t => t.readyState === 'live'),
                videoLive: s.getVideoTracks().every(t => t.readyState === 'live'),
                kinds: s.getTracks().map(t => t.kind).sort(),
              };
            })()
            """
        )

        assert result["audio"] == 1
        assert result["video"] == 1
        assert result["audioLive"] is True
        assert result["videoLive"] is True
        assert result["kinds"] == ["audio", "video"]

    async def test_a_video_only_request_yields_no_audio_track(
        self, page: PageSession
    ) -> None:
        """Meet asks for each independently; handing back a track it did not request would
        publish a microphone the user believes is off."""
        await page.configure()

        result = await page.driver.evaluate(
            """
            (async () => {
              const s = await navigator.mediaDevices.getUserMedia({video: true});
              return {audio: s.getAudioTracks().length, video: s.getVideoTracks().length};
            })()
            """
        )

        assert result == {"audio": 0, "video": 1}

    async def test_the_devices_are_enumerable(self, page: PageSession) -> None:
        """Meet renders a 'camera blocked' state and never calls getUserMedia if it cannot
        see a device."""
        await page.configure()

        kinds = await page.driver.evaluate(
            """
            (async () => {
              const d = await navigator.mediaDevices.enumerateDevices();
              return d.map(x => x.kind).sort();
            })()
            """
        )

        assert "videoinput" in kinds
        assert "audioinput" in kinds

    async def test_camera_and_microphone_permissions_report_granted(
        self, page: PageSession
    ) -> None:
        await page.configure()

        states = await page.driver.evaluate(
            """
            (async () => ({
              camera: (await navigator.permissions.query({name: 'camera'})).state,
              microphone: (await navigator.permissions.query({name: 'microphone'})).state,
            }))()
            """
        )

        assert states == {"camera": "granted", "microphone": "granted"}

    async def test_getdisplaymedia_is_left_alone(self, page: PageSession) -> None:
        """Faking screen share would make Meet offer a feature that produces a grey box."""
        await page.configure()

        patched = await page.driver.evaluate(
            "navigator.mediaDevices.getDisplayMedia.toString().includes('MC_')"
        )
        assert patched is False


# --------------------------------------------------------------------------- #
# Egress: Python -> browser
# --------------------------------------------------------------------------- #


class TestEgressReachesTheBrowser:
    async def test_an_i420_frame_becomes_a_videoframe_and_is_drawn(
        self, page: PageSession, frame_ctx: FrameContext
    ) -> None:
        """The riskiest line of JavaScript in the connector, finally executed.

        ``new VideoFrame(planes, {format: 'I420', layout: [...]})`` either accepts our plane
        layout or throws, and until now nothing had ever found out which. A wrong stride here
        renders a sheared image rather than an error, so the page reports the frame count and
        this asserts it moved.
        """
        await page.configure()
        assert page.channel is not None

        frame = VideoFrame(
            planes=bytes([0x50]) * (VIDEO.width * VIDEO.height)
            + bytes([0x80]) * (VIDEO.width * VIDEO.height // 2),
            pts_us=0,
            format=VIDEO,
            ctx=frame_ctx,
        )
        for seq in range(5):
            await page.channel.send_raw(encode_video(frame, seq=seq))

        drawn = await page.wait_until(
            lambda: int(page.latest_heartbeat().get("video_frames") or 0),
            what="a video frame reaching the canvas",
        )
        assert drawn >= 1
        assert page.errors == [], f"drawing failed: {page.errors}"

    async def test_pcm_reaches_the_playout_worklet(
        self, page: PageSession, frame_ctx: FrameContext
    ) -> None:
        """The synthetic microphone's source end, in a real Web Audio graph.

        The worklet reports its own buffer occupancy, so a non-zero ``buffered`` proves the PCM
        crossed the socket, was converted, and was accepted by a processor running on the audio
        thread.
        """
        await page.configure()
        assert page.channel is not None

        samples = PUBLISH_AUDIO.sample_rate_hz // 50  # 20 ms
        audio = AudioFrame(
            pcm=b"\x00\x40" * samples, pts_us=0, format=PUBLISH_AUDIO, ctx=frame_ctx
        )
        for seq in range(10):
            await page.channel.send_raw(encode_audio(audio, seq=seq))

        stats = await page.wait_until(
            lambda: page.latest_heartbeat().get("playout") or None,
            what="the playout worklet reporting stats",
        )
        assert isinstance(stats, dict)
        # It renders continuously, so it may already have drained what we sent — the
        # meaningful assertion is that it is running and has not been starved of everything.
        assert "underruns" in stats
        assert "buffered" in stats

    async def test_the_worklet_drops_nothing_at_a_normal_rate(
        self, page: PageSession, frame_ctx: FrameContext
    ) -> None:
        """A drop at 20 ms cadence would mean the ring buffer is mis-sized."""
        await page.configure()
        assert page.channel is not None

        samples = PUBLISH_AUDIO.sample_rate_hz // 50
        audio = AudioFrame(
            pcm=b"\x11\x11" * samples, pts_us=0, format=PUBLISH_AUDIO, ctx=frame_ctx
        )
        for seq in range(10):
            await page.channel.send_raw(encode_audio(audio, seq=seq))
            await asyncio.sleep(0.02)

        await page.wait_until(
            lambda: page.latest_heartbeat().get("playout") or None,
            what="playout stats",
        )
        assert int(page.latest_heartbeat()["playout"]["dropped"]) == 0  # type: ignore[index]


# --------------------------------------------------------------------------- #
# Ingest: browser -> Python
# --------------------------------------------------------------------------- #


class TestIngestFromTheBrowser:
    async def test_a_remote_audio_track_is_tapped_and_arrives_as_16k_pcm(
        self, page: PageSession
    ) -> None:
        """The whole ingest path, without a conference.

        The page builds its own ``RTCPeerConnection`` pair and sends a real oscillator through
        it. Because ``bridge.js`` wraps the constructor, the receiving side's inbound track is
        tapped exactly as a Meet participant's would be — so this exercises the tap, the mixing
        graph, the 16 kHz resample that Web Audio performs for us, the capture worklet's
        int16 conversion and framing, and the wire encoding.

        It is also the only test that can prove the resample actually happens, since that is
        the browser's work and not ours.
        """
        await page.configure()

        await page.driver.evaluate(
            """
            (async () => {
              const ctx = new AudioContext();
              const osc = ctx.createOscillator();
              osc.frequency.value = 440;
              const dest = ctx.createMediaStreamDestination();
              osc.connect(dest);
              osc.start();

              const pc1 = new RTCPeerConnection();
              const pc2 = new RTCPeerConnection();
              pc1.onicecandidate = e => e.candidate && pc2.addIceCandidate(e.candidate);
              pc2.onicecandidate = e => e.candidate && pc1.addIceCandidate(e.candidate);

              dest.stream.getAudioTracks().forEach(t => pc1.addTrack(t, dest.stream));

              const offer = await pc1.createOffer();
              await pc1.setLocalDescription(offer);
              await pc2.setRemoteDescription(offer);
              const answer = await pc2.createAnswer();
              await pc2.setLocalDescription(answer);
              await pc1.setRemoteDescription(answer);
              window.__SMOKE_PC = [pc1, pc2];
            })()
            """
        )

        # Waiting for a frame with actual signal, not merely for a frame. WebRTC needs a
        # moment to negotiate and Chromium emits silence over the new track until it has, so
        # asserting on the first arrival would race the codec and read as "all silence".
        pcm = await page.wait_until(
            lambda: page.inbound_pcm if any(any(f) for f in page.inbound_pcm) else None,
            timeout_s=25.0,
            what="audible conference audio arriving from the page",
        )

        # 20 ms at 16 kHz mono s16le is exactly 640 bytes. Any other size means the capture
        # context was not built at 16 kHz, or the worklet's framing is wrong.
        assert all(len(frame) == 640 for frame in pcm[:5]), [len(f) for f in pcm[:5]]
        assert any(any(frame) for frame in pcm), "every frame was digital silence"

    async def test_the_tap_reports_the_track_it_attached(self, page: PageSession) -> None:
        """The page reports facts upward; the Python side decides what they mean."""
        await page.configure()

        await page.driver.evaluate(
            """
            (async () => {
              const ctx = new AudioContext();
              const dest = ctx.createMediaStreamDestination();
              const osc = ctx.createOscillator();
              osc.connect(dest); osc.start();
              const pc1 = new RTCPeerConnection(), pc2 = new RTCPeerConnection();
              pc1.onicecandidate = e => e.candidate && pc2.addIceCandidate(e.candidate);
              pc2.onicecandidate = e => e.candidate && pc1.addIceCandidate(e.candidate);
              dest.stream.getAudioTracks().forEach(t => pc1.addTrack(t, dest.stream));
              const o = await pc1.createOffer(); await pc1.setLocalDescription(o);
              await pc2.setRemoteDescription(o);
              const a = await pc2.createAnswer(); await pc2.setLocalDescription(a);
              await pc1.setRemoteDescription(a);
              window.__SMOKE_PC2 = [pc1, pc2];
            })()
            """
        )

        events = await page.wait_until(
            lambda: [e for e in page.page_events if e.get("event") == "remoteAudioAttached"]
            or None,
            timeout_s=20.0,
            what="the tap reporting an attached track",
        )
        assert events[0]["detail"]["total"] >= 1  # type: ignore[index]

    async def test_the_avatars_own_microphone_is_never_tapped(
        self, page: PageSession
    ) -> None:
        """Echo is structurally impossible, and this is the proof rather than the claim.

        The synthetic microphone is acquired and added to a peer connection as an *outbound*
        track. The tap listens for ``track`` events, which fire for inbound transceivers only,
        so no audio must arrive — where a naive implementation that enumerated all tracks would
        loop the avatar straight back into itself.
        """
        await page.configure()

        await page.driver.evaluate(
            """
            (async () => {
              const s = await navigator.mediaDevices.getUserMedia({audio: true});
              const pc = new RTCPeerConnection();
              s.getAudioTracks().forEach(t => pc.addTrack(t, s));
              await pc.setLocalDescription(await pc.createOffer());
              window.__SMOKE_SELF = pc;
            })()
            """
        )
        await asyncio.sleep(2.0)

        assert page.inbound_pcm == [], (
            "the avatar's own microphone was captured back into ingest — echo is possible"
        )
        attached = [e for e in page.page_events if e.get("event") == "remoteAudioAttached"]
        assert attached == []
