"""``TeamsWebJoiner`` — the two routes in, the lobby, and the mute trap.

Driven against ``FakeBrowserDriver``, which is the second implementation that justifies the
``BrowserDriver`` seam: the whole join flow runs here with no Chromium, no Teams tenant, and no
meeting.

The properties worth pinning are the ones whose failure looks like success:

* **A muted avatar reports a healthy join.** Every other signal says the session is fine and
  nothing it says is audible, which makes it the most expensive failure this connector has.
* **A lobby is not an error.** Failing early on a guest waiting for admission turns a slow
  organiser into a broken session.
* **Leaving has to be confirmed.** Closing the browser drops the socket, and Teams keeps the
  participant until its own timeout — so the avatar's tile stays visible long after
  ``DELETE /sessions/{id}`` returned success.
"""

from __future__ import annotations

import pytest

from src.connectors.teams_web.exceptions import (
    TeamsWebAdmissionError,
    TeamsWebJoinTargetError,
    TeamsWebJoinTimeoutError,
)
from src.connectors.teams_web.meeting.join import (
    DEFAULT_SELECTORS,
    TeamsWebJoiner,
    looks_like_join_url,
    normalise_meeting_id,
)
from tests.fakes.meet_page import FakeBrowserDriver

JOIN_URL = (
    "https://teams.microsoft.com/l/meetup-join/19%3ameeting_ABC123%40thread.v2/0"
    "?context=%7b%22Tid%22%3a%22t%22%2c%22Oid%22%3a%22o%22%7d"
)
LANDING = "https://teams.microsoft.com/v2/?meetingjoin=true"
LIVE_URL = "https://teams.live.com/meet/9350242031207?p=JFKVvmQwvhYorxAh3K"
"""A real personal-account link shape, from the live run that found the gap this covers."""

IN_MEETING = "button[data-tid='hangup-main-btn']"
JOIN_BUTTON = "button[data-tid='prejoin-join-button']:not([disabled])"
"""Carries ``:not([disabled])`` because Teams renders "Join now" disabled first, and
matching it anyway burns Playwright's full click timeout per selector per poll."""
NAME_INPUT = "input[data-tid='prejoin-display-name-input']"
ID_INPUT = "input[data-tid='meeting-id-input']"
PASSCODE_INPUT = "input[data-tid='meeting-passcode-input']"
UNMUTE = "button[aria-label*='unmute' i]"
WEB_CLIENT = "[data-tid='joinOnWeb']"
LEAVE = "button[data-tid='hangup-main-btn']"


def _joiner(driver: FakeBrowserDriver, **kwargs: object) -> TeamsWebJoiner:
    return TeamsWebJoiner(
        driver=driver,
        join_url_template=LANDING,
        live_url_template=kwargs.pop(  # type: ignore[arg-type]
            "live_url_template", "https://teams.live.com/meet/{meeting_id}"
        ),
        timeout_s=kwargs.pop("timeout_s", 5.0),  # type: ignore[arg-type]
        poll_interval_s=kwargs.pop("poll_interval_s", 0.01),  # type: ignore[arg-type]
        **kwargs,  # type: ignore[arg-type]
    )


class TestJoinTarget:
    def test_a_link_is_navigated_to_directly(self) -> None:
        joiner = _joiner(FakeBrowserDriver())
        assert joiner.join_target(meeting_number="", meeting_url=JOIN_URL) == JOIN_URL

    def test_a_meeting_id_goes_to_the_web_client_s_join_form(self) -> None:
        joiner = _joiner(FakeBrowserDriver())
        assert joiner.join_target(meeting_number="281442953617", meeting_url=None) == LANDING

    def test_a_link_pasted_into_the_meeting_number_field_is_accepted(self) -> None:
        """A natural operator mistake: ``POST /sessions`` has a ``meeting_number`` for every
        platform, and a Teams invite gives you a link. Rejecting a request that carries
        everything needed would be pedantry."""
        joiner = _joiner(FakeBrowserDriver())
        assert joiner.join_target(meeting_number=JOIN_URL, meeting_url=None) == JOIN_URL

    def test_a_link_wins_over_an_id_when_both_are_present(self) -> None:
        """It carries the tenant and the thread, so it identifies the meeting exactly — where
        an id has to be looked up and can be ambiguous across tenants."""
        joiner = _joiner(FakeBrowserDriver())
        target = joiner.join_target(meeting_number="281442953617", meeting_url=JOIN_URL)
        assert target == JOIN_URL

    def test_a_spaced_id_is_normalised(self) -> None:
        """Teams prints the id in groups and an operator pastes what is printed."""
        assert normalise_meeting_id("281 442 953 617") == "281442953617"

    def test_neither_route_fails_before_the_browser_goes_anywhere(self) -> None:
        """Named at the input rather than surfacing as a join timeout two minutes later."""
        driver = FakeBrowserDriver()
        joiner = _joiner(driver)
        with pytest.raises(TeamsWebJoinTargetError) as excinfo:
            joiner.join_target(meeting_number="not-a-meeting", meeting_url=None)
        assert "meetup-join" in str(excinfo.value)
        assert driver.visited == []

    def test_a_join_url_is_recognised_by_its_path_segment(self) -> None:
        assert looks_like_join_url(JOIN_URL)
        assert not looks_like_join_url("281442953617")

    def test_a_personal_teams_live_link_is_a_join_link(self) -> None:
        """**The bug a live run found.** ``teams.live.com/meet/<id>?p=…`` is what a personal
        ("Teams for Life") account's "Copy link" produces, and matching only ``meetup-join``
        sent it down the meeting-id route — which navigated the work/school join form and, for
        a signed-in personal account, landed on the Teams app home. No form, nothing to fill,
        and a timeout with no selector at fault."""
        assert looks_like_join_url(LIVE_URL)
        joiner = _joiner(FakeBrowserDriver())
        assert joiner.join_target(meeting_number=LIVE_URL, meeting_url=None) == LIVE_URL

    def test_the_work_school_short_link_is_also_a_join_link(self) -> None:
        """Microsoft ships the same short form on ``teams.microsoft.com``."""
        short = "https://teams.microsoft.com/meet/9350242031207?p=aBc123"
        assert looks_like_join_url(short)

    def test_a_bare_id_can_never_match_the_loose_marker(self) -> None:
        """``/meet/`` is deliberately loose, and requiring an absolute URL is what keeps it
        safe — otherwise a stray path fragment would be taken for a join link."""
        for value in ("281442953617", "/meet/281442953617", "meet/281442953617", ""):
            assert not looks_like_join_url(value)


class TestPersonalMeetingFallback:
    """A meeting id does not say which Teams it belongs to, so the joiner finds out.

    Both are 9-13 digits: a work/school id goes into a form, a personal one is a path segment
    at ``teams.live.com/meet/<id>``. Guessing from the shape of the id or the passcode is the
    kind of heuristic that works until it silently does not — so the joiner tries the form and
    falls back on the *page's* behaviour rather than on the string's.
    """

    def test_the_alternate_target_carries_the_passcode_in_the_query(self) -> None:
        """The short form has no passcode field, which is also why an absent passcode input on
        that page is expected rather than a missed selector."""
        joiner = _joiner(FakeBrowserDriver())
        target = joiner.alternate_target(
            meeting_number="9350242031207", passcode="JFKVvmQwvhYorxAh3K"
        )
        assert target == "https://teams.live.com/meet/9350242031207?p=JFKVvmQwvhYorxAh3K"

    def test_a_passcode_with_url_characters_is_escaped(self) -> None:
        joiner = _joiner(FakeBrowserDriver())
        target = joiner.alternate_target(meeting_number="123456789", passcode="a&b=c d")
        assert target is not None
        assert target.endswith("?p=a%26b%3Dc%20d")

    def test_no_passcode_leaves_the_url_bare(self) -> None:
        joiner = _joiner(FakeBrowserDriver())
        assert joiner.alternate_target(meeting_number="123456789", passcode=None) == (
            "https://teams.live.com/meet/123456789"
        )

    def test_there_is_no_alternate_for_a_non_numeric_id(self) -> None:
        joiner = _joiner(FakeBrowserDriver())
        assert joiner.alternate_target(meeting_number=LIVE_URL, passcode=None) is None

    def test_an_empty_template_switches_the_fallback_off(self) -> None:
        """Worth having for a deployment that only joins work/school meetings and would rather
        see a clean timeout than a second navigation."""
        joiner = _joiner(FakeBrowserDriver(), live_url_template="")
        assert joiner.alternate_target(meeting_number="123456789", passcode=None) is None

    @pytest.mark.asyncio
    async def test_a_page_that_responds_to_nothing_triggers_the_re_navigation(self) -> None:
        """**The exact live failure.** The work/school form never appeared — no selector
        matched anything — so after ``ROUTE_FALLBACK_POLLS`` idle polls the joiner navigates
        the personal short link for the same id instead."""
        driver = FakeBrowserDriver(visible=set())

        # Generous, because each poll cycle spends real time in the driver's own
        # wait-for-selector deadlines: four *idle* polls is a few seconds, not a few
        # milliseconds. In production that is a few seconds out of a 120 s budget.
        joiner = _joiner(driver, timeout_s=8.0)
        with pytest.raises(TeamsWebJoinTimeoutError):
            await joiner.join(
                meeting_number="9350242031207",
                passcode="JFKVvmQwvhYorxAh3K",
                display_name="AI Avatar",
            )

        assert driver.visited[0] == LANDING
        assert LIVE_URL in driver.visited, "the personal short link was never tried"
        # Once, not a loop between the two pages.
        assert driver.visited.count(LIVE_URL) == 1

    @pytest.mark.asyncio
    async def test_the_fallback_can_complete_the_join(self) -> None:
        driver = FakeBrowserDriver(visible=set())

        async def goto(url: str, *, timeout_s: float) -> None:
            driver.visited.append(url)
            if url == LIVE_URL:
                driver.show(IN_MEETING)

        driver.goto = goto  # type: ignore[method-assign]

        outcome = await _joiner(driver, timeout_s=8.0).join(
            meeting_number="9350242031207",
            passcode="JFKVvmQwvhYorxAh3K",
            display_name="AI Avatar",
        )
        assert outcome.in_meeting

    @pytest.mark.asyncio
    async def test_a_page_making_progress_is_never_re_navigated(self) -> None:
        """A form that is being filled is the right page, however slowly it resolves."""
        driver = FakeBrowserDriver(visible={ID_INPUT, PASSCODE_INPUT, NAME_INPUT})
        with pytest.raises(TeamsWebJoinTimeoutError):
            await _joiner(driver, timeout_s=1.0).join(
                meeting_number="9350242031207", passcode="x", display_name="AI Avatar"
            )
        assert driver.visited == [LANDING]

    @pytest.mark.asyncio
    async def test_a_lobby_is_never_mistaken_for_the_wrong_page(self) -> None:
        """A lobby is also a page where nothing responds for minutes. It is reached by
        clicking Join, so it cannot be idle by this definition — and re-navigating out of a
        lobby would throw away an admission that was about to happen."""
        driver = FakeBrowserDriver(
            visible={JOIN_BUTTON}, text="you're in the lobby"
        )
        with pytest.raises(TeamsWebJoinTimeoutError):
            await _joiner(driver, timeout_s=1.0).join(
                meeting_number="9350242031207", passcode="x", display_name="AI Avatar"
            )
        assert driver.visited == [LANDING]

    @pytest.mark.asyncio
    async def test_a_link_route_is_never_re_navigated(self) -> None:
        """A link already says where it goes; there is no second place to look."""
        driver = FakeBrowserDriver(visible=set())
        with pytest.raises(TeamsWebJoinTimeoutError):
            await _joiner(driver, timeout_s=1.0).join(
                meeting_number="", passcode=None, display_name="AI Avatar",
                meeting_url=JOIN_URL,
            )
        assert driver.visited == [JOIN_URL]

    @pytest.mark.asyncio
    async def test_the_timeout_says_the_page_responded_to_nothing(self) -> None:
        """A different diagnosis from a stuck lobby, and the message has to say which."""
        driver = FakeBrowserDriver(visible=set())
        with pytest.raises(TeamsWebJoinTimeoutError) as excinfo:
            await _joiner(driver, timeout_s=1.0, live_url_template="").join(
                meeting_number="9350242031207", passcode="x", display_name="AI Avatar"
            )
        assert "responded to nothing" in str(excinfo.value) or (
            "responded to the join sequence" in str(excinfo.value)
        )


class TestJoiningByLink:
    @pytest.mark.asyncio
    async def test_the_launcher_is_clicked_past_and_the_name_is_typed(self) -> None:
        driver = FakeBrowserDriver(visible={WEB_CLIENT, NAME_INPUT, JOIN_BUTTON})

        def on_click(d: FakeBrowserDriver, selector: str) -> None:
            if selector == JOIN_BUTTON:
                d.hide(WEB_CLIENT, NAME_INPUT, JOIN_BUTTON)
                d.show(IN_MEETING)

        driver._on_click = on_click

        outcome = await _joiner(driver).join(
            meeting_number="", passcode=None, display_name="AI Avatar", meeting_url=JOIN_URL
        )

        assert outcome.in_meeting
        assert driver.visited == [JOIN_URL]
        assert WEB_CLIENT in driver.clicked
        assert (NAME_INPUT, "AI Avatar") in driver.filled

    @pytest.mark.asyncio
    async def test_the_launcher_click_can_be_switched_off(self) -> None:
        driver = FakeBrowserDriver(visible={WEB_CLIENT, IN_MEETING})
        await _joiner(driver, force_web_client=False).join(
            meeting_number="", passcode=None, display_name="AI Avatar", meeting_url=JOIN_URL
        )
        assert WEB_CLIENT not in driver.clicked


class TestJoiningByMeetingId:
    @pytest.mark.asyncio
    async def test_the_id_and_passcode_are_filled_into_the_join_form(self) -> None:
        driver = FakeBrowserDriver(
            visible={ID_INPUT, PASSCODE_INPUT, NAME_INPUT, JOIN_BUTTON}
        )

        def on_click(d: FakeBrowserDriver, selector: str) -> None:
            if selector == JOIN_BUTTON:
                d.hide(ID_INPUT, PASSCODE_INPUT, NAME_INPUT, JOIN_BUTTON)
                d.show(IN_MEETING)

        driver._on_click = on_click

        outcome = await _joiner(driver).join(
            meeting_number="281 442 953 617", passcode="abc123", display_name="AI Avatar"
        )

        assert outcome.in_meeting
        assert driver.visited == [LANDING]
        # Normalised: the form wants the digits, not the presentation spacing.
        assert (ID_INPUT, "281442953617") in driver.filled
        assert (PASSCODE_INPUT, "abc123") in driver.filled

    @pytest.mark.asyncio
    async def test_no_passcode_is_typed_when_the_meeting_has_none(self) -> None:
        driver = FakeBrowserDriver(visible={ID_INPUT, PASSCODE_INPUT, IN_MEETING})
        await _joiner(driver).join(
            meeting_number="281442953617", passcode=None, display_name="AI Avatar"
        )
        assert all(selector != PASSCODE_INPUT for selector, _ in driver.filled)


class TestMuteState:
    @pytest.mark.asyncio
    async def test_the_avatar_is_unmuted_before_and_after_the_join(self) -> None:
        """**Before matters as much as after.** Teams carries the pre-join microphone toggle
        into the call and a persistent profile remembers it across sessions, so an avatar muted
        on the way in is muted in the meeting."""
        driver = FakeBrowserDriver(visible={IN_MEETING, UNMUTE})

        def on_click(d: FakeBrowserDriver, selector: str) -> None:
            if selector == UNMUTE:
                d.hide(UNMUTE)

        driver._on_click = on_click

        outcome = await _joiner(driver).join(
            meeting_number="281442953617", passcode=None, display_name="AI Avatar"
        )
        assert outcome.unmuted
        assert UNMUTE in driver.clicked

    @pytest.mark.asyncio
    async def test_a_join_that_stays_muted_is_reported_rather_than_failed(self) -> None:
        """The session is otherwise fine and an organiser can unmute it. Reported because
        every other signal says the join succeeded."""
        driver = FakeBrowserDriver(visible={IN_MEETING, UNMUTE})
        outcome = await _joiner(driver).join(
            meeting_number="281442953617", passcode=None, display_name="AI Avatar"
        )
        assert outcome.in_meeting
        assert outcome.unmuted is False

    @pytest.mark.asyncio
    async def test_mute_is_never_clicked(self) -> None:
        """The control is labelled by what a click *does*, so matching "Mute" would silence
        the avatar. Guarded here because the selector list is the only thing standing between
        those two outcomes."""
        assert all(
            "unmute" in selector.lower() for selector in DEFAULT_SELECTORS.unmute_button
        )


class TestLobbyAndRefusal:
    @pytest.mark.asyncio
    async def test_the_lobby_is_waited_out_and_reported(self) -> None:
        driver = FakeBrowserDriver(
            visible={JOIN_BUTTON},
            text="Someone in the meeting should let you in soon",
        )
        polls = {"n": 0}

        def on_click(d: FakeBrowserDriver, selector: str) -> None:
            polls["n"] += 1
            if polls["n"] >= 3:
                d.hide(JOIN_BUTTON)
                d.show(IN_MEETING)

        driver._on_click = on_click

        outcome = await _joiner(driver).join(
            meeting_number="281442953617", passcode=None, display_name="AI Avatar"
        )
        assert outcome.in_meeting
        assert outcome.lobby is True

    @pytest.mark.asyncio
    async def test_a_denied_request_is_fatal(self) -> None:
        """An organiser who denied entry will deny it again, and rejoining a meeting we were
        removed from repeatedly is what gets an account blocked."""
        driver = FakeBrowserDriver(
            text="Someone in the meeting denied your request to join"
        )
        with pytest.raises(TeamsWebAdmissionError):
            await _joiner(driver).join(
                meeting_number="281442953617", passcode=None, display_name="AI Avatar"
            )

    @pytest.mark.asyncio
    async def test_a_rejected_passcode_is_fatal(self) -> None:
        driver = FakeBrowserDriver(text="That passcode didn't work")
        with pytest.raises(TeamsWebAdmissionError) as excinfo:
            await _joiner(driver).join(
                meeting_number="281442953617", passcode="wrong", display_name="AI Avatar"
            )
        assert "passcode" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_never_getting_in_is_recoverable_and_says_why(self) -> None:
        driver = FakeBrowserDriver(
            visible={JOIN_BUTTON}, text="you're in the lobby"
        )
        with pytest.raises(TeamsWebJoinTimeoutError) as excinfo:
            await _joiner(driver, timeout_s=0.05).join(
                meeting_number="281442953617", passcode=None, display_name="AI Avatar"
            )
        assert "lobby" in str(excinfo.value)


class TestLeaving:
    @pytest.mark.asyncio
    async def test_leaving_is_confirmed_by_the_controls_disappearing(self) -> None:
        """**Closing the browser is not leaving.** Only the controls being gone makes "the
        avatar left" true rather than assumed."""
        driver = FakeBrowserDriver(visible={LEAVE})

        def on_click(d: FakeBrowserDriver, selector: str) -> None:
            if selector == LEAVE:
                d.hide(LEAVE)

        driver._on_click = on_click

        assert await _joiner(driver).leave() is True

    @pytest.mark.asyncio
    async def test_an_unconfirmed_leave_is_reported_rather_than_raised(self) -> None:
        """Teardown must not add a second exception over whatever brought us here — and the
        browser is closed regardless, which is what actually removes the participant."""
        driver = FakeBrowserDriver(visible={LEAVE})
        assert await _joiner(driver).leave() is False

    @pytest.mark.asyncio
    async def test_a_missing_control_falls_back_to_an_in_page_click(self) -> None:
        """Teams' calling toolbar is an overlay Playwright can find, judge visible, and still
        refuse to click when another layer covers it."""
        driver = FakeBrowserDriver(visible=set(), script_result=True)
        assert await _joiner(driver).leave() is True

    @pytest.mark.asyncio
    async def test_ending_the_meeting_for_everybody_is_never_a_confirm_option(self) -> None:
        """Not a thing the avatar may do to somebody else's meeting, even by accident."""
        for selector in DEFAULT_SELECTORS.leave_confirm_button:
            assert "end meeting" not in selector.lower()


def test_no_join_selector_matches_a_disabled_button() -> None:
    """**Fifteen seconds a poll, measured.**

    Teams renders "Join now" before its calling stack is ready, as a plain ``<button
    disabled>``. Playwright judges a disabled button *visible*, so the click is attempted and
    waits its full five-second actionability timeout for an element that will not become
    enabled on that attempt. A live run burned three of those per poll cycle.
    """
    for selector in DEFAULT_SELECTORS.join_button:
        assert ":not([disabled])" in selector, selector
