"""The Zoom-web connector: joining, and being audible once there.

Two properties carry the weight, and both encode a failure this connector actually
hit against live meetings:

1. **Being in the meeting and being audible are different states.** Zoom does not
   create an audio path until asked, so a join that stops at "in the meeting" leaves
   an avatar that appears in the roster, passes every health check, and cannot be
   heard.
2. **A microphone that cannot publish must fail loudly at start.** The alternative is
   the same silent-but-healthy session, arrived at from the other direction.

The join is tested against the real ``ZoomWebJoiner`` driving a fake browser; the
microphone against real subprocesses, because the interesting behaviour is broken
pipes and backpressure, which a mock would not exercise.
"""

from __future__ import annotations

import asyncio

import pytest
import websockets

from src.connectors.zoom_web.egress.media_sink import ZoomWebMediaSink
from src.connectors.zoom_web.exceptions import (
    ZoomWebAdmissionError,
    ZoomWebJoinTimeoutError,
)
from src.connectors.zoom_web.meeting.join import ZoomWebJoiner
from src.connectors.zoom_web.page.protocol import HEADER_SIZE, MAGIC
from src.connectors.zoom_web.page.server import PageAudioServer
from src.domain.avatar import AVATAR_INPUT_FORMAT
from src.domain.context import FrameContext
from src.domain.health import ComponentHealth, ComponentState, HealthReport
from src.domain.ids import CorrelationId, SessionId
from src.domain.media import AudioFrame
from src.domain.meeting import MeetingContext, MeetingPlatform
from src.domain.session import SessionContext
from src.services.media.clock import MediaClock
from tests.fakes.meet_page import FakeBrowserDriver

IN_MEETING = "button[aria-label='Leave']"
JOIN_AUDIO = ".join-audio-container__btn:not([aria-disabled='true'])"
"""The enabled form. Zoom renders the control disabled first, and the selectors
exclude that state so a poll misses instantly instead of burning a click timeout."""
UNMUTE = "button[aria-label='Unmute']"
PCM = bytes(640)


def _ctx() -> FrameContext:
    return FrameContext(
        session_id=SessionId("ses_zoomweb000000000000000000000"),
        correlation_id=CorrelationId("cor_zoomweb000000000000000000000"),
    )


def _joiner(driver: FakeBrowserDriver, timeout_s: float = 5.0) -> ZoomWebJoiner:
    return ZoomWebJoiner(driver=driver, timeout_s=timeout_s, poll_interval_s=0.01)


# --------------------------------------------------------------------------- #
# Joining
# --------------------------------------------------------------------------- #


async def test_a_clean_join_fills_the_form_and_connects_audio() -> None:
    driver = FakeBrowserDriver(
        visible={"input#input-for-name", "input#input-for-pwd", "button:has-text('Join')"}
    )

    def on_click(fake: FakeBrowserDriver, selector: str) -> None:
        if selector == "button:has-text('Join')":
            fake.visible.update({IN_MEETING, JOIN_AUDIO})

    driver._on_click = on_click

    outcome = await _joiner(driver).join(
        meeting_number="94241716923", passcode="139601", display_name="AI Avatar"
    )

    assert outcome.in_meeting
    assert outcome.audio_joined
    assert ("input#input-for-name", "AI Avatar") in driver.filled
    assert ("input#input-for-pwd", "139601") in driver.filled
    assert JOIN_AUDIO in driver.clicked


async def test_the_join_url_targets_the_redirect_destination() -> None:
    """``zoom.us/wc`` redirects to ``app.zoom.us``; the selectors match the latter."""
    assert (
        _joiner(FakeBrowserDriver()).join_url("94241716923")
        == "https://app.zoom.us/wc/94241716923/join"
    )


async def test_being_in_the_meeting_is_not_enough_to_finish() -> None:
    """The failure that looks exactly like success. It must time out, not succeed."""
    driver = FakeBrowserDriver(visible={IN_MEETING})

    with pytest.raises(ZoomWebJoinTimeoutError, match="audio_joined=False"):
        await _joiner(driver, timeout_s=0.2).join(
            meeting_number="1", passcode=None, display_name="AI Avatar"
        )


async def test_a_meeting_without_a_passcode_does_not_fill_one() -> None:
    driver = FakeBrowserDriver(visible={IN_MEETING, JOIN_AUDIO})

    await _joiner(driver).join(
        meeting_number="1", passcode=None, display_name="AI Avatar"
    )

    assert all(selector != "input#input-for-pwd" for selector, _ in driver.filled)


async def test_a_disabled_audio_control_is_not_clicked() -> None:
    """Zoom renders "join audio" before enabling it.

    Matching the disabled element made Playwright wait its full click timeout on
    every poll — five seconds each, roughly a third of the join budget — for a
    control that could not be clicked. The join must skip it and keep polling.
    """
    disabled = ".join-audio-container__btn"  # the bare, disabled form
    driver = FakeBrowserDriver(visible={IN_MEETING, disabled})

    with pytest.raises(ZoomWebJoinTimeoutError, match="audio_joined=False"):
        await _joiner(driver, timeout_s=0.2).join(
            meeting_number="1", passcode=None, display_name="AI Avatar"
        )

    assert disabled not in driver.clicked


async def test_the_control_is_clicked_once_zoom_enables_it() -> None:
    """The other half: a control that becomes enabled must be taken."""
    disabled = ".join-audio-container__btn"
    driver = FakeBrowserDriver(visible={IN_MEETING, disabled})
    polls = 0
    original = driver.wait_for_any

    async def enable_on_third(selectors, *, timeout_s):
        nonlocal polls
        polls += 1
        if polls >= 3:
            driver.visible.discard(disabled)
            driver.visible.add(JOIN_AUDIO)
        return await original(selectors, timeout_s=timeout_s)

    driver.wait_for_any = enable_on_third  # type: ignore[assignment]

    outcome = await _joiner(driver).join(
        meeting_number="1", passcode=None, display_name="AI Avatar"
    )

    assert outcome.audio_joined
    assert JOIN_AUDIO in driver.clicked


async def test_a_muted_admission_is_unmuted() -> None:
    """Zoom can admit a participant muted; "Unmute" showing means we are muted."""
    driver = FakeBrowserDriver(visible={IN_MEETING, JOIN_AUDIO, UNMUTE})

    def on_click(fake: FakeBrowserDriver, selector: str) -> None:
        if selector == UNMUTE:
            fake.visible.discard(UNMUTE)  # the control disappears once unmuted

    driver._on_click = on_click

    outcome = await _joiner(driver).join(
        meeting_number="1", passcode=None, display_name="AI Avatar"
    )

    assert UNMUTE in driver.clicked
    assert outcome.unmuted


async def test_unmuting_is_retried_until_the_control_clears() -> None:
    """One click is not enough: Zoom renders the toolbar after the audio join.

    The first attempts land on a page whose mute control is not ready, and a click
    that does nothing leaves the avatar silent while every other signal says the
    join succeeded.
    """
    driver = FakeBrowserDriver(visible={IN_MEETING, JOIN_AUDIO, UNMUTE})
    clicks = 0

    def on_click(fake: FakeBrowserDriver, selector: str) -> None:
        nonlocal clicks
        if selector == UNMUTE:
            clicks += 1
            if clicks >= 3:  # the first two do nothing, as on a settling toolbar
                fake.visible.discard(UNMUTE)

    driver._on_click = on_click

    outcome = await _joiner(driver).join(
        meeting_number="1", passcode=None, display_name="AI Avatar"
    )

    assert clicks >= 3
    assert outcome.unmuted


async def test_a_stuck_mute_is_reported_rather_than_hidden() -> None:
    """If the control never clears the join still succeeds, but says so.

    A muted avatar is the connector's worst failure mode precisely because it looks
    like success, so it is reported instead of being silently accepted.
    """
    driver = FakeBrowserDriver(visible={IN_MEETING, JOIN_AUDIO, UNMUTE})

    outcome = await _joiner(driver).join(
        meeting_number="1", passcode=None, display_name="AI Avatar"
    )

    assert outcome.in_meeting
    assert not outcome.unmuted


async def test_mute_is_never_clicked() -> None:
    """The inverse mistake, which the label invites.

    Zoom names the control by what a click does, so an already-unmuted avatar shows
    "Mute" — clicking it is the one action that guarantees silence.
    """
    driver = FakeBrowserDriver(
        visible={IN_MEETING, JOIN_AUDIO, "button[aria-label='Mute']"}
    )

    await _joiner(driver).join(
        meeting_number="1", passcode=None, display_name="AI Avatar"
    )

    assert "button[aria-label='Mute']" not in driver.clicked


@pytest.mark.parametrize(
    "page_text",
    ["The host has denied your request", "You have been removed", "Wrong passcode"],
)
async def test_a_refusal_is_fatal_not_retried(page_text: str) -> None:
    """A host who denied entry will deny it again; retrying gets accounts blocked."""
    driver = FakeBrowserDriver(text=page_text)

    with pytest.raises(ZoomWebAdmissionError):
        await _joiner(driver).join(
            meeting_number="1", passcode="0000", display_name="AI Avatar"
        )


async def test_leave_never_raises() -> None:
    """Teardown runs on failure paths; it must not add a second exception."""
    driver = FakeBrowserDriver()

    async def boom(selectors):
        raise RuntimeError("page already gone")

    driver.click_first = boom  # type: ignore[assignment]
    await _joiner(driver).leave()


# --------------------------------------------------------------------------- #
# Being heard
# --------------------------------------------------------------------------- #


async def test_avatar_audio_reaches_the_page() -> None:
    """The publish contract: a frame handed in arrives framed on the page socket."""
    server = PageAudioServer()
    await server.start()
    sink = ZoomWebMediaSink(server=server)
    try:
        async with websockets.connect(server.endpoint) as page:
            assert await server.wait_connected(timeout_s=2.0)
            await sink.publish_audio(_frame())
            payload = await asyncio.wait_for(page.recv(), timeout=2.0)
    finally:
        await server.stop()

    assert payload.startswith(MAGIC)
    assert payload[HEADER_SIZE:] == PCM
    assert sink.published == 1


async def test_publishing_before_the_page_attaches_is_not_fatal() -> None:
    """A page that has not dialled in yet must not raise onto the pacer's path."""
    server = PageAudioServer()
    await server.start()
    sink = ZoomWebMediaSink(server=server)
    try:
        await sink.publish_audio(_frame())
    finally:
        await server.stop()


async def test_an_unattached_page_reports_unhealthy() -> None:
    """The silent-but-healthy failure this connector keeps producing."""
    server = PageAudioServer()
    sink = ZoomWebMediaSink(server=server)

    assert sink.health().state is ComponentState.UNHEALTHY
    assert "not attached" in (sink.health().detail or "")


async def test_a_client_without_the_token_is_refused() -> None:
    """The socket is reachable by anything on the host, so it is not open."""
    server = PageAudioServer()
    await server.start()
    try:
        with pytest.raises(Exception):  # noqa: B017 - any refusal will do
            async with websockets.connect(server.endpoint.split("?")[0]):
                pass
    finally:
        await server.stop()


async def test_stop_is_idempotent() -> None:
    server = PageAudioServer()
    await server.start()
    await server.stop()
    await server.stop()


def _frame() -> AudioFrame:
    return AudioFrame(pcm=PCM, pts_us=0, format=AVATAR_INPUT_FORMAT, ctx=_ctx())


# --------------------------------------------------------------------------- #
# Leaving
# --------------------------------------------------------------------------- #

LEAVE = "button[aria-label='Leave']"
LEAVE_CONFIRM = "button:has-text('Leave Meeting')"


async def test_leaving_confirms_the_dialog_and_verifies_departure() -> None:
    """Zoom's Leave opens a menu; without the confirm the avatar never goes."""
    driver = FakeBrowserDriver(visible={LEAVE, LEAVE_CONFIRM})

    def on_click(fake: FakeBrowserDriver, selector: str) -> None:
        if selector == LEAVE_CONFIRM:
            # Leaving removes the in-meeting controls, which is how departure is
            # detected rather than assumed.
            fake.visible.clear()

    driver._on_click = on_click

    assert await _joiner(driver).leave() is True
    assert LEAVE in driver.clicked
    assert LEAVE_CONFIRM in driver.clicked


async def test_an_offscreen_leave_button_is_clicked_in_page() -> None:
    """The reason the avatar stayed in the meeting.

    Zoom's Leave sits in a fixed footer that reports as "visible, enabled and
    stable" and simultaneously "outside of the viewport", so Playwright scrolls,
    fails, and times out. No selector fixes that and a bigger viewport only moves
    it, so the click is dispatched inside the page — where the element genuinely is
    clickable, Playwright just cannot prove it.
    """
    scripts: list[str] = []

    class OffscreenFooterDriver(FakeBrowserDriver):
        async def click_first(self, selectors):
            return None  # every real click fails the viewport check

        async def evaluate(self, script):
            scripts.append(script)
            self.visible.clear()  # the in-page click lands and we leave
            return True

    driver = OffscreenFooterDriver(visible={LEAVE, IN_MEETING})

    assert await _joiner(driver).leave() is True
    assert scripts, "no in-page click was attempted"
    # ``:has-text`` is Playwright syntax; querySelector cannot parse it.
    assert ":has-text(" not in scripts[0]


async def test_a_leave_that_does_not_take_is_reported() -> None:
    """Closing the browser is not leaving.

    Zoom keeps the participant until its own timeout, so an unconfirmed leave means
    the avatar's tile lingers for everyone else. That must be visible rather than
    reported as a clean stop.
    """
    driver = FakeBrowserDriver(visible={LEAVE, LEAVE_CONFIRM, IN_MEETING})

    assert await _joiner(driver).leave() is False


async def test_the_browser_is_closed_even_when_teardown_steps_fail() -> None:
    """The regression behind an avatar that would not leave.

    ``DELETE /sessions/{id}`` returned success while the participant stayed in the
    meeting, because an ingest ``stop`` raised and the lines after it — including
    the one that closes the browser — never ran. Closing the browser is what
    actually removes the participant, so nothing earlier may prevent it.
    """
    from src.connectors.zoom_web.session.zoom_web_session import ZoomWebSession

    class ExplodingSource:
        async def start(self) -> None: ...

        async def stop(self) -> None:
            raise RuntimeError("the ingest task died earlier")

        async def frames(self):
            return
            yield  # pragma: no cover

        def health(self):
            return ComponentHealth(name="ingest", state=ComponentState.UNKNOWN)

    class RecordingDriver(FakeBrowserDriver):
        def __init__(self) -> None:
            super().__init__()
            self.stop_calls = 0

        async def stop(self) -> None:
            self.stop_calls += 1
            await super().stop()

    class StubRouter:
        def close(self) -> None: ...

        def health(self):
            return HealthReport()

    server = PageAudioServer()
    driver = RecordingDriver()
    zoom = ZoomWebSession(
        session=_session_context(),
        config=_config(),
        clock=MediaClock(),
        driver=driver,
        joiner=_joiner(driver),
        page_server=server,
        source=ExplodingSource(),  # type: ignore[arg-type]
        publisher=ZoomWebMediaSink(server=server),
        router=StubRouter(),  # type: ignore[arg-type]
    )

    await zoom.stop()  # must not raise

    assert driver.stop_calls == 1


def _session_context() -> SessionContext:
    return SessionContext(
        session_id=SessionId("ses_zoomweb000000000000000000000"),
        correlation_id=CorrelationId("cor_zoomweb000000000000000000000"),
        meeting=MeetingContext(
            meeting_number="1",
            display_name="AI Avatar",
            platform=MeetingPlatform.ZOOM_WEB,
        ),
    )


def _config():
    from src.config.settings import Settings
    from src.connectors.zoom_web.config import ZoomWebConnectorConfig

    return ZoomWebConnectorConfig.from_settings(Settings(_env_file=None))
