"""The join flow.

Getting a browser into a Google Meet conference is a sequence of UI states, and the only
hard part is that **the failure modes are not symmetric**. Some outcomes deserve a retry
and some must never be retried, and getting that wrong is the difference between a session
that recovers and an account that gets flagged for hammering a meeting it was thrown out
of.

    navigate
        │
        ├── not signed in ─────────────▶ GoogleAuthError            (fatal)
        │
    dismiss interstitials
        │
    click "Join now" / "Ask to join"
        │
        ├── in call ───────────────────▶ JOINED
        ├── "Asking to join" ──────────▶ LOBBY ──┬── admitted ────▶ JOINED
        │                                        └── timeout ─────▶ JoinTimeoutError
        ├── request denied ────────────▶ MeetingAdmissionError      (fatal)
        └── meeting has ended ─────────▶ MeetingEndedError          (terminal)

Two properties of this module are load-bearing:

**The lobby is not a failure and does not share the join timeout.** "Asking to join" means
a human has to notice a notification and click Admit, which routinely takes minutes.
Charging that wait against ``join_timeout_s`` would abandon meetings that were about to let
us in; hence ``lobby_timeout_s``, and hence the distinction being modelled at all.

**Denial and ejection are terminal, deliberately.** A host who clicked "Deny" will click it
again. Retrying is not resilience there — it is the behaviour that gets an automated Google
account restricted, and it is worth failing a session loudly to avoid.

This module drives ``BrowserDriver`` and reads ``MeetSelectors``. It holds no Playwright
import and no media, so the whole flow is testable against a fake page.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from src.connectors.google_meet.auth.google_login import verify_signed_in
from src.connectors.google_meet.automation.driver import BrowserDriver
from src.connectors.google_meet.automation.selectors import MeetSelectors
from src.connectors.google_meet.exceptions import (
    JoinTimeoutError,
    MeetingAdmissionError,
    MeetingEndedError,
)
from src.connectors.google_meet.meeting.meet_url import MeetJoinTarget
from src.connectors.google_meet.websocket.protocol import MeetState
from src.infrastructure.logging import get_logger

logger = get_logger(__name__)

_LOBBY_POLL_S = 2.0
_SETTLE_S = 1.0


@dataclass(frozen=True, slots=True)
class JoinOutcome:
    """How the join ended."""

    state: MeetState
    target: MeetJoinTarget
    waited_in_lobby_s: float = 0.0
    matched_join_button: str | None = None
    """Which selector actually worked.

    Recorded because ``automation/selectors.py`` carries several candidates per concept
    precisely so that Meet can rename one without breaking us — and the only way to know a
    rename happened is to see which candidate is being used in production."""


class MeetJoiner:
    """Drives one browser from a URL to being in a conference."""

    __slots__ = ("_display_name", "_driver", "_join_timeout_s", "_lobby_timeout_s", "_selectors")

    def __init__(
        self,
        *,
        driver: BrowserDriver,
        selectors: MeetSelectors,
        display_name: str,
        join_timeout_s: float = 120.0,
        lobby_timeout_s: float = 300.0,
    ) -> None:
        self._driver = driver
        self._selectors = selectors
        self._display_name = display_name
        self._join_timeout_s = join_timeout_s
        self._lobby_timeout_s = lobby_timeout_s

    async def join(self, target: MeetJoinTarget) -> JoinOutcome:
        """Navigate to ``target`` and get into the conference.

        Raises:
            GoogleAuthError: the profile has no valid Google session.
            MeetingAdmissionError: entry was denied. Fatal.
            MeetingEndedError: the conference is over. Terminal.
            JoinTimeoutError: the join or the lobby wait ran out. Recoverable.
            BrowserUnavailableError: Chromium went away.
        """
        # Checked before navigating to the meeting, not after a join fails. An
        # unauthenticated profile lands on a Meet page that looks almost identical to a
        # normal pre-join screen, so without this the symptom is an unexplained timeout
        # rather than "you are not signed in".
        (await verify_signed_in(self._driver, timeout_s=self._join_timeout_s)).require_signed_in()

        logger.info("meet_join.navigating", target=str(target))
        await self._driver.goto(target.url, timeout_s=self._join_timeout_s)

        await self._dismiss_interstitials()
        await self._supply_name_if_asked()

        matched = await self._press_join()
        state = await self._await_admission()

        waited = 0.0
        if state is MeetState.LOBBY:
            waited = await self._wait_out_lobby()
            state = MeetState.JOINED

        logger.info(
            "meet_join.joined",
            target=str(target),
            lobby_wait_s=round(waited, 1),
            join_button=matched,
        )
        return JoinOutcome(
            state=state,
            target=target,
            waited_in_lobby_s=waited,
            matched_join_button=matched,
        )

    # -- steps -------------------------------------------------------------

    async def _dismiss_interstitials(self) -> None:
        """Clear whatever Meet put in front of the join screen.

        Best-effort and bounded: none of these is required to be present, and a fixed
        number of passes stops a dialog that reappears from looping forever. Each pass
        clicks at most one thing, because dismissing one dialog frequently reveals
        another.
        """
        for _ in range(4):
            matched = await self._driver.click_first(self._selectors.dismiss_buttons)
            if matched is None:
                return
            logger.debug("meet_join.dismissed", selector=matched)
            await asyncio.sleep(0.4)

    async def _supply_name_if_asked(self) -> None:
        """Fill the name field, which should never be there.

        A signed-in profile joins under its account's name, so this field appearing means
        the Google session was lost between ``verify_signed_in`` and now. Filling it lets
        the join proceed as a guest rather than failing outright — but it is logged as a
        warning, because a guest cannot join a meeting restricted to the host's
        organisation and the resulting denial would otherwise look inexplicable.
        """
        matched = await self._driver.fill_first(self._selectors.name_input, self._display_name)
        if matched is not None:
            logger.warning(
                "meet_join.guest_name_supplied",
                selector=matched,
                note="Meet asked for a name, so the profile is not signed in; joining as "
                "a guest, which many meetings refuse",
            )

    async def _press_join(self) -> str | None:
        """Find and click the join button.

        Returns the selector that matched, or ``None`` when the page was already in the
        call — which happens on a rejoin into a meeting the browser never fully left.
        """
        if await self._in_call():
            logger.info("meet_join.already_in_call")
            return None

        found = await self._driver.wait_for_any(
            self._selectors.join_button, timeout_s=self._join_timeout_s
        )
        if found is None:
            await self._raise_for_terminal_state()
            raise JoinTimeoutError(
                "no join button appeared within "
                f"{self._join_timeout_s}s; Meet's pre-join screen may have changed "
                "(see automation/selectors.py) or the meeting code may be invalid"
            )

        clicked = await self._driver.click_first(self._selectors.join_button)
        if clicked is None:  # pragma: no cover - visible then unclickable is a race
            raise JoinTimeoutError(f"join button {found!r} became unclickable")
        logger.info("meet_join.pressed", selector=clicked)
        return clicked

    async def _await_admission(self) -> MeetState:
        """Wait until we are in the call, in the lobby, or refused.

        Polls rather than waiting on a single selector because the outcomes are mutually
        exclusive alternatives and three of the four are text-only. Checking terminal
        states *first* on each pass is what stops a stale leave button from reading as
        ``JOINED`` for the moment after a host removes us.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._join_timeout_s

        while True:
            await self._raise_for_terminal_state()

            if await self._in_call():
                return MeetState.JOINED
            if await self._in_lobby():
                logger.info("meet_join.in_lobby")
                return MeetState.LOBBY

            if loop.time() >= deadline:
                raise JoinTimeoutError(
                    f"neither joined nor placed in a lobby within {self._join_timeout_s}s"
                )
            await asyncio.sleep(0.5)

    async def _wait_out_lobby(self) -> float:
        """Wait for a host to admit us, on the lobby's own, longer budget.

        Returns the seconds spent waiting.

        Raises:
            MeetingAdmissionError: the request was denied.
            JoinTimeoutError: nobody admitted us in time.
        """
        loop = asyncio.get_running_loop()
        started = loop.time()
        deadline = started + self._lobby_timeout_s

        while True:
            await self._raise_for_terminal_state()

            if await self._in_call():
                # A short settle before reporting success. Meet mounts the call UI a beat
                # before media negotiation completes, and starting the media pipeline into
                # a peer connection that has not finished setting up loses the first
                # second of audio.
                await asyncio.sleep(_SETTLE_S)
                return loop.time() - started

            if loop.time() >= deadline:
                raise JoinTimeoutError(
                    f"nobody admitted the avatar within {self._lobby_timeout_s}s in the "
                    "lobby; the host may not have seen the request"
                )
            await asyncio.sleep(_LOBBY_POLL_S)

    # -- state probes ------------------------------------------------------

    async def _in_call(self) -> bool:
        return await self._driver.wait_for_any(self._selectors.in_call, timeout_s=0.0) is not None

    async def _in_lobby(self) -> bool:
        if await self._driver.wait_for_any(self._selectors.lobby, timeout_s=0.0) is not None:
            return True
        return self._text_has(await self._driver.page_text(), self._selectors.lobby_text)

    async def _raise_for_terminal_state(self) -> None:
        """Fail immediately on an outcome no retry can improve.

        Raises:
            MeetingAdmissionError: denied entry or removed from the call.
            MeetingEndedError: the conference is over.
        """
        text = await self._driver.page_text()

        if self._text_has(text, self._selectors.ejected_text):
            raise MeetingAdmissionError("the avatar was removed from the meeting")
        if self._text_has(text, self._selectors.denied_text):
            raise MeetingAdmissionError("the host denied the request to join")
        if self._text_has(text, self._selectors.sign_in_text):
            # Reached only if the Google session expired between the pre-flight check and
            # here. Reported as an admission failure rather than an auth error because at
            # this point Meet, not Google, is the thing refusing us.
            raise MeetingAdmissionError(
                "Meet is asking the avatar to sign in; the browser profile's Google "
                "session expired mid-join"
            )
        if self._text_has(text, self._selectors.ended_text) and not await self._in_call():
            # Guarded on ``_in_call`` because "you left the meeting" copy can linger in the
            # DOM from a previous call while we are legitimately in a new one.
            raise MeetingEndedError("the meeting has ended")

    @staticmethod
    def _text_has(text: str, needles: tuple[str, ...]) -> bool:
        lowered = text.lower()
        return any(needle.lower() in lowered for needle in needles)
