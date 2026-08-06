"""Google Meet doubles: a fake browser, and a fake page that speaks the real protocol.

These two are the second implementations that justify the ``BrowserDriver`` seam in
``connectors/google_meet/automation/driver.py``. Between them the whole Meet connector —
join flow, page handshake, media in both directions, roster, recovery — runs with **no
Chromium, no Google account, and no meeting**.

``FakeBrowserDriver`` replaces Playwright. Its DOM is a set of strings, which is enough
because everything above it only ever asks two questions of a page: is this selector
visible, and what text does the body contain. Scripted state transitions let a test say
"this meeting puts us in a lobby and admits us on the third poll" in one line.

``FakePage`` is deliberately *not* a fake. It is a real WebSocket client that connects to
the real ``PageBridgeServer`` over loopback and encodes and decodes with the real
``protocol.py``. So the tests that use it exercise the actual wire format, the actual token
check, the actual framing, and the actual backpressure path — everything except the
JavaScript. That is where the value is: the codec is the one place a Python/JS mismatch
would be invisible until a live meeting.
"""

from __future__ import annotations

import asyncio
import json
import re
import struct
from collections.abc import Callable, Iterable
from contextlib import suppress
from pathlib import Path

from websockets.asyncio.client import ClientConnection, connect

from src.connectors.google_meet.browser.launcher import LaunchPlan
from src.connectors.google_meet.exceptions import BrowserCrashedError, BrowserUnavailableError
from src.connectors.google_meet.websocket.protocol import (
    AUDIO_HEADER_SIZE,
    VIDEO_HEADER_SIZE,
    MeetFlags,
    MeetMessage,
    MeetMessageType,
    MeetState,
    decode,
    decode_video_payload,
    encode_header,
    encode_json,
)
from src.domain.avatar import AVATAR_INPUT_FORMAT

# --------------------------------------------------------------------------- #
# Canned page states
# --------------------------------------------------------------------------- #

IN_CALL_SELECTOR = 'button[aria-label*="Leave call" i]'
JOIN_NOW_SELECTOR = '//button[.//span[text()="Join now"]]'
ASK_TO_JOIN_SELECTOR = '//button[.//span[text()="Ask to join"]]'
LOBBY_SELECTOR = '[aria-label*="Asking to join" i]'
MIC_ON_SELECTOR = 'button[aria-label*="Turn off microphone" i]'
MIC_OFF_SELECTOR = 'button[aria-label*="Turn on microphone" i]'
CAM_ON_SELECTOR = 'button[aria-label*="Turn off camera" i]'
CAM_OFF_SELECTOR = 'button[aria-label*="Turn on camera" i]'

SIGNED_IN_SCRIPT_RESULT = "avatar@example.com"


class FakeBrowserDriver:
    """An in-memory ``BrowserDriver``.

    Args:
        visible: Selectors the page starts with.
        text: Body text the page starts with.
        on_click: Called with a clicked selector, so a test can mutate the DOM the way a
            real click would. This is what lets "clicking Join now puts us in the lobby" be
            expressed without a browser.
        script_result: What ``evaluate`` returns. Drives the signed-in probe: the default
            reports an account, and ``None`` makes the profile look signed out.
        crash_after_goto: Navigations to allow before the page "crashes", for exercising
            rejoin.
        auto_page: Open a real ``FakePage`` against the injected bridge endpoint on first
            navigation, the way a real init script would. This is what lets a test drive the
            whole bridge — handshake, media, roster — over a real socket.
    """

    def __init__(
        self,
        *,
        visible: Iterable[str] = (),
        text: str = "",
        on_click: Callable[[FakeBrowserDriver, str], None] | None = None,
        script_result: object = SIGNED_IN_SCRIPT_RESULT,
        crash_after_goto: int | None = None,
        auto_page: bool = False,
    ) -> None:
        self.visible: set[str] = set(visible)
        self.text = text
        self._on_click = on_click
        self.script_result = script_result
        self._crash_after_goto = crash_after_goto
        self._auto_page = auto_page

        self.plan: LaunchPlan | None = None
        self.init_scripts: list[str] = []
        self.visited: list[str] = []
        self.clicked: list[str] = []
        self.filled: list[tuple[str, str]] = []
        self.screenshots: list[Path] = []
        self.started = 0
        self.stopped = 0
        self.crashed = False
        self.page: FakePage | None = None

    # -- BrowserDriver -----------------------------------------------------

    async def start(self, plan: LaunchPlan) -> None:
        self.plan = plan
        self.started += 1

    async def add_init_script(self, script: str) -> None:
        self.init_scripts.append(script)

    async def goto(self, url: str, *, timeout_s: float) -> None:
        if self.crashed:
            raise BrowserCrashedError("fake page has crashed")
        self.visited.append(url)
        if self._crash_after_goto is not None and len(self.visited) > self._crash_after_goto:
            self.crashed = True
            raise BrowserCrashedError("fake page crashed on navigation")
        if self._auto_page and self.page is None:
            # A real init script opens the socket on first navigation. Doing the same here is
            # what makes the bridge's ordering testable: the page has to attach *after* the
            # server is bound and *before* the CONFIG handshake.
            self.page = FakePage(self.bridge_endpoint)
            await self.page.open()

    async def wait_for_any(self, selectors: tuple[str, ...], *, timeout_s: float) -> str | None:
        """Match immediately, or poll until the deadline.

        Real polling rather than an instant answer, because the join flow's correctness
        depends on it re-checking: a test that scripts "admitted on the third poll" needs
        the loop to actually run.
        """
        deadline = asyncio.get_running_loop().time() + timeout_s
        while True:
            for selector in selectors:
                if selector in self.visible:
                    return selector
            if asyncio.get_running_loop().time() >= deadline:
                return None
            await asyncio.sleep(0.01)

    async def click_first(self, selectors: tuple[str, ...]) -> str | None:
        for selector in selectors:
            if selector in self.visible:
                self.clicked.append(selector)
                if self._on_click is not None:
                    self._on_click(self, selector)
                return selector
        return None

    async def fill_first(self, selectors: tuple[str, ...], value: str) -> str | None:
        for selector in selectors:
            if selector in self.visible:
                self.filled.append((selector, value))
                return selector
        return None

    async def page_text(self) -> str:
        return self.text

    async def evaluate(self, script: str) -> object:
        if self.crashed:
            raise BrowserUnavailableError("fake page has crashed")
        return self.script_result

    async def screenshot(self, path: Path) -> bool:
        self.screenshots.append(path)
        return True

    def is_alive(self) -> bool:
        return not self.crashed and self.stopped == 0

    async def stop(self) -> None:
        self.stopped += 1
        page, self.page = self.page, None
        if page is not None:
            # A closed browser takes its socket with it, which is exactly the signal the
            # bridge's read loop is waiting for.
            await page.close()

    # -- helpers for tests -------------------------------------------------

    def show(self, *selectors: str) -> None:
        self.visible.update(selectors)

    def hide(self, *selectors: str) -> None:
        self.visible.difference_update(selectors)

    @property
    def bridge_config_script(self) -> str:
        """The injected preamble, so a test can assert what the page was told."""
        return next((s for s in self.init_scripts if "__MC_BRIDGE_CONFIG__" in s), "")

    @property
    def bridge_config(self) -> dict[str, object]:
        """The config the bridge injected, parsed back out of the preamble.

        Reads what the page would actually receive rather than what a test assumes, so a
        change to how the preamble is built cannot make these tests pass vacuously.
        """
        match = re.search(
            r"window\.__MC_BRIDGE_CONFIG__ = (\{.*?\});\n", self.bridge_config_script, re.DOTALL
        )
        assert match is not None, "no bridge config was injected"
        return json.loads(match.group(1))

    @property
    def bridge_endpoint(self) -> str:
        return str(self.bridge_config["endpoint"])

    @property
    def injected_worklets(self) -> str:
        return next((s for s in self.init_scripts if "__MC_BRIDGE_WORKLETS__" in s), "")


def joined_driver(**kwargs: object) -> FakeBrowserDriver:
    """A driver already in a call, with the microphone and camera off.

    The common starting point: the join flow finds the call, and ``MeetControls`` then has
    real work to do turning the devices on — which is the step whose absence produces a
    silent avatar.
    """
    return FakeBrowserDriver(
        visible={IN_CALL_SELECTOR, MIC_OFF_SELECTOR, CAM_OFF_SELECTOR},
        on_click=_toggle_devices,
        **kwargs,  # type: ignore[arg-type]
    )


def _toggle_devices(driver: FakeBrowserDriver, selector: str) -> None:
    """Mirror Meet's action-labelled buttons: clicking one swaps it for its opposite."""
    swaps = {
        MIC_OFF_SELECTOR: MIC_ON_SELECTOR,
        MIC_ON_SELECTOR: MIC_OFF_SELECTOR,
        CAM_OFF_SELECTOR: CAM_ON_SELECTOR,
        CAM_ON_SELECTOR: CAM_OFF_SELECTOR,
    }
    replacement = swaps.get(selector)
    if replacement is not None:
        driver.hide(selector)
        driver.show(replacement)


# --------------------------------------------------------------------------- #
# A real WebSocket client standing in for the page
# --------------------------------------------------------------------------- #


class FakePage:
    """A real WebSocket client that speaks the real page protocol.

    Everything the injected JavaScript would do, minus the JavaScript: it connects, sends
    ``HELLO``, answers ``CONFIG`` with ``READY``, ships PCM up, and collects the video and
    audio sent down.
    """

    def __init__(self, endpoint: str) -> None:
        self._endpoint = endpoint
        self._connection: ClientConnection | None = None
        self._reader: asyncio.Task[None] | None = None

        self.config: dict[str, object] | None = None
        self.received_video: list[tuple[int, int, int]] = []
        """``(width, height, payload_bytes)`` per frame, so geometry is assertable without
        holding megabytes of planes in memory."""
        self.received_audio: list[bytes] = []
        self.leave_requested = False
        self.heartbeat_replies = 0
        self.configured = asyncio.Event()

        self._audio_seq = 0

    # -- lifecycle ---------------------------------------------------------

    async def open(self) -> None:
        self._connection = await connect(self._endpoint, max_size=None)
        self._reader = asyncio.create_task(self._read(), name="fake-page-reader")
        await self._send(
            encode_json(
                MeetMessageType.HELLO,
                {"wire_version": 1, "user_agent": "FakePage/1", "url": "https://meet.google.com/"},
            )
        )

    async def close(self) -> None:
        reader, self._reader = self._reader, None
        if reader is not None:
            reader.cancel()
            with suppress(asyncio.CancelledError):
                await reader
        connection, self._connection = self._connection, None
        if connection is not None:
            await connection.close()

    async def _send(self, payload: bytes) -> None:
        connection = self._connection
        if connection is None:
            raise RuntimeError("FakePage is not open")
        await connection.send(payload)

    # -- the page's own behaviour ------------------------------------------

    async def _read(self) -> None:
        connection = self._connection
        if connection is None:
            return
        async for raw in connection:
            message = decode(raw if isinstance(raw, bytes) else raw.encode())
            await self._handle(message)

    async def _handle(self, message: MeetMessage) -> None:
        match message.msg_type:
            case MeetMessageType.CONFIG:
                self.config = message.json()
                await self._send(
                    encode_json(
                        MeetMessageType.READY,
                        {
                            # Echo the configured values back, which is what the real page
                            # does after constructing its contexts at those rates. A test
                            # that wants to prove ``_verify_page_media`` bites overrides
                            # them with ``ready_override``.
                            "capture_sample_rate_hz": self.config.get("captureSampleRateHz"),
                            "publish_sample_rate_hz": self.config.get("publishSampleRateHz"),
                            "video_width": self.config.get("videoWidth"),
                            "video_height": self.config.get("videoHeight"),
                        },
                    )
                )
                self.configured.set()

            case MeetMessageType.VIDEO_I420:
                header, planes = decode_video_payload(message.payload)
                self.received_video.append((header.width, header.height, len(planes)))

            case MeetMessageType.AUDIO_PCM:
                self.received_audio.append(message.payload[AUDIO_HEADER_SIZE:])

            case MeetMessageType.LEAVE:
                self.leave_requested = True

            case MeetMessageType.HEARTBEAT:
                self.heartbeat_replies += 1

            case _:
                pass

    # -- driving the connector from the page side --------------------------

    async def send_audio(self, pcm: bytes) -> None:
        """Ship one PCM frame up, in the avatar's fixed input format.

        Built by hand rather than through ``encode_audio`` on purpose: that helper takes a
        domain ``AudioFrame``, and the real page has no domain types. Assembling the header
        here is what proves ``audio_capture/mapping.py`` accepts bytes the JavaScript could
        actually produce.
        """
        await self._send(self._audio_message(pcm, AVATAR_INPUT_FORMAT.sample_rate_hz, 1))

    async def send_audio_at(self, pcm: bytes, *, sample_rate_hz: int, channels: int = 1) -> None:
        """Ship audio in the wrong format, to prove the boundary rejects it."""
        await self._send(self._audio_message(pcm, sample_rate_hz, channels))

    def _audio_message(self, pcm: bytes, sample_rate_hz: int, channels: int) -> bytes:
        header = struct.pack(">IBBHI", sample_rate_hz, channels, 1, 20, 0)
        payload_len = AUDIO_HEADER_SIZE + len(pcm)
        message = (
            encode_header(
                MeetMessageType.AUDIO_PCM,
                payload_len=payload_len,
                seq=self._audio_seq,
                pts_us=0,
                flags=MeetFlags.MIXED,
            )
            + header
            + pcm
        )
        self._audio_seq += 1
        return message

    async def send_state(self, state: MeetState) -> None:
        await self._send(encode_json(MeetMessageType.MEET_STATE, {"state": str(state)}))

    async def send_participants(
        self, names: Iterable[str], *, self_name: str = "AI Avatar"
    ) -> None:
        await self._send(
            encode_json(
                MeetMessageType.PARTICIPANTS,
                {
                    "participants": [
                        {"id": f"p{i}", "name": name} for i, name in enumerate(names)
                    ],
                    "selfName": self_name,
                },
            )
        )

    async def send_error(self, code: str, message: str, *, fatal: bool = False) -> None:
        await self._send(
            encode_json(
                MeetMessageType.ERROR, {"code": code, "message": message, "fatal": fatal}
            )
        )

    async def send_heartbeat(self) -> None:
        await self._send(encode_json(MeetMessageType.HEARTBEAT, {"sent_at_us": 1234}))


def video_payload_size(width: int, height: int) -> int:
    """Bytes in one packed I420 frame plus its wire header."""
    return VIDEO_HEADER_SIZE + width * height * 3 // 2
