"""Which failures are worth retrying, in one place.

**Why this is its own module.** The decision appears in two places in
``bridge/chromium_bridge.py`` — once when the read loop fails, and once when a rejoin
attempt itself fails — and the two must agree. Written out twice they would drift, and the
way they drift is the expensive way: a failure classed as fatal in one branch and
recoverable in the other produces a browser that relaunches into a meeting that has already
refused it, ten times, with backoff.

**The rule, and why it is drawn where it is.** Retry only when the *next attempt could
plausibly differ*. That splits the failure modes cleanly:

* A crashed renderer, a dropped channel, a join timeout — the state that caused them is
  gone once a fresh browser starts. Retry.
* A missing Chromium, an unsigned-in profile, a bad configuration — every attempt reproduces
  the fault identically, so retrying converts a clear error into the same error reported
  much later. Fail now.
* A denied or ejected join — the next attempt *could* differ, if the host changed their
  mind. This is the interesting case, and it is classed fatal anyway: an automated account
  that repeatedly asks to enter a meeting it was thrown out of is indistinguishable from
  abuse, and the cost of getting that wrong is the Google account being restricted, which
  breaks every session rather than one.

That last exception is the reason this file explains itself at length. It is the one place
where the right engineering answer and the right operational answer diverge, and a reader who
does not know why would reasonably "fix" it.
"""

from __future__ import annotations

from src.connectors.google_meet.exceptions import (
    BridgeAuthError,
    GoogleAuthError,
    MeetConfigurationError,
    MeetingAdmissionError,
    MeetingEndedError,
    MeetUrlError,
    PlaywrightUnavailableError,
)
from src.infrastructure.reconnect import ReconnectPolicy

FATAL_ERRORS: tuple[type[Exception], ...] = (
    # Deployment faults: no retry installs a browser or signs a profile in.
    PlaywrightUnavailableError,
    MeetConfigurationError,
    GoogleAuthError,
    MeetUrlError,
    BridgeAuthError,
    # Decisions made by someone else. See the module docstring for why admission failures
    # are here despite a retry being technically able to succeed.
    MeetingAdmissionError,
    MeetingEndedError,
)
"""Every failure for which rejoining is wrong. Order is documentation, not precedence."""


def is_fatal(error: BaseException) -> bool:
    """True when this failure must end the session rather than trigger a rejoin."""
    return isinstance(error, FATAL_ERRORS)


def build_policy(*, max_attempts: int) -> ReconnectPolicy:
    """The rejoin backoff for one bridge.

    Slower to start and slower to give up than the other connectors', because a rejoin here
    costs far more. Zoom reconnects a WebSocket and Teams re-creates a call; this closes a
    browser, clones a profile, launches Chromium, signs in, navigates, and may sit in a
    lobby waiting for a human. Retrying that on a 500 ms initial delay would spend the
    entire budget inside the time one attempt needs to finish.

    Full jitter is inherited from ``ReconnectPolicy`` and matters here for the same reason it
    does elsewhere: several sessions losing their browsers to the same host-level memory
    pressure would otherwise all relaunch Chromium in lockstep and reproduce the pressure
    that killed them.
    """
    return ReconnectPolicy(
        initial_delay_s=2.0,
        max_delay_s=30.0,
        max_attempts=max_attempts,
    )
