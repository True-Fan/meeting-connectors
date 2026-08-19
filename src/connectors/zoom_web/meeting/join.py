"""Getting the avatar into a Zoom meeting.

Zoom's web join is a multi-step SPA with no single "ready" signal, and the steps do
not always appear: a meeting without a passcode skips that field, a locked meeting
inserts a waiting room, and the audio prompt exists only once we are actually in. So
this is a **poll over the whole sequence** rather than a script of steps — each cycle
tries every action, and each action is a no-op when its control is absent.

That shape was established empirically. A fixed sequence of waits failed repeatedly
against the live client; retrying the same handful of actions succeeded.

**Joining audio is a separate step from joining the meeting**, and skipping it is the
failure that looks like success: the avatar appears in the roster, every health check
passes, and it is inaudible. Zoom does not create an audio path until asked.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

# Imported from the Google Meet connector rather than moved into shared code.
# Moving it is the better end state — Chromium is not a Meet concept — but that
# refactor touches ~10 files in a connector that is in production, and the brief for
# this change is explicitly not to disturb it. So the coupling is deliberate, narrow
# (a driver protocol and a launch-plan builder, neither of which knows what a meeting
# is), and recorded here as the debt it is.
from src.connectors.google_meet.automation.driver import BrowserDriver
from src.connectors.zoom_web.exceptions import (
    ZoomWebAdmissionError,
    ZoomWebJoinTimeoutError,
)
from src.infrastructure.logging import get_logger

logger = get_logger(__name__)

JOIN_URL_TEMPLATE = "https://app.zoom.us/wc/{meeting_number}/join"
"""``zoom.us/wc/...`` redirects here, and this is the DOM the selectors were written
against. Navigating straight to it removes a redirect that can otherwise be caught
mid-flight with the form not yet present."""


@dataclass(frozen=True, slots=True)
class ZoomWebSelectors:
    """What to click and type. Injected so a Zoom UI change is configuration.

    Ordering is the contract: first visible match wins, so the most specific and most
    recently observed selector goes first.
    """

    name_input: tuple[str, ...] = (
        "input#input-for-name",
        "input[name='displayName']",
        "input[aria-label*='name' i]",
    )
    passcode_input: tuple[str, ...] = (
        "input#input-for-pwd",
        "input[name='password']",
        "input[type='password']",
    )
    join_button: tuple[str, ...] = (
        "button:has-text('Join')",
        "#joinBtn",
        "button[type='submit']",
    )
    join_audio_button: tuple[str, ...] = (
        # **Every selector here excludes the disabled state, deliberately.**
        # Zoom renders this button before its audio subsystem is ready, as
        # ``aria-disabled="true"`` with a ``--disabled`` class. Matching it anyway
        # means Playwright waits its full click timeout for an element that will
        # never become enabled *on that attempt* — five seconds burned per poll,
        # roughly a third of the join budget spent learning nothing.
        #
        # Excluding it makes the miss instant instead, so the loop keeps polling
        # cheaply until Zoom enables the control.
        ".join-audio-container__btn:not([aria-disabled='true'])",
        ".join-audio-by-voip__join-btn:not([aria-disabled='true'])",
        "button:has-text('Join Audio by Computer'):not([aria-disabled='true'])",
        "button:has-text('Join Computer Audio'):not([aria-disabled='true'])",
        "button[aria-label*='join audio' i]:not([aria-disabled='true'])",
    )
    audio_disabled_markers: tuple[str, ...] = field(
        default=(
            ".join-audio-container--disabled",
            "button[aria-label*='join audio' i][aria-disabled='true']",
        )
    )
    """Zoom rendering the audio control but refusing it yet.

    Distinguished from "no control at all" so a stalled join can say *which* it is:
    waiting on Zoom's audio subsystem is a different problem from a selector that
    stopped matching."""

    unmute_button: tuple[str, ...] = (
        # Zoom labels the control by what a click *does*, so "Unmute" showing means
        # we are muted now. Never match "Mute" here: that would silence the avatar.
        #
        # Matched case-insensitively and by substring because the label has carried
        # a suffix in some builds ("unmute my microphone"), and an exact match that
        # misses leaves the avatar muted — silent, but reporting a healthy join.
        "button[aria-label='Unmute']",
        "button[aria-label*='unmute' i]",
        "[role='button'][aria-label*='unmute' i]",
        "button:has-text('Unmute')",
    )
    leave_button: tuple[str, ...] = (
        "button[aria-label='Leave']",
        "button:has-text('Leave')",
    )
    leave_confirm_button: tuple[str, ...] = field(
        default=(
            # Zoom's Leave opens a menu; the meeting is only left on the confirm.
            # Clicking Leave alone dismisses nothing and the avatar stays put.
            "button:has-text('Leave Meeting')",
            "button:has-text('Leave meeting')",
            ".leave-meeting-options__btn--danger",
            "[role='menuitem']:has-text('Leave')",
        )
    )
    in_meeting_markers: tuple[str, ...] = field(
        default=("button[aria-label='Leave']", "[aria-label*='Participants' i]")
    )
    waiting_room_text: tuple[str, ...] = field(
        default=("the meeting host will let you in soon",)
    )
    denied_text: tuple[str, ...] = field(
        default=(
            "you have been removed",
            "the host has denied your request",
            "this meeting has been ended by host",
        )
    )
    passcode_error_text: tuple[str, ...] = field(
        default=("passcode is incorrect", "wrong passcode")
    )


DEFAULT_SELECTORS = ZoomWebSelectors()


@dataclass(frozen=True, slots=True)
class JoinOutcome:
    """What the join achieved. ``audio_joined`` is not implied by ``in_meeting``."""

    in_meeting: bool
    audio_joined: bool
    unmuted: bool = True
    """False when the avatar is in the meeting but could not be unmuted.

    Not an error: the session is otherwise fine and a host can unmute it. It is
    surfaced because a muted avatar is inaudible while every other signal says the
    join succeeded."""


class ZoomWebJoiner:
    """Drives Zoom's web client from a URL to an avatar that can be heard."""

    __slots__ = ("_driver", "_poll_interval_s", "_selectors", "_timeout_s")

    def __init__(
        self,
        *,
        driver: BrowserDriver,
        selectors: ZoomWebSelectors = DEFAULT_SELECTORS,
        timeout_s: float = 90.0,
        poll_interval_s: float = 2.0,
    ) -> None:
        self._driver = driver
        self._selectors = selectors
        self._timeout_s = timeout_s
        self._poll_interval_s = poll_interval_s

    def join_url(self, meeting_number: str) -> str:
        return JOIN_URL_TEMPLATE.format(meeting_number=meeting_number)

    async def join(
        self, *, meeting_number: str, passcode: str | None, display_name: str
    ) -> JoinOutcome:
        """Join the meeting and connect audio.

        Raises:
            ZoomWebAdmissionError: Zoom refused us. Fatal.
            ZoomWebJoinTimeoutError: the sequence did not complete. Recoverable.
        """
        await self._driver.goto(self.join_url(meeting_number), timeout_s=self._timeout_s)
        logger.info("zoom_web.join_navigated", meeting_number=meeting_number)

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._timeout_s
        in_meeting = False
        audio_joined = False
        waiting_logged = False
        audio_disabled_logged = False

        while loop.time() < deadline:
            await self._raise_if_refused()

            if not in_meeting:
                # Idempotent: refilling a filled field is harmless, and the fields
                # vanish once submitted.
                await self._driver.fill_first(self._selectors.name_input, display_name)
                if passcode:
                    await self._driver.fill_first(self._selectors.passcode_input, passcode)
                await self._driver.click_first(self._selectors.join_button)
                in_meeting = await self._is_in_meeting()
                if in_meeting:
                    logger.info("zoom_web.in_meeting")

            if in_meeting and not audio_joined:
                clicked = await self._driver.click_first(self._selectors.join_audio_button)
                if clicked is not None:
                    audio_joined = True
                    logger.info("zoom_web.audio_joined", selector=clicked)
                elif not audio_disabled_logged and await self._audio_control_disabled():
                    audio_disabled_logged = True
                    logger.info(
                        "zoom_web.audio_control_disabled",
                        note="Zoom is showing the join-audio button but has not "
                        "enabled it yet; still polling",
                    )

            if in_meeting and audio_joined:
                unmuted = await self._ensure_unmuted()
                return JoinOutcome(
                    in_meeting=True, audio_joined=True, unmuted=unmuted
                )

            if not in_meeting and not waiting_logged and await self._is_waiting():
                waiting_logged = True
                logger.info("zoom_web.waiting_for_host")

            await asyncio.sleep(self._poll_interval_s)

        detail = f"in_meeting={in_meeting}, audio_joined={audio_joined}"
        if audio_disabled_logged:
            detail += "; Zoom never enabled its join-audio control"
        raise ZoomWebJoinTimeoutError(
            f"did not join meeting {meeting_number} within {self._timeout_s:.0f}s "
            f"({detail})"
        )

    async def leave(self) -> bool:
        """Leave the meeting, and confirm we actually left.

        **Closing the browser is not leaving.** It drops the socket, but Zoom keeps
        the participant until its own timeout expires — so the avatar's tile stays
        visible to everyone else long after the session is gone. The observable
        symptom is a stop that reports success while the avatar is plainly still in
        the meeting.

        So this clicks Leave, clicks the confirmation it opens, and then **waits for
        the in-meeting controls to disappear** before returning. Only that last step
        makes "the avatar left" true rather than assumed.

        Returns whether departure was confirmed. Never raises: teardown must not add
        a second exception over whatever brought us here.
        """
        try:
            clicked = await self._driver.click_first(self._selectors.leave_button)
            if clicked is None:
                # **Not a missing button — an unclickable one.**
                # Zoom's Leave lives in a fixed footer that sits outside the
                # viewport, so Playwright finds it "visible, enabled and stable",
                # scrolls, and still refuses: "element is outside of the viewport".
                # No selector fixes that, and enlarging the viewport only moves the
                # problem. Dispatching the click inside the page skips the
                # actionability checks that are wrong here — the element genuinely
                # is clickable, Playwright simply cannot prove it.
                logger.info("zoom_web.leave_click_via_script")
                if not await self._click_in_page(self._selectors.leave_button):
                    logger.info("zoom_web.leave_control_not_found")
                    return False

            # Leave opens a confirmation menu; the meeting is only left on the
            # confirm. Retried because the menu animates in, and attempted in-page
            # too because the menu is anchored to that same off-screen footer.
            for _ in range(3):
                await asyncio.sleep(0.4)
                if await self._driver.click_first(self._selectors.leave_confirm_button):
                    break
                if await self._click_in_page(self._selectors.leave_confirm_button):
                    break

            for _ in range(10):
                gone = await self._driver.wait_for_any(
                    self._selectors.in_meeting_markers, timeout_s=0.5
                )
                if gone is None:
                    logger.info("zoom_web.left_meeting")
                    return True
                await asyncio.sleep(0.5)

            logger.warning(
                "zoom_web.leave_unconfirmed",
                note="clicked leave but the meeting controls are still present; the "
                "browser is closed anyway, so the participant may linger until Zoom "
                "times it out",
            )
            return False
        except Exception as exc:
            logger.info("zoom_web.leave_click_failed", error=str(exc))
            return False

    async def _click_in_page(self, selectors: tuple[str, ...]) -> bool:
        """Click the first matching element from inside the page.

        Used only where Playwright's actionability checks are wrong rather than
        protective — currently Zoom's fixed footer, which reports as outside the
        viewport and can never be scrolled into it.

        Deliberately narrow: it skips the visibility and stability guarantees a
        normal click gives, so it is a fallback for a known-bad case, not the way
        this connector clicks things. ``:has-text`` is Playwright-only syntax and is
        skipped here, since ``querySelector`` cannot parse it.
        """
        usable = [s for s in selectors if ":has-text(" not in s]
        if not usable:
            return False
        script = f"""() => {{
          const selectors = {json.dumps(usable)};
          for (const selector of selectors) {{
            const el = document.querySelector(selector);
            if (el) {{ el.click(); return true; }}
          }}
          return false;
        }}"""
        try:
            return bool(await self._driver.evaluate(script))
        except Exception as exc:
            logger.info("zoom_web.in_page_click_failed", error=str(exc))
            return False

    async def _ensure_unmuted(self, attempts: int = 8) -> bool:
        """Clear the mute state, retrying until the control disappears.

        **A single click here is not enough, and that is not a timing nicety.**
        Zoom renders its toolbar after the audio join resolves, so the first attempt
        can run against a page that has no mute control yet; and a click that lands
        on a stale node silently does nothing. Either way the avatar stays muted —
        which is the worst failure this connector has, because every other signal
        says the session is fine.

        Success is defined by the control being *gone*, not by the click returning:
        "Unmute" absent means we are unmuted, since Zoom names the button after what
        pressing it would do.
        """
        for attempt in range(attempts):
            still_muted = await self._driver.wait_for_any(
                self._selectors.unmute_button, timeout_s=0.5
            )
            if still_muted is None:
                if attempt:
                    logger.info("zoom_web.unmuted", attempts=attempt + 1)
                return True
            await self._driver.click_first(self._selectors.unmute_button)
            await asyncio.sleep(0.5)

        logger.warning(
            "zoom_web.still_muted",
            note="the avatar is in the meeting but muted, so nothing it says will "
            "be heard; the unmute control did not clear",
        )
        return False

    # -- page state --------------------------------------------------------

    async def _is_in_meeting(self) -> bool:
        """A short timeout on purpose: the outer loop owns waiting, not this call."""
        found = await self._driver.wait_for_any(
            self._selectors.in_meeting_markers, timeout_s=0.5
        )
        return found is not None

    async def _audio_control_disabled(self) -> bool:
        """True when Zoom shows the audio control but has not enabled it."""
        found = await self._driver.wait_for_any(
            self._selectors.audio_disabled_markers, timeout_s=0.3
        )
        return found is not None

    async def _is_waiting(self) -> bool:
        text = await self._safe_text()
        return any(marker in text for marker in self._selectors.waiting_room_text)

    async def _raise_if_refused(self) -> None:
        text = await self._safe_text()
        for marker in self._selectors.denied_text:
            if marker in text:
                raise ZoomWebAdmissionError(marker)
        for marker in self._selectors.passcode_error_text:
            if marker in text:
                raise ZoomWebAdmissionError("passcode rejected")

    async def _safe_text(self) -> str:
        try:
            return (await self._driver.page_text()).lower()
        except Exception:
            return ""
