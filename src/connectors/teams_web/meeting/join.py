"""Getting the avatar into a Teams meeting.

Teams' web join is a multi-step SPA with no single "ready" signal, and the steps do not
always appear: a launcher page offering the desktop app shows up for a meetup-join link and
not for the web client's own join form, a tenant user skips the name field an anonymous guest
must fill, and the lobby exists only when the organiser left it on. So this is a **poll over
the whole sequence** rather than a script of steps — each cycle tries every action, and each
action is a no-op when its control is absent.

That shape is inherited rather than guessed: the Zoom-web joiner arrived at it after a fixed
sequence of waits failed repeatedly against the live client, and the reason it worked there
applies here in a stronger form. Teams has *more* optional steps than Zoom, not fewer.

**Two routes in, and the poll loop serves both.**

1. **A join link** — ``platform_data["meeting_url"]``, or the same URL supplied in
   ``meeting_number``, which is a natural operator mistake worth accepting. Navigated to
   directly. This is the route a calendar invite gives you.
2. **A meeting id and passcode** — what a Teams invite prints as "Meeting ID". The joiner
   navigates to the web client's join form and fills them in.

**What is deliberately *not* here: audio joining.** Zoom creates no audio path until asked,
which is the failure that looks like success on that connector — the avatar appears in the
roster, every health check passes, and it is inaudible. Teams negotiates audio as part of the
join itself, so there is no separate step to miss. The equivalent trap here is the
**pre-join mute state**: Teams remembers a muted microphone across sessions in a persistent
profile, and a muted avatar is inaudible while every other signal says the join succeeded.
Hence ``_ensure_unmuted``, which is checked before *and* after the join button.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from urllib.parse import quote

# Imported from the Google Meet connector rather than moved into shared code.
# Moving it is the better end state — Chromium is not a Meet concept — but that refactor
# touches ~10 files across two connectors that are in production, and the brief for this
# change is explicitly not to disturb them. So the coupling is deliberate, narrow (a driver
# protocol and a launch-plan builder, neither of which knows what a meeting is), identical to
# the one ``connectors/zoom_web`` already takes, and recorded here as the debt it is.
from src.connectors.google_meet.automation.driver import BrowserDriver
from src.connectors.teams_web.exceptions import (
    TeamsWebAdmissionError,
    TeamsWebJoinTargetError,
    TeamsWebJoinTimeoutError,
)
from src.infrastructure.logging import get_logger

logger = get_logger(__name__)

_JOIN_URL_MARKERS = ("meetup-join", "/meet/")
"""What makes a string a Teams join link rather than a meeting id.

**Two formats, and missing the second one cost a live test run.** Microsoft ships at least
these shapes, and the differences are not cosmetic:

* ``teams.microsoft.com/l/meetup-join/19%3ameeting_…%40thread.v2/0?context=…`` — the classic
  work/school link. Carries the thread id and the tenant.
* ``teams.live.com/meet/9350242031207?p=<passcode>`` — a **personal / free ("Teams for Life")**
  link, and the shape a consumer account's "Copy link" produces. Carries the id *and the
  passcode* in the URL. ``teams.microsoft.com/meet/<id>?p=…`` is the same short form for
  work/school accounts.

The first version of this matched ``meetup-join`` alone, so a `teams.live.com` link failed the
test, fell through to the meeting-id route, and navigated the work/school join form — which for
a signed-in personal account redirects to the Teams app home. No join form, nothing to fill,
and a join that times out with no selector at fault. That is the bug this list exists to
prevent, and it is why ``/meet/`` is here.

``connectors/teams/graph/join_url.looks_like_join_url`` makes the narrower test on purpose:
Graph needs a thread id, which only the first shape carries. Duplicated rather than imported
because a connector that imports another's parser is coupled to its release cycle, which
``tests/architecture/test_layering.py`` exists to prevent — and this connector needs a *URL to
navigate*, not a Graph descriptor."""


def looks_like_join_url(value: str) -> bool:
    """True when ``value`` is a Teams join link rather than a meeting id.

    **An absolute URL is required**, which is what keeps the loose ``/meet/`` marker safe: a
    bare meeting id can never match, and neither can a stray path fragment.
    """
    lowered = value.strip().lower()
    if not lowered.startswith(("http://", "https://")):
        return False
    return any(marker in lowered for marker in _JOIN_URL_MARKERS)


ROUTE_FALLBACK_POLLS = 4
"""Polls in which the page responded to *nothing* before the id route tries elsewhere.

Four rather than one, because a Teams web client takes several seconds to render its join form
and a single unresponsive poll is normal. Four is comfortably past that and comfortably inside
the default 120 s budget, so the fallback happens early enough to be useful and late enough not
to fire at a page that was merely loading. See ``TeamsWebJoiner.alternate_target``."""


def normalise_meeting_id(raw: str) -> str:
    """Strip the presentation spacing out of a printed Teams meeting id.

    Teams prints a 9-12 digit id in space-separated groups ("281 442 953 617"), and an
    operator pastes what is printed. The join form wants the digits.
    """
    return "".join(raw.split())


@dataclass(frozen=True, slots=True)
class TeamsWebSelectors:
    """What to click and type. Injected so a Teams UI change is configuration.

    Ordering is the contract: first visible match wins, so the most specific and most
    recently observed selector goes first. Every list is tried on every poll cycle and a
    miss is free, which is what lets one loop cover both join routes.
    """

    web_client_button: tuple[str, ...] = (
        # The launcher page a meetup-join link lands on. Only the web option is reachable
        # here: there is no desktop Teams in the container, and a native app's own dialog
        # cannot be clicked from a page at all.
        "[data-tid='joinOnWeb']",
        "button[data-tid='joinOnWeb']",
        "a[data-tid='joinOnWeb']",
        "button:has-text('Continue on this browser')",
        "button:has-text('Join on the web instead')",
        "a:has-text('Continue on this browser')",
        "a:has-text('Use the web app instead')",
    )
    """"Continue on this browser", under whichever of its several names.

    Microsoft has reworded this control repeatedly ("Join on the web instead", "Continue on
    this browser", "Use the web app instead") and the ``data-tid`` has outlived every
    wording, which is why it leads."""

    meeting_id_input: tuple[str, ...] = (
        "input[data-tid='meeting-id-input']",
        "input[placeholder*='meeting id' i]",
        "input[aria-label*='meeting id' i]",
    )
    passcode_input: tuple[str, ...] = (
        "input[data-tid='meeting-passcode-input']",
        "input[placeholder*='passcode' i]",
        "input[aria-label*='passcode' i]",
    )
    name_input: tuple[str, ...] = (
        "input[data-tid='prejoin-display-name-input']",
        "input[placeholder*='type your name' i]",
        "input[placeholder*='enter name' i]",
        "input[aria-label*='your name' i]",
        "input[name='displayName']",
    )
    """The pre-join name field, which exists **only for an anonymous guest**.

    A signed-in profile joins under its account name and Teams renders no field at all — so
    an absent match here is the normal case for a configured profile, and not something to
    diagnose."""

    join_button: tuple[str, ...] = (
        # **Every selector here excludes the disabled state, and a live run measured what it
        # costs not to.** Teams renders "Join now" before its calling stack is ready, as a
        # plain ``<button disabled>``. Playwright judges a disabled button *visible*, so the
        # click is attempted and waits its full five-second actionability timeout for an
        # element that will not become enabled on that attempt — three selectors, fifteen
        # seconds a poll, spent learning nothing. The log showed exactly that: click failures
        # at 14:30:24, :29 and :35 before the join finally landed at :37.
        #
        # Excluding it makes the miss instant instead, so the loop keeps polling cheaply until
        # Teams enables the control. The same correction ``ZoomWebSelectors.join_audio_button``
        # carries, for the same reason — Zoom spells it ``aria-disabled``, Teams uses the real
        # attribute.
        "button[data-tid='prejoin-join-button']:not([disabled])",
        "[data-tid='prejoin-join-button']:not([disabled])",
        "button[data-tid='joinMeeting']:not([disabled])",
        "button[aria-label*='join now' i]:not([disabled]):not([aria-disabled='true'])",
        "button:has-text('Join now'):not([disabled])",
    )
    unmute_button: tuple[str, ...] = (
        # Teams labels the control by what a click *does*, so "Unmute" showing means we are
        # muted now. **Never match "Mute" here**: that would silence the avatar.
        #
        # Matched case-insensitively and by substring because the label carries suffixes and
        # keyboard hints in some builds ("Unmute (Ctrl+Shift+M)"), and an exact match that
        # misses leaves the avatar muted — silent, while reporting a healthy join.
        "button[data-tid='toggle-mute'][aria-label*='unmute' i]",
        "button[aria-label*='unmute' i]",
        "[role='button'][aria-label*='unmute' i]",
        "button[title*='unmute' i]",
        "button:has-text('Unmute')",
    )
    leave_button: tuple[str, ...] = (
        "button[data-tid='hangup-main-btn']",
        "button[data-tid='hangup-button']",
        "button[id='hangup-button']",
        "button[aria-label*='leave' i]",
        "button:has-text('Leave')",
    )
    leave_confirm_button: tuple[str, ...] = field(
        default=(
            # Teams' Leave is a split button in some builds: the main half leaves immediately,
            # the chevron opens "Leave" / "End meeting". Where the menu appears, clicking the
            # main half alone dismisses nothing and the avatar stays put.
            #
            # **"End meeting" is deliberately absent from this list.** It would end the call
            # for everybody, which is not a thing the avatar may do to somebody else's
            # meeting even by accident.
            "button[data-tid='hangup-leave-button']",
            "[role='menuitem'][data-tid='hangup-leave']",
            "button:has-text('Leave meeting')",
            "[role='menuitem']:has-text('Leave')",
        )
    )
    in_meeting_markers: tuple[str, ...] = field(
        default=(
            "button[data-tid='hangup-main-btn']",
            "button[data-tid='hangup-button']",
            "[data-tid='calling-toolbar']",
            "button[aria-label*='leave' i]",
        )
    )
    """What proves we are in the call rather than looking at a pre-join screen.

    The hang-up control, because it exists **only** once the call is live — where a roster
    button or a chat button can be rendered on the pre-join screen too."""

    lobby_text: tuple[str, ...] = field(
        default=(
            "someone in the meeting should let you in soon",
            "when the meeting starts, we'll let people know you're waiting",
            "waiting for someone to let you in",
            "you're in the lobby",
        )
    )
    """Teams' lobby wording. Logged once, never treated as a failure — a guest waiting for
    admission is a slow join, and the timeout is set long enough to cover a real one."""

    denied_text: tuple[str, ...] = field(
        default=(
            "you've been removed from this meeting",
            "someone in the meeting denied your request to join",
            "sorry, but you weren't admitted",
            "this meeting has ended",
            "the meeting has ended",
        )
    )
    """Fatal, and the reason it is fatal is worth stating: an organiser who denied entry will
    deny it again, and rejoining a meeting we were removed from repeatedly is the behaviour
    that gets an account blocked."""

    passcode_error_text: tuple[str, ...] = field(
        default=(
            "that passcode didn't work",
            "the passcode is incorrect",
            "invalid meeting id",
            "we couldn't find that meeting",
        )
    )


DEFAULT_SELECTORS = TeamsWebSelectors()


@dataclass(frozen=True, slots=True)
class JoinOutcome:
    """What the join achieved."""

    in_meeting: bool
    unmuted: bool = True
    """False when the avatar is in the meeting but could not be unmuted.

    Not an error: the session is otherwise fine and an organiser can unmute it. It is
    surfaced because a muted avatar is inaudible while every other signal says the join
    succeeded — which is the most expensive failure this connector has."""

    lobby: bool = False
    """Whether the avatar was held in the lobby on the way in. Carried because it explains a
    slow join to whoever reads the log, and because a meeting that lobbies guests will lobby
    the next rejoin too."""


class TeamsWebJoiner:
    """Drives the Teams web client from a link or a meeting id to an avatar that can be heard."""

    __slots__ = (
        "_driver",
        "_force_web_client",
        "_join_url_template",
        "_live_url_template",
        "_poll_interval_s",
        "_selectors",
        "_timeout_s",
    )

    def __init__(
        self,
        *,
        driver: BrowserDriver,
        selectors: TeamsWebSelectors = DEFAULT_SELECTORS,
        join_url_template: str = "https://teams.microsoft.com/v2/?meetingjoin=true",
        live_url_template: str = "https://teams.live.com/meet/{meeting_id}",
        force_web_client: bool = True,
        timeout_s: float = 120.0,
        poll_interval_s: float = 2.0,
    ) -> None:
        self._driver = driver
        self._selectors = selectors
        self._join_url_template = join_url_template
        self._live_url_template = live_url_template
        self._force_web_client = force_web_client
        self._timeout_s = timeout_s
        self._poll_interval_s = poll_interval_s

    def join_target(self, *, meeting_number: str, meeting_url: str | None) -> str:
        """Where to navigate first. Resolves the routes in one place.

        A link wins over an id when both are present: it identifies the meeting exactly,
        passcode included for the short forms, where an id has to be looked up by Teams.

        Raises:
            TeamsWebJoinTargetError: neither a link nor a usable meeting id is present.
                Raised **before the browser goes anywhere**, so the failure names the missing
                input rather than surfacing as a join timeout two minutes later.
        """
        url = (meeting_url or "").strip()
        number = (meeting_number or "").strip()

        # A join URL supplied in the meeting-number field is a natural operator mistake —
        # ``POST /sessions`` has a ``meeting_number`` on it for every platform, and a Teams
        # invite gives you a link. Accept it rather than rejecting a request that carries
        # everything needed.
        if not url and looks_like_join_url(number):
            url, number = number, ""

        if url:
            return url
        if normalise_meeting_id(number).isdigit() and normalise_meeting_id(number):
            return self._join_url_template
        raise TeamsWebJoinTargetError(
            "cannot resolve a Teams join: supply either a join link (a "
            "/l/meetup-join/ URL, or a teams.live.com/meet/<id>?p=<passcode> short link) "
            "in meeting_url or meeting_number, or a numeric meeting id in meeting_number "
            f"with its passcode; got meeting_number={meeting_number!r} "
            f"meeting_url={meeting_url!r}"
        )

    def alternate_target(self, *, meeting_number: str, passcode: str | None) -> str | None:
        """The *other* place a bare meeting id might be joinable, or ``None``.

        **This exists because a meeting id does not say which Teams it belongs to.** A
        work/school id is entered into a form at ``join_url_template``; a personal ("Teams for
        Life") id is a path segment at ``teams.live.com/meet/<id>``. The two look identical —
        both are 9-13 digits — and guessing from the shape of the id or the passcode is the kind
        of heuristic that works until it silently does not.

        So the joiner tries the configured form first and, if it makes no progress at all
        (§``ROUTE_FALLBACK_POLLS``), navigates here instead. That is a fact about the page
        rather than an inference about the string, which is the distinction worth paying a
        re-navigation for.

        A live run is why: a ``teams.live.com`` meeting driven through the work/school form
        landed on the Teams app home for a signed-in personal account — no form, no pre-join,
        nothing to fill, and a timeout with no selector at fault.
        """
        meeting_id = normalise_meeting_id(meeting_number or "")
        if not meeting_id.isdigit() or not self._live_url_template:
            return None
        target = self._live_url_template.format(meeting_id=meeting_id)
        # The short form carries the passcode in the query rather than in a field, which is
        # also why the passcode input being absent on that page is expected rather than a miss.
        if passcode:
            joiner = "&" if "?" in target else "?"
            target = f"{target}{joiner}p={quote(passcode, safe='')}"
        return target

    async def join(
        self,
        *,
        meeting_number: str,
        passcode: str | None,
        display_name: str,
        meeting_url: str | None = None,
    ) -> JoinOutcome:
        """Join the meeting and make sure the avatar is not muted.

        Raises:
            TeamsWebJoinTargetError: the request does not say which meeting to join. Fatal.
            TeamsWebAdmissionError: Teams refused us. Fatal.
            TeamsWebJoinTimeoutError: the sequence did not complete. Recoverable.
        """
        target = self.join_target(meeting_number=meeting_number, meeting_url=meeting_url)
        meeting_id = normalise_meeting_id(meeting_number)
        by_id = target == self._join_url_template
        # Only the id route has a second place to look — a link already says where it goes.
        alternate = (
            self.alternate_target(meeting_number=meeting_number, passcode=passcode)
            if by_id
            else None
        )

        await self._driver.goto(target, timeout_s=self._timeout_s)
        logger.info(
            "teams_web.join_navigated",
            route="meeting_id" if by_id else "join_url",
            meeting_number=meeting_number if by_id else None,
        )

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._timeout_s
        in_meeting = False
        lobby_logged = False
        web_client_logged = False
        # Polls in which nothing on the page responded to anything we did. The counter is what
        # distinguishes "waiting for a slow page" from "this is the wrong page entirely".
        idle_polls = 0

        while loop.time() < deadline:
            await self._raise_if_refused()

            in_meeting = await self._is_in_meeting()
            if in_meeting:
                logger.info("teams_web.in_meeting")
                unmuted = await self._ensure_unmuted()
                return JoinOutcome(
                    in_meeting=True, unmuted=unmuted, lobby=lobby_logged
                )

            # **Every action below is idempotent and every one is attempted on every cycle.**
            # Refilling a filled field is harmless and the fields vanish once submitted, so
            # the loop needs no state machine — which is the whole reason it survives Teams
            # inserting or removing a step.
            touched: list[str | None] = []

            if self._force_web_client:
                clicked = await self._driver.click_first(self._selectors.web_client_button)
                touched.append(clicked)
                if clicked is not None and not web_client_logged:
                    web_client_logged = True
                    logger.info("teams_web.continued_in_browser", selector=clicked)

            if by_id:
                touched.append(
                    await self._driver.fill_first(
                        self._selectors.meeting_id_input, meeting_id
                    )
                )
                if passcode:
                    touched.append(
                        await self._driver.fill_first(
                            self._selectors.passcode_input, passcode
                        )
                    )

            # The name field exists only for an anonymous guest; a signed-in profile has none
            # and this is a no-op.
            touched.append(
                await self._driver.fill_first(self._selectors.name_input, display_name)
            )

            # **Before the join button, not after.** Teams carries the pre-join microphone
            # toggle state into the call, and a persistent profile remembers it across
            # sessions — so an avatar that was muted on the way in is muted in the meeting,
            # and unmuting afterwards is a second chance rather than the mechanism.
            await self._ensure_unmuted(attempts=1)

            touched.append(await self._driver.click_first(self._selectors.join_button))

            if not lobby_logged and await self._is_in_lobby():
                lobby_logged = True
                logger.info(
                    "teams_web.waiting_in_lobby",
                    note="an organiser has to admit the avatar; still polling",
                )

            # **The route fallback: a page that responds to nothing is the wrong page.**
            #
            # A meeting id does not say which Teams it belongs to (see ``alternate_target``),
            # and the failure when the guess is wrong is silent — a signed-in personal account
            # sent to the work/school join form lands on the Teams app home, where every
            # selector correctly matches nothing. Waiting the full timeout there teaches
            # nobody anything.
            #
            # Gated on *no progress at all* rather than on a timer, because a lobby is also a
            # page where nothing happens for minutes — and there ``in_meeting`` is what we are
            # waiting for, not a form. A lobby is reached by clicking Join, so it cannot be
            # idle by this definition.
            if any(entry is not None for entry in touched) or lobby_logged:
                idle_polls = 0
            else:
                idle_polls += 1
                if alternate is not None and idle_polls >= ROUTE_FALLBACK_POLLS:
                    logger.info(
                        "teams_web.route_fallback",
                        note="the work/school join form never appeared; trying the "
                        "personal (teams.live.com) short link for the same meeting id",
                        polls=idle_polls,
                    )
                    await self._driver.goto(alternate, timeout_s=self._timeout_s)
                    # Consumed: one re-navigation, not a loop between two pages.
                    alternate, by_id, idle_polls = None, False, 0

            await asyncio.sleep(self._poll_interval_s)

        detail = f"in_meeting={in_meeting}"
        if lobby_logged:
            detail += "; the avatar was in the lobby and was never admitted"
        elif idle_polls:
            # Said out loud, because it is a different diagnosis from a stuck lobby: the page
            # responded to nothing we tried, which points at the wrong page or renamed
            # selectors rather than at an inattentive organiser.
            detail += (
                "; nothing on the page responded to the join sequence — check that the "
                "meeting id belongs to the Teams the join form serves, or re-run with "
                "MC_TEAMS_WEB__HEADLESS=false and look at what loaded"
            )
        raise TeamsWebJoinTimeoutError(
            f"did not join the Teams meeting within {self._timeout_s:.0f}s ({detail})"
        )

    async def leave(self) -> bool:
        """Leave the meeting, and confirm we actually left.

        **Closing the browser is not leaving.** It drops the socket, but Teams keeps the
        participant until its own timeout expires — so the avatar's tile stays visible to
        everyone else long after the session is gone. The observable symptom is a stop that
        reports success while the avatar is plainly still in the meeting.

        So this clicks the hang-up control, clicks any confirmation it opens, and then
        **waits for the in-meeting controls to disappear** before returning. Only that last
        step makes "the avatar left" true rather than assumed.

        Returns whether departure was confirmed. Never raises: teardown must not add a second
        exception over whatever brought us here.
        """
        try:
            clicked = await self._driver.click_first(self._selectors.leave_button)
            if clicked is None:
                # **Not a missing button — possibly an unclickable one.** Teams' calling
                # toolbar is an overlay that Playwright can find, judge visible, and still
                # refuse to click when another layer covers it. Dispatching the click inside
                # the page skips the actionability checks that are wrong in that case.
                logger.info("teams_web.leave_click_via_script")
                if not await self._click_in_page(self._selectors.leave_button):
                    logger.info("teams_web.leave_control_not_found")
                    return False

            # A split hang-up button opens a menu; the meeting is only left on the confirm.
            # Retried because the menu animates in, and attempted in-page too for the reason
            # above. Absent in the builds where the main button leaves directly, which is why
            # a miss here is not treated as a failure.
            for _ in range(3):
                await asyncio.sleep(0.4)
                if await self._driver.click_first(self._selectors.leave_confirm_button):
                    break
                if await self._click_in_page(self._selectors.leave_confirm_button):
                    break

            for _ in range(10):
                still_there = await self._driver.wait_for_any(
                    self._selectors.in_meeting_markers, timeout_s=0.5
                )
                if still_there is None:
                    logger.info("teams_web.left_meeting")
                    return True
                await asyncio.sleep(0.5)

            logger.warning(
                "teams_web.leave_unconfirmed",
                note="clicked leave but the meeting controls are still present; the browser "
                "is closed anyway, so the participant may linger until Teams times it out",
            )
            return False
        except Exception as exc:
            logger.info("teams_web.leave_click_failed", error=str(exc))
            return False

    async def _click_in_page(self, selectors: tuple[str, ...]) -> bool:
        """Click the first matching element from inside the page.

        Used only where Playwright's actionability checks are wrong rather than protective.
        Deliberately narrow: it skips the visibility and stability guarantees a normal click
        gives, so it is a fallback for a known-bad case, not the way this connector clicks
        things. ``:has-text`` is Playwright-only syntax and is skipped here, since
        ``querySelector`` cannot parse it.
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
            logger.info("teams_web.in_page_click_failed", error=str(exc))
            return False

    async def _ensure_unmuted(self, attempts: int = 8) -> bool:
        """Clear the mute state, retrying until the control disappears.

        **A single click is not enough, and that is not a timing nicety.** Teams renders its
        toolbar after the call is established, so the first attempt can run against a page
        with no mute control yet; and a click that lands on a stale node silently does
        nothing. Either way the avatar stays muted — which is the worst failure this
        connector has, because every other signal says the session is fine.

        Success is defined by the control being *gone*, not by the click returning: "Unmute"
        absent means we are unmuted, since Teams names the button after what pressing it
        would do.

        Called with ``attempts=1`` from inside the join loop, where the loop itself provides
        the retries, and with the default after admission, where it is the last chance.
        """
        for attempt in range(max(attempts, 1)):
            still_muted = await self._driver.wait_for_any(
                self._selectors.unmute_button, timeout_s=0.5
            )
            if still_muted is None:
                if attempt:
                    logger.info("teams_web.unmuted", attempts=attempt + 1)
                return True
            await self._driver.click_first(self._selectors.unmute_button)
            if attempts > 1:
                await asyncio.sleep(0.5)

        if attempts > 1:
            logger.warning(
                "teams_web.still_muted",
                note="the avatar is in the meeting but muted, so nothing it says will be "
                "heard; the unmute control did not clear",
            )
        return False

    # -- page state --------------------------------------------------------

    async def _is_in_meeting(self) -> bool:
        """A short timeout on purpose: the outer loop owns waiting, not this call."""
        found = await self._driver.wait_for_any(
            self._selectors.in_meeting_markers, timeout_s=0.5
        )
        return found is not None

    async def _is_in_lobby(self) -> bool:
        text = await self._safe_text()
        return any(marker in text for marker in self._selectors.lobby_text)

    async def _raise_if_refused(self) -> None:
        text = await self._safe_text()
        for marker in self._selectors.denied_text:
            if marker in text:
                raise TeamsWebAdmissionError(marker)
        for marker in self._selectors.passcode_error_text:
            if marker in text:
                raise TeamsWebAdmissionError("meeting id or passcode rejected")

    async def _safe_text(self) -> str:
        try:
            return (await self._driver.page_text()).lower()
        except Exception:
            return ""
