"""Google session detection.

**Regression cover for a bug that made the connector unusable on a correctly-authenticated
profile.** The original ``verify_signed_in`` had exactly one positive signal: a
``document.querySelector('a[aria-label*="@"], [aria-label*="Google Account"]')`` scrape. Two
things were wrong with that, and they compounded:

* the selector does not match on ``myaccount.google.com`` — the page the function navigated to;
* it navigated *first*, so a caller with a rendered Gmail tab open (where the selector does
  resolve) had it thrown away before anything looked at it.

A profile holding all seven Google session cookies therefore reported
``"no Google account session found"``, every single time, and the join flow turned that into a
fatal ``GoogleAuthError``.

The signals are now, in order of authority: **session cookies** (a Google session *is* its
cookies), **the host already loaded** (only a signed-in browser reaches Gmail), and **where a
probe redirects to** (behaviour, not markup). The ARIA scrape survives only to name the account
in a log line, and these tests pin that it can never again decide the verdict.
"""

from __future__ import annotations

import pytest

from src.connectors.google_meet.auth.google_login import (
    AUTHENTICATED_SURFACES,
    SESSION_COOKIES,
    SIGN_IN_HOST,
    AuthStatus,
    verify_signed_in,
)
from src.connectors.google_meet.exceptions import BrowserError, GoogleAuthError
from tests.fakes.meet_page import (
    GOOGLE_SESSION_COOKIES,
    NO_SESSION_COOKIES,
    FakeBrowserDriver,
)

GMAIL = "https://mail.google.com/mail/u/0/#inbox"
MYACCOUNT = "https://myaccount.google.com/"
ACCOUNTS = "https://accounts.google.com/"
SERVICE_LOGIN = "https://accounts.google.com/v3/signin/identifier?flowName=GlifWebSignIn"


# --------------------------------------------------------------------------- #
# The four cases from the bug report
# --------------------------------------------------------------------------- #


class TestTheReportedCases:
    """Each of these returned "not signed in" before the rewrite."""

    async def test_gmail_already_open_is_signed_in(self) -> None:
        """And **without navigating away from it**.

        The heart of the original bug: the function moved to myaccount.google.com before
        looking, so an open, rendered Gmail tab — proof of a working session — was discarded.
        """
        driver = FakeBrowserDriver(cookies=GOOGLE_SESSION_COOKIES, url=GMAIL)

        status = await verify_signed_in(driver)

        assert status.signed_in is True
        assert driver.visited == [], "must not navigate away from an authenticated page"

    async def test_myaccount_already_open_is_signed_in(self) -> None:
        """The page whose markup the old ARIA selector did not match."""
        driver = FakeBrowserDriver(cookies=GOOGLE_SESSION_COOKIES, url=MYACCOUNT)

        status = await verify_signed_in(driver)

        assert status.signed_in is True
        assert driver.visited == []

    async def test_accounts_google_com_with_a_session_is_signed_in(self) -> None:
        """``accounts.google.com`` is the *sign-in* host, so being on it proves nothing by
        itself — it has to be probed.

        With a live session Google redirects the probe away from the sign-in host, which is the
        answer. This is the case a naive "is the URL accounts.google.com?" check gets wrong, and
        it is why the redirect is read after the probe rather than before it.
        """
        driver = FakeBrowserDriver(
            cookies=GOOGLE_SESSION_COOKIES, url=ACCOUNTS, redirect_to=MYACCOUNT
        )

        status = await verify_signed_in(driver)

        assert status.signed_in is True
        assert driver.visited == [MYACCOUNT], "the sign-in host must be probed, not trusted"

    async def test_a_real_login_page_is_not_signed_in(self) -> None:
        driver = FakeBrowserDriver(
            cookies=NO_SESSION_COOKIES, url=SERVICE_LOGIN, text="Sign in"
        )

        status = await verify_signed_in(driver)

        assert status.signed_in is False
        assert "no Google session cookie" in (status.detail or "")


# --------------------------------------------------------------------------- #
# Cookies are the authoritative signal
# --------------------------------------------------------------------------- #


class TestCookieEvidence:
    async def test_no_session_cookie_is_conclusive_without_navigating(self) -> None:
        """Absence is definitive, so it costs nothing — no page load at all."""
        driver = FakeBrowserDriver(cookies=NO_SESSION_COOKIES, url="about:blank")

        status = await verify_signed_in(driver)

        assert status.signed_in is False
        assert driver.visited == []

    async def test_an_empty_cookie_jar_is_not_signed_in(self) -> None:
        driver = FakeBrowserDriver(cookies=(), url="about:blank")

        assert (await verify_signed_in(driver)).signed_in is False

    @pytest.mark.parametrize("cookie", sorted(SESSION_COOKIES))
    async def test_any_single_session_cookie_is_enough(self, cookie: str) -> None:
        """Google sets different subsets on different hosts and rotates them over time.
        Requiring one specific name would rebuild the brittleness this replaced.
        """
        driver = FakeBrowserDriver(
            cookies=({"name": cookie, "value": "x", "domain": ".google.com"},),
            url=MYACCOUNT,
        )

        assert (await verify_signed_in(driver)).signed_in is True

    async def test_stale_cookies_with_a_redirect_are_not_signed_in(self) -> None:
        """The case cookies alone cannot decide, and the reason the probe still exists.

        A profile can hold expired session cookies. Only Google can say whether they still
        work, and it says so by redirecting an auth-required page to its sign-in host.
        """
        driver = FakeBrowserDriver(
            cookies=GOOGLE_SESSION_COOKIES, url="about:blank", redirect_to=SERVICE_LOGIN
        )

        status = await verify_signed_in(driver)

        assert status.signed_in is False
        assert "no longer valid" in (status.detail or "")
        assert SIGN_IN_HOST in (status.detail or "")


# --------------------------------------------------------------------------- #
# No DOM selector may decide the verdict
# --------------------------------------------------------------------------- #


class TestNoDomSelectorIsLoadBearing:
    async def test_signed_in_even_when_the_account_hint_cannot_be_read(self) -> None:
        """The exact shape of the original bug, pinned.

        ``script_result=None`` is a page where the ARIA scrape finds nothing — which is what
        ``myaccount.google.com`` really does. That must no longer change the answer.
        """
        driver = FakeBrowserDriver(
            cookies=GOOGLE_SESSION_COOKIES, url=MYACCOUNT, script_result=None
        )

        status = await verify_signed_in(driver)

        assert status.signed_in is True
        assert status.account_hint is None, "unreadable is fine; it is only a log field"

    async def test_the_account_hint_is_reported_when_available(self) -> None:
        driver = FakeBrowserDriver(
            cookies=GOOGLE_SESSION_COOKIES, url=GMAIL, script_result="avatar@example.com"
        )

        assert (await verify_signed_in(driver)).account_hint == "avatar@example.com"

    async def test_sign_in_prose_on_an_authenticated_page_does_not_flip_the_verdict(
        self,
    ) -> None:
        """A signed-in Google Account page legitimately contains the words "sign in" inside
        security copy. Text matching alone would read that as signed out."""
        driver = FakeBrowserDriver(
            cookies=GOOGLE_SESSION_COOKIES,
            url=MYACCOUNT,
            text="Signing in to Google. Review devices where you're signed in.",
        )

        assert (await verify_signed_in(driver)).signed_in is True

    @pytest.mark.parametrize("host", AUTHENTICATED_SURFACES)
    async def test_every_authenticated_surface_is_accepted(self, host: str) -> None:
        driver = FakeBrowserDriver(
            cookies=GOOGLE_SESSION_COOKIES, url=f"https://{host}/something"
        )

        assert (await verify_signed_in(driver)).signed_in is True


# --------------------------------------------------------------------------- #
# Challenges and browser failures stay distinguishable
# --------------------------------------------------------------------------- #


class TestOtherOutcomesStillWork:
    async def test_a_challenge_is_named_rather_than_reported_generically(self) -> None:
        """Reported ahead of the redirect test: a challenge also lives on the sign-in host, and
        "Google wants your second factor" is far more actionable than "not signed in"."""
        driver = FakeBrowserDriver(
            cookies=GOOGLE_SESSION_COOKIES,
            url="about:blank",
            redirect_to=SERVICE_LOGIN,
            text="2-Step Verification — check your phone",
        )

        status = await verify_signed_in(driver)

        assert status.signed_in is False
        assert "challenging this browser" in (status.detail or "")

    async def test_a_crashed_browser_propagates_rather_than_reading_as_signed_out(
        self,
    ) -> None:
        """Regression, preserved from the earlier fix: the two have opposite handling.
        ``GoogleAuthError`` is fatal, a crash is recoverable, and conflating them turns a dead
        tab into a permanently failed session that a relaunch would have fixed.
        """
        driver = FakeBrowserDriver(cookies=GOOGLE_SESSION_COOKIES, crash_after_goto=0)
        driver.crashed = True

        # ``BrowserError``, the common base, because that is the actual contract: every
        # browser-level failure propagates and none is reinterpreted as an auth verdict. Pinning
        # the concrete subclass would couple this to which flavour the fake happens to raise.
        with pytest.raises(BrowserError):
            await verify_signed_in(driver)

    async def test_an_unreachable_google_is_not_signed_in_but_is_not_a_crash(self) -> None:
        class Unreachable(FakeBrowserDriver):
            async def goto(self, url: str, *, timeout_s: float) -> None:
                raise OSError("network is unreachable")

        driver = Unreachable(cookies=GOOGLE_SESSION_COOKIES, url="about:blank")

        status = await verify_signed_in(driver)

        assert status.signed_in is False
        assert "could not reach Google" in (status.detail or "")


# --------------------------------------------------------------------------- #
# The contract the join flow depends on
# --------------------------------------------------------------------------- #


class TestRequireSignedIn:
    def test_a_signed_in_status_passes(self) -> None:
        AuthStatus(signed_in=True).require_signed_in()

    def test_the_error_names_the_remedy_and_carries_the_reason(self) -> None:
        status = AuthStatus(signed_in=False, detail="no Google session cookie")

        with pytest.raises(GoogleAuthError) as exc_info:
            status.require_signed_in()

        message = str(exc_info.value)
        assert "no Google session cookie" in message
        assert "MC_GOOGLE_MEET__HEADLESS=false" in message
        assert "MC_GOOGLE_MEET__PROFILE_DIR" in message
