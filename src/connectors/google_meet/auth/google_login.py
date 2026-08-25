"""Google sign-in — verified every session, performed almost never.

**The position this module takes, and why.** The supported way to authenticate the avatar
is to sign the persistent profile in **once, interactively**, and let every session
inherit the cookie (``browser/profile.py``). Scripted sign-in is offered here as a
bootstrap for an empty profile, and it is genuinely best-effort, because Google's flow can
legitimately interrupt with any of:

* a second factor — a prompt on a phone, a TOTP, a passkey;
* a device-verification challenge on an unrecognised browser or IP;
* a "this browser or app may not be secure" refusal, which is Google declining automation
  outright;
* a consent or recovery-details interstitial.

None of those is a bug to be worked around, and none is solved by retrying. They are
Google protecting an account, which is the same protection the avatar's account wants. So
this module's *primary* job is not to sign in — it is to **tell the two failure modes
apart**:

* the profile has no valid Google session → fatal, and the operator must re-authenticate;
* the profile is fine, but this meeting refused us → a meeting-level outcome.

Both leave the browser sitting outside the call, and conflating them produces the worst
possible behaviour: a rejoin loop against a meeting that will never admit us, driven by an
account that Google is already suspicious of.

``verify_signed_in`` is what the join flow calls. ``attempt_password_login`` exists so a
first run on a fresh deployment has a path, and it says so when it gives up.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from src.connectors.google_meet.automation.driver import BrowserDriver
from src.connectors.google_meet.exceptions import BrowserError, GoogleAuthError
from src.infrastructure.logging import get_logger

logger = get_logger(__name__)

ACCOUNTS_URL = "https://accounts.google.com/"
SIGNED_IN_PROBE_URL = "https://myaccount.google.com/"

SIGN_IN_HOST = "accounts.google.com"
"""The host Google sends an unauthenticated browser to. A probe of an auth-required page that
*ends* here is the definition of signed out."""

COOKIE_PROBE_URLS: tuple[str, ...] = (
    "https://www.google.com",
    "https://accounts.google.com",
)

SESSION_COOKIES: frozenset[str] = frozenset(
    {
        "SID",
        "__Secure-1PSID",
        "__Secure-3PSID",
        "SSID",
        "HSID",
        "SAPISID",
        "APISID",
        "LSID",
    }
)
"""Cookies that constitute a Google account session.

**These are the authoritative signal, and the reason this module was rewritten.** A Google
session *is* these cookies; anything a page displays about being signed in is a rendering of
them. Any one present means the profile holds a session — Google sets different subsets on
different hosts and rotates the set over time, so requiring one specific name would reintroduce
the brittleness this replaces."""

AUTHENTICATED_SURFACES: tuple[str, ...] = (
    "mail.google.com",
    "myaccount.google.com",
    "drive.google.com",
    "calendar.google.com",
    "docs.google.com",
    "meet.google.com",
    "photos.google.com",
    "contacts.google.com",
)
"""Hosts a browser only reaches with a working session.

Being *already on* one of these is proof in itself: Google served the page rather than
redirecting to a login. It also lets verification confirm an open Gmail tab **without navigating
away from it** — which the previous implementation could not do, because it navigated first and
so never looked at the page it had been given."""

_EMAIL_SELECTORS: tuple[str, ...] = (
    'input[type="email"]',
    "#identifierId",
)
_PASSWORD_SELECTORS: tuple[str, ...] = (
    'input[type="password"][name="Passwd"]',
    'input[type="password"]',
)
_NEXT_SELECTORS: tuple[str, ...] = (
    "#identifierNext button",
    "#passwordNext button",
    '//button[.//span[text()="Next"]]',
)

_CHALLENGE_TEXT: tuple[str, ...] = (
    "2-step verification",
    "verify it's you",
    "verify it\u2019s you",
    "check your phone",
    "enter the code",
    "couldn't sign you in",
    "couldn\u2019t sign you in",
    "this browser or app may not be secure",
    "try using a different browser",
)
"""Text that means "a human is required". Matched to produce a message that names the
actual obstacle, because "sign-in failed" sends an operator looking for a wrong password
when the real answer is a phone prompt nobody answered.

Two spellings of "it's" and "couldn't" appear because Google renders a typographic
apostrophe (U+2019) on some surfaces and an ASCII one on others, and matching only one
silently misses the challenge — which is the failure this whole list exists to catch. The
typographic variant is written as ``\\u2019`` so the near-duplicate lines are visibly
different in source rather than looking like an editing mistake."""

_SIGNED_OUT_TEXT: tuple[str, ...] = (
    "sign in",
    "use your google account",
    "choose an account",
)


@dataclass(frozen=True, slots=True)
class AuthStatus:
    """What we could determine about the profile's Google session."""

    signed_in: bool
    account_hint: str | None = None
    detail: str | None = None

    def require_signed_in(self) -> None:
        """Raise unless the profile is authenticated.

        Raises:
            GoogleAuthError: no valid session. Fatal by design — see the module
                docstring.
        """
        if self.signed_in:
            return
        raise GoogleAuthError(
            "the Chromium profile is not signed in to Google"
            + (f" ({self.detail})" if self.detail else "")
            + ". Sign the profile in once, interactively — run the connector with "
            "MC_GOOGLE_MEET__HEADLESS=false, complete the Google sign-in including any "
            "second factor, then stop the process. Every later session inherits that "
            "session from MC_GOOGLE_MEET__PROFILE_DIR"
        )


async def verify_signed_in(driver: BrowserDriver, *, timeout_s: float = 30.0) -> AuthStatus:
    """Determine whether the profile carries a usable Google session.

    **Rewritten because the original was wrong, and wrong in a way that made the connector
    unusable on a correctly-authenticated profile.** Its only positive signal was scraping an
    ARIA label for an ``@``, and that label does not exist on ``myaccount.google.com`` — the very
    page it navigated to. Worse, it navigated *first*, so even when the caller had a rendered
    Gmail tab open (where the label does resolve) it moved away before looking. A profile holding
    all seven Google session cookies reported "no Google account session found", every time.

    The replacement uses signals Google cannot rename, in decreasing order of authority:

    1. **Session cookies.** A Google session *is* its cookies; everything a page shows about
       being signed in is a rendering of them. Their absence is conclusive and needs no
       navigation at all.
    2. **Where we already are.** If the page is on a host that only serves an authenticated
       browser, Google has already answered the question. This is what lets an open Gmail tab
       be confirmed without navigating away from it.
    3. **Where a probe lands.** Otherwise, ask for an auth-required page and see whether Google
       *redirects* to a sign-in host. A redirect is behaviour, not markup, so it survives UI
       changes — and it is the only thing that can distinguish a live session from stale
       cookies, which is why step 1 alone is not enough.

    No DOM selector is load-bearing anywhere in that chain. ``_read_account_hint`` still runs,
    purely to name the account in a log line, and a failure there cannot change the verdict.

    Never raises for an unauthenticated profile — it reports. The caller decides, because a
    first-run bootstrap wants to attempt a login where a steady-state join wants to fail.

    **A browser failure is not an authentication failure, and the difference is load-bearing.**
    ``BrowserError`` propagates rather than being reported as "not signed in": a crashed renderer
    is recoverable, and ``GoogleAuthError`` is classified as fatal (``reconnect/classify.py``).
    Absorbing one into the other would turn a tab that died during the probe into a permanently
    failed session that a relaunch would have fixed — while telling the operator to
    re-authenticate a profile that was never the problem.

    Raises:
        BrowserError: the browser is gone. Recoverable, and the caller's rejoin handles it.
    """
    # -- 1. cookies: conclusive when absent, and free ---------------------------
    session_cookies = await _session_cookie_names(driver)
    if not session_cookies:
        return AuthStatus(
            signed_in=False,
            detail="the browser profile holds no Google session cookie",
        )

    # -- 2. already on a page only a signed-in browser can see -----------------
    current = driver.current_url()
    if _is_authenticated_surface(current):
        return await _signed_in(
            driver,
            session_cookies,
            detail=f"already on {_host(current)}",
        )

    # -- 3. probe, and read the redirect ---------------------------------------
    try:
        await driver.goto(SIGNED_IN_PROBE_URL, timeout_s=timeout_s)
    except BrowserError:
        raise
    except Exception as exc:
        # A navigation refused or a network failure reaching Google really does mean we cannot
        # establish that this profile has a session.
        logger.warning("meet_auth.probe_failed", error=str(exc))
        return AuthStatus(signed_in=False, detail=f"could not reach Google: {exc}")

    landed = driver.current_url()
    text = (await driver.page_text()).lower()

    # Checked before the redirect test because a challenge also lives on the sign-in host, and
    # "Google is asking for your second factor" is a far more actionable message than "you are
    # not signed in".
    challenge = _first_present(text, _CHALLENGE_TEXT)
    if challenge is not None:
        return AuthStatus(
            signed_in=False,
            detail=f"Google is challenging this browser ({challenge!r})",
        )

    if _is_sign_in_url(landed):
        return AuthStatus(
            signed_in=False,
            detail=(
                f"Google redirected an auth-required page to {_host(landed)}, so the session "
                "cookies present in the profile are no longer valid"
            ),
        )

    return await _signed_in(driver, session_cookies, detail=f"probe landed on {_host(landed)}")


async def _signed_in(
    driver: BrowserDriver, session_cookies: list[str], *, detail: str
) -> AuthStatus:
    """Build a signed-in verdict, naming the account if it can be read.

    The hint is looked up *after* the verdict is settled, and its failure is not an error — it
    exists so a log line can say which account is in use, nothing more. Making it decide the
    outcome is precisely the mistake this module was rewritten to remove.
    """
    account_hint = await _read_account_hint(driver)
    logger.info(
        "meet_auth.signed_in",
        account=_redact(account_hint) if account_hint else "unknown",
        evidence=detail,
        cookies=len(session_cookies),
    )
    return AuthStatus(signed_in=True, account_hint=account_hint, detail=detail)


async def _session_cookie_names(driver: BrowserDriver) -> list[str]:
    """Which Google session cookies the profile holds.

    A ``BrowserError`` propagates for the reason given in ``verify_signed_in``. Anything else is
    treated as "cannot tell", and returns empty — the caller then reports not-signed-in, which is
    the safe direction to fail in: it prompts a re-authentication rather than sending a browser
    into a meeting it cannot join.
    """
    try:
        cookies = await driver.cookies(COOKIE_PROBE_URLS)
    except BrowserError:
        raise
    except Exception as exc:
        logger.warning("meet_auth.cookies_unreadable", error=str(exc))
        return []
    names = {str(cookie.get("name", "")) for cookie in cookies}
    return sorted(names & SESSION_COOKIES)


def _host(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().split(":")[0]
    except ValueError:
        return ""


def _is_authenticated_surface(url: str) -> bool:
    """True when ``url`` is a host only a signed-in browser reaches."""
    return _host(url) in AUTHENTICATED_SURFACES


def _is_sign_in_url(url: str) -> bool:
    """True when ``url`` is Google's sign-in host.

    Host-level rather than path-level on purpose. A signed-in probe of ``myaccount.google.com``
    stays on ``myaccount.google.com`` — it reaches ``accounts.google.com`` only when Google wants
    credentials. Matching specific paths (``/ServiceLogin``, ``/v3/signin``, …) would be another
    list to keep current, and Google has renamed those repeatedly.
    """
    return _host(url) == SIGN_IN_HOST


async def _read_account_hint(driver: BrowserDriver) -> str | None:
    """Read the signed-in account's email from the page, if it is there.

    Uses the ARIA label on Google's account switcher, which states the account
    explicitly. Returns ``None`` rather than raising for a missing label: this is a
    diagnostic nicety, and a session is not less valid because Google moved an attribute.

    A ``BrowserError`` still propagates, for the reason given in ``verify_signed_in``: a dead
    page must not be reported as a signed-out profile.

    Raises:
        BrowserError: the browser is gone.
    """
    script = """
    (() => {
      const EMAIL = /[\\w.+-]+@[\\w.-]+\\.[A-Za-z]{2,}/;
      // Several sources, because no single one is present on every Google surface --
      // which is exactly how the previous single-selector version failed on
      // myaccount.google.com. Purely diagnostic now, so a miss costs a log field.
      const attrs = ['aria-label', 'data-email', 'title', 'alt'];
      const nodes = document.querySelectorAll(
        '[aria-label], [data-email], [data-identifier], img[alt], meta[content]'
      );
      for (const node of nodes) {
        for (const attr of attrs) {
          const value = node.getAttribute && node.getAttribute(attr);
          const found = value && value.match(EMAIL);
          if (found) return found[0];
        }
      }
      const inBody = (document.body ? document.body.innerText : '').match(EMAIL);
      return inBody ? inBody[0] : null;
    })()
    """
    try:
        value = await driver.evaluate(script)
    except BrowserError:
        raise
    except Exception:
        return None
    return str(value) if value else None


async def attempt_password_login(
    driver: BrowserDriver,
    *,
    email: str,
    password: str,
    timeout_s: float = 60.0,
) -> AuthStatus:
    """Try to sign in with an email and password. Best-effort by design.

    Only useful for bootstrapping an empty profile on a deployment that cannot run an
    interactive session, and only when the account has no second factor — which is not a
    configuration to recommend for an account that sits in customer meetings.

    Returns an ``AuthStatus``; it does not raise on refusal, so the caller can report the
    specific obstacle Google named rather than a generic failure.
    """
    if not email or not password:
        return AuthStatus(signed_in=False, detail="no google_email/google_password configured")

    logger.warning(
        "meet_auth.scripted_login",
        account=_redact(email),
        note="attempting scripted Google sign-in; this fails on any account with a "
        "second factor and is intended only to bootstrap an empty profile",
    )

    try:
        await driver.goto(ACCOUNTS_URL, timeout_s=timeout_s)

        if await driver.fill_first(_EMAIL_SELECTORS, email) is None:
            return AuthStatus(signed_in=False, detail="no email field on the sign-in page")
        await driver.click_first(_NEXT_SELECTORS)

        # Password fields are rendered after an animated transition, so the wait is on the
        # field itself rather than on a fixed delay.
        if await driver.wait_for_any(_PASSWORD_SELECTORS, timeout_s=20.0) is None:
            text = (await driver.page_text()).lower()
            challenge = _first_present(text, _CHALLENGE_TEXT)
            return AuthStatus(
                signed_in=False,
                detail=f"Google interrupted before the password step ({challenge!r})"
                if challenge
                else "the password field never appeared",
            )

        await driver.fill_first(_PASSWORD_SELECTORS, password)
        await driver.click_first(_NEXT_SELECTORS)
    except BrowserError:
        # As in ``verify_signed_in``: a dead browser is recoverable and must not be reported
        # as an authentication failure, which is classified fatal.
        raise
    except Exception as exc:
        return AuthStatus(signed_in=False, detail=f"sign-in navigation failed: {exc}")

    # Verify rather than assume. A submitted form is not a session, and Google's
    # post-password interstitials are exactly where a scripted login stops working.
    status = await verify_signed_in(driver, timeout_s=timeout_s)
    if status.signed_in:
        logger.info("meet_auth.scripted_login_succeeded", account=_redact(email))
    else:
        logger.error("meet_auth.scripted_login_failed", detail=status.detail)
    return status


def _first_present(haystack: str, needles: tuple[str, ...]) -> str | None:
    return next((needle for needle in needles if needle in haystack), None)


def _redact(email: str) -> str:
    """Keep the domain and the first character; drop the rest.

    Enough to confirm *which* account is configured when diagnosing a deployment, without
    writing a full account identifier into every log aggregator that scrapes these lines.
    """
    local, _, domain = email.partition("@")
    if not domain:
        return "***"
    head = local[:1] if local else ""
    return f"{head}***@{domain}"
