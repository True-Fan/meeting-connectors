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

from src.connectors.google_meet.automation.driver import BrowserDriver
from src.connectors.google_meet.exceptions import BrowserError, GoogleAuthError
from src.infrastructure.logging import get_logger

logger = get_logger(__name__)

ACCOUNTS_URL = "https://accounts.google.com/"
SIGNED_IN_PROBE_URL = "https://myaccount.google.com/"

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

    Probes ``myaccount.google.com``, which redirects to the sign-in page when there is no
    session and renders the account page when there is. That indirection is the point: it
    is a question only Google can answer, and asking it *before* navigating to the meeting
    is what turns "not signed in" into a precise error instead of an inexplicable join
    timeout twenty seconds later.

    Never raises for an unauthenticated profile — it reports. The caller decides, because
    a first-run bootstrap wants to attempt a login where a steady-state join wants to
    fail.

    **A browser failure is not an authentication failure, and the difference is
    load-bearing.** ``BrowserError`` propagates rather than being reported as "not signed
    in": a crashed renderer or a dead page is recoverable, and ``GoogleAuthError`` is
    classified as fatal (``reconnect/classify.py``). Absorbing one into the other would turn
    a tab that died during the probe into a permanently failed session that a relaunch would
    have fixed — while telling the operator to go and re-authenticate a profile that was
    never the problem.

    Raises:
        BrowserError: the browser is gone. Recoverable, and the caller's rejoin handles it.
    """
    try:
        await driver.goto(SIGNED_IN_PROBE_URL, timeout_s=timeout_s)
    except BrowserError:
        raise
    except Exception as exc:
        # Anything else — a navigation refused, a network failure reaching Google — really
        # does mean we cannot establish that this profile has a session.
        logger.warning("meet_auth.probe_failed", error=str(exc))
        return AuthStatus(signed_in=False, detail=f"could not reach Google: {exc}")

    text = (await driver.page_text()).lower()

    challenge = _first_present(text, _CHALLENGE_TEXT)
    if challenge is not None:
        return AuthStatus(
            signed_in=False,
            detail=f"Google is challenging this browser ({challenge!r})",
        )

    # Order matters: a signed-in account page can contain the word "sign in" inside
    # security copy, so the positive signal is checked by asking the page directly rather
    # than by the absence of sign-out text.
    account_hint = await _read_account_hint(driver)
    if account_hint is not None:
        logger.info("meet_auth.signed_in", account=_redact(account_hint))
        return AuthStatus(signed_in=True, account_hint=account_hint)

    signed_out = _first_present(text, _SIGNED_OUT_TEXT)
    return AuthStatus(
        signed_in=False,
        detail=f"Google presented a sign-in page ({signed_out!r})"
        if signed_out
        else "no Google account session found",
    )


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
      const el = document.querySelector('a[aria-label*="@"], [aria-label*="Google Account"]');
      if (!el) return null;
      const label = el.getAttribute('aria-label') || '';
      const match = label.match(/[\\w.+-]+@[\\w.-]+/);
      return match ? match[0] : null;
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
