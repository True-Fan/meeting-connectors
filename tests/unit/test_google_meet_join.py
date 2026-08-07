"""The join flow, and the asymmetry of its failure modes.

The tests that matter most are the ones about *not* retrying. A denied or ejected join must
be fatal, because an automated Google account that repeatedly asks to enter a meeting it was
thrown out of is indistinguishable from abuse — and the cost of getting that wrong is the
account being restricted, which breaks every session rather than one.

The lobby is the mirror image: it is not a failure at all, and it must not share the join
timeout, because a human host takes minutes to click Admit.
"""

from __future__ import annotations

import pytest

from src.connectors.google_meet.automation.selectors import DEFAULT_SELECTORS
from src.connectors.google_meet.exceptions import (
    BrowserCrashedError,
    GoogleAuthError,
    JoinTimeoutError,
    MeetingAdmissionError,
    MeetingEndedError,
)
from src.connectors.google_meet.meeting.join import MeetJoiner
from src.connectors.google_meet.meeting.meet_url import MeetJoinTarget, canonical_url
from src.connectors.google_meet.reconnect.classify import is_fatal
from src.connectors.google_meet.websocket.protocol import MeetState
from tests.fakes.meet_page import (
    ASK_TO_JOIN_SELECTOR,
    IN_CALL_SELECTOR,
    JOIN_NOW_SELECTOR,
    LOBBY_SELECTOR,
    NO_SESSION_COOKIES,
    FakeBrowserDriver,
)

CODE = "abc-defg-hij"
TARGET = MeetJoinTarget(url=canonical_url(CODE), meeting_code=CODE)


def _joiner(driver: FakeBrowserDriver, **kwargs: float) -> MeetJoiner:
    return MeetJoiner(
        driver=driver,
        selectors=DEFAULT_SELECTORS,
        display_name="AI Avatar",
        join_timeout_s=kwargs.get("join_timeout_s", 1.0),
        lobby_timeout_s=kwargs.get("lobby_timeout_s", 1.0),
    )


class TestDirectJoin:
    async def test_join_now_puts_us_straight_in(self) -> None:
        driver = FakeBrowserDriver(
            visible={JOIN_NOW_SELECTOR},
            on_click=lambda d, s: (d.hide(s), d.show(IN_CALL_SELECTOR)),
        )
        outcome = await _joiner(driver).join(TARGET)

        assert outcome.state is MeetState.JOINED
        assert outcome.waited_in_lobby_s == 0.0
        assert outcome.matched_join_button == JOIN_NOW_SELECTOR
        assert TARGET.url in driver.visited

    async def test_the_google_session_is_verified_before_the_meeting_is_opened(self) -> None:
        """Otherwise an unsigned-in profile presents as an unexplained join timeout."""
        driver = FakeBrowserDriver(
            visible={JOIN_NOW_SELECTOR},
            on_click=lambda d, s: (d.hide(s), d.show(IN_CALL_SELECTOR)),
        )
        await _joiner(driver).join(TARGET)

        assert "myaccount.google.com" in driver.visited[0]
        assert driver.visited.index(TARGET.url) > 0

    async def test_an_already_joined_page_needs_no_button(self) -> None:
        """Happens on a rejoin into a meeting the browser never fully left."""
        driver = FakeBrowserDriver(visible={IN_CALL_SELECTOR})
        outcome = await _joiner(driver).join(TARGET)

        assert outcome.state is MeetState.JOINED
        assert outcome.matched_join_button is None

    async def test_interstitials_are_dismissed_first(self) -> None:
        dismiss = 'button[aria-label="Got it"]'
        driver = FakeBrowserDriver(
            visible={'button[aria-label="Close"]', JOIN_NOW_SELECTOR},
            on_click=lambda d, s: (d.hide(s), d.show(IN_CALL_SELECTOR))
            if s == JOIN_NOW_SELECTOR
            else d.hide(s),
        )
        await _joiner(driver).join(TARGET)
        assert 'button[aria-label="Close"]' in driver.clicked
        assert dismiss not in driver.clicked


class TestLobby:
    async def test_ask_to_join_waits_then_is_admitted(self) -> None:
        driver = FakeBrowserDriver(
            visible={ASK_TO_JOIN_SELECTOR},
            on_click=lambda d, s: (d.hide(s), d.show(LOBBY_SELECTOR)),
        )

        async def admit_soon() -> None:
            import asyncio

            await asyncio.sleep(0.05)
            driver.hide(LOBBY_SELECTOR)
            driver.show(IN_CALL_SELECTOR)

        import asyncio

        task = asyncio.create_task(admit_soon())
        outcome = await _joiner(driver, lobby_timeout_s=5.0).join(TARGET)
        await task

        assert outcome.state is MeetState.JOINED
        assert outcome.waited_in_lobby_s > 0
        assert outcome.matched_join_button == ASK_TO_JOIN_SELECTOR

    async def test_the_lobby_has_its_own_much_longer_budget(self) -> None:
        """A short join timeout must not abandon a meeting that is about to admit us."""
        driver = FakeBrowserDriver(
            visible={ASK_TO_JOIN_SELECTOR},
            on_click=lambda d, s: (d.hide(s), d.show(LOBBY_SELECTOR)),
        )
        joiner = _joiner(driver, join_timeout_s=0.2, lobby_timeout_s=0.6)

        with pytest.raises(JoinTimeoutError, match="nobody admitted the avatar"):
            await joiner.join(TARGET)

    async def test_lobby_is_detected_from_text_when_the_selector_moves(self) -> None:
        driver = FakeBrowserDriver(
            visible={ASK_TO_JOIN_SELECTOR},
            text="Asking to join",
            on_click=lambda d, s: d.hide(s),
        )
        with pytest.raises(JoinTimeoutError, match="nobody admitted"):
            await _joiner(driver, lobby_timeout_s=0.3).join(TARGET)

    async def test_denial_while_in_the_lobby_is_fatal_immediately(self) -> None:
        """It must not wait out the lobby budget before reporting a decision already made."""
        driver = FakeBrowserDriver(
            visible={ASK_TO_JOIN_SELECTOR},
            on_click=lambda d, s: (d.hide(s), d.show(LOBBY_SELECTOR)),
        )

        async def deny_soon() -> None:
            import asyncio

            await asyncio.sleep(0.05)
            driver.text = "Your request to join was denied"

        import asyncio

        task = asyncio.create_task(deny_soon())
        with pytest.raises(MeetingAdmissionError, match="denied the request"):
            await _joiner(driver, lobby_timeout_s=10.0).join(TARGET)
        await task


class TestTerminalOutcomes:
    """Rejoining any of these is wrong. See ``reconnect/classify.py``."""

    async def test_denied_entry(self) -> None:
        driver = FakeBrowserDriver(text="No one responded to your request to join")
        with pytest.raises(MeetingAdmissionError, match="denied the request"):
            await _joiner(driver).join(TARGET)

    async def test_ejected(self) -> None:
        driver = FakeBrowserDriver(visible={IN_CALL_SELECTOR}, text="You've been removed")
        with pytest.raises(MeetingAdmissionError, match="removed from the meeting"):
            await _joiner(driver).join(TARGET)

    async def test_ejection_wins_over_a_stale_leave_button(self) -> None:
        """The leave button lingers for a beat; reading it as JOINED causes a rejoin loop."""
        driver = FakeBrowserDriver(
            visible={IN_CALL_SELECTOR}, text="You were removed from the meeting"
        )
        with pytest.raises(MeetingAdmissionError):
            await _joiner(driver).join(TARGET)

    async def test_meeting_ended(self) -> None:
        driver = FakeBrowserDriver(text="This meeting has ended")
        with pytest.raises(MeetingEndedError, match="has ended"):
            await _joiner(driver).join(TARGET)

    async def test_ended_text_is_ignored_while_genuinely_in_a_call(self) -> None:
        """Stale copy from a previous call must not fail a live one."""
        driver = FakeBrowserDriver(visible={IN_CALL_SELECTOR}, text="You left the meeting")
        outcome = await _joiner(driver).join(TARGET)
        assert outcome.state is MeetState.JOINED

    async def test_a_mid_join_sign_out_is_reported_as_an_admission_failure(self) -> None:
        """At that point Meet, not Google, is the thing refusing us."""
        driver = FakeBrowserDriver(text="Sign in to join this video call")
        with pytest.raises(MeetingAdmissionError, match="session expired mid-join"):
            await _joiner(driver).join(TARGET)


class TestAuthentication:
    async def test_an_unsigned_in_profile_fails_before_navigating_to_the_meeting(self) -> None:
        # Signed out is modelled by the *absence of session cookies*, which is what the
        # detector actually reads. The old spelling of this test set script_result=None,
        # encoding the brittle ARIA-label check that reported authenticated profiles as
        # signed out — see test_google_meet_auth.py.
        driver = FakeBrowserDriver(cookies=NO_SESSION_COOKIES, text="Sign in")
        with pytest.raises(GoogleAuthError, match="not signed in to Google"):
            await _joiner(driver).join(TARGET)
        assert canonical_url(CODE) not in driver.visited

    async def test_the_error_says_how_to_fix_it(self) -> None:
        """A deployment step was skipped; the message should name it."""
        driver = FakeBrowserDriver(cookies=NO_SESSION_COOKIES, text="Sign in")
        with pytest.raises(GoogleAuthError, match=r"MC_GOOGLE_MEET__HEADLESS=false"):
            await _joiner(driver).join(TARGET)

    async def test_a_google_challenge_is_named_rather_than_reported_generically(self) -> None:
        """'Sign-in failed' sends an operator looking for a wrong password."""
        driver = FakeBrowserDriver(text="This browser or app may not be secure")
        with pytest.raises(GoogleAuthError, match="challenging this browser"):
            await _joiner(driver).join(TARGET)

    async def test_a_crashed_browser_is_not_reported_as_an_unsigned_in_profile(self) -> None:
        """Regression: the probe used to absorb any exception, including a dead renderer.

        The two failures have opposite handling — ``GoogleAuthError`` is fatal, a browser
        crash is recoverable — so conflating them turned a tab that died during the probe
        into a permanently failed session that a relaunch would have fixed, while telling the
        operator to re-authenticate a profile that was never the problem.
        """
        driver = FakeBrowserDriver(crash_after_goto=0)

        with pytest.raises(BrowserCrashedError):
            await _joiner(driver).join(TARGET)

    async def test_a_crashed_browser_is_recoverable_where_an_auth_failure_is_not(self) -> None:
        """The classification that makes the distinction above matter."""
        assert is_fatal(GoogleAuthError("no session")) is True
        assert is_fatal(BrowserCrashedError("renderer died")) is False


class TestJoinTimeout:
    async def test_no_join_button_points_at_the_selector_file(self) -> None:
        """The likeliest cause is a Meet UI change, so the message says where to look."""
        driver = FakeBrowserDriver()
        with pytest.raises(JoinTimeoutError, match=r"automation/selectors\.py"):
            await _joiner(driver, join_timeout_s=0.2).join(TARGET)

    async def test_neither_joined_nor_in_a_lobby(self) -> None:
        driver = FakeBrowserDriver(visible={JOIN_NOW_SELECTOR}, on_click=lambda d, s: d.hide(s))
        with pytest.raises(JoinTimeoutError, match="neither joined nor placed in a lobby"):
            await _joiner(driver, join_timeout_s=0.3).join(TARGET)


class TestGuestName:
    async def test_a_name_prompt_is_filled_but_warned_about(self) -> None:
        """The field appearing means the Google session was lost; guests are often refused."""
        name_field = 'input[aria-label="Your name"]'
        driver = FakeBrowserDriver(
            visible={name_field, JOIN_NOW_SELECTOR},
            on_click=lambda d, s: (d.hide(s), d.show(IN_CALL_SELECTOR)),
        )
        await _joiner(driver).join(TARGET)
        assert (name_field, "AI Avatar") in driver.filled
