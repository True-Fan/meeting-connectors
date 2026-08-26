"""Google Meet connector exceptions.

A separate hierarchy from ``connectors.zoom_web.exceptions`` and
``connectors.teams_web.exceptions``, for the same reason those two are separate from each
other: the failure modes have nothing in common. A Chromium crash, a Google sign-in
challenge, and a "you have been removed from the meeting" banner are not variants of
an RTMS handshake failure or a Graph call rejection, and a shared base class would be
an abstraction none of the three asked for.

The split that matters here is **recoverable versus fatal**, because it decides whether
the reconnect budget is spent or the session fails immediately:

* ``BrowserUnavailableError`` / ``BridgeUnavailableError`` — the browser or the page
  channel dropped. Retrying rejoins and usually works.
* ``GoogleAuthError`` / ``MeetConfigurationError`` — a credential, a consent, or a
  configuration problem. Every retry fails identically, so retrying only delays the
  real diagnosis by the length of the backoff budget.
* ``MeetingAdmissionError`` — the meeting itself refused us. Also fatal: a host who
  denied entry will deny it again.
"""

from __future__ import annotations


class GoogleMeetError(Exception):
    """Base class for Google Meet connector errors."""


class MeetConfigurationError(GoogleMeetError):
    """The connector is misconfigured and cannot join anything.

    Raised at build time rather than join time, so a missing profile directory fails
    the session-creation request instead of a live meeting.
    """


class MeetUrlError(GoogleMeetError):
    """A meeting code or URL could not be resolved into a Google Meet join URL."""


# --------------------------------------------------------------------------- #
# Browser
# --------------------------------------------------------------------------- #


class BrowserError(GoogleMeetError):
    """Base class for Chromium and Playwright failures."""


class PlaywrightUnavailableError(BrowserError):
    """Playwright, or its Chromium build, is not installed.

    Fatal and deliberately loud. The connector's entire media path runs inside a
    browser, so a missing browser is a deployment error, not a transient one — and the
    message names the exact command that fixes it, because the failure otherwise
    surfaces as an unhelpful import error deep inside a session start.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(
            f"{detail}. The Google Meet connector needs Playwright and its Chromium "
            "build: `pip install playwright && playwright install chromium`"
        )


class BrowserLaunchError(BrowserError):
    """Chromium could not be launched. Recoverable — usually a resource limit."""


class BrowserUnavailableError(BrowserError):
    """The browser, context, or page is gone. Recoverable by relaunching."""


class BrowserCrashedError(BrowserUnavailableError):
    """Chromium or the tab died. Recoverable, and the single most likely fault.

    A distinct type because it is worth counting separately: a session that rejoins
    five times because the renderer keeps dying needs more memory, whereas one that
    rejoins because the network flapped needs nothing.
    """


# --------------------------------------------------------------------------- #
# Google authentication
# --------------------------------------------------------------------------- #


class GoogleAuthError(GoogleMeetError):
    """The browser profile is not signed in to Google, and cannot be.

    Fatal. Google's sign-in flow can present a second factor, a device-verification
    challenge, or an automation block, none of which a retry resolves. The documented
    remedy is to authenticate the persistent profile once, interactively — see
    ``auth/google_login.py``.
    """


# --------------------------------------------------------------------------- #
# Joining and being in the meeting
# --------------------------------------------------------------------------- #


class JoinError(GoogleMeetError):
    """Base class for join failures."""


class JoinTimeoutError(JoinError):
    """The join did not complete in time. Recoverable."""


class MeetingAdmissionError(JoinError):
    """The meeting refused us: entry denied, ejected, or the meeting has ended.

    Fatal for the session. A host who clicked "Deny" will click it again, and rejoining
    a meeting we were removed from is exactly the behaviour that gets an account
    blocked.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"google meet refused admission: {reason}")
        self.reason = reason


class MeetingEndedError(JoinError):
    """The conference is over. Terminal, and not a failure of ours."""


# --------------------------------------------------------------------------- #
# The page↔bridge channel
# --------------------------------------------------------------------------- #


class BridgeError(GoogleMeetError):
    """Base class for failures on the loopback channel to the page."""


class BridgeUnavailableError(BridgeError):
    """The page is not connected, or the channel dropped. Recoverable."""


class BridgeProtocolError(BridgeError):
    """The page sent something that violates the wire contract.

    Fatal for the connection by design. Unlike Teams' TCP link there is no framing to
    resynchronise — WebSocket preserves message boundaries — so this means the page is
    running the wrong script version or has been tampered with, and neither is fixed by
    reading the next message.
    """


class BridgeAuthError(BridgeError):
    """A WebSocket client presented the wrong session token.

    The loopback server is reachable by anything running on the host, so an
    unauthenticated connection is refused rather than trusted. Fatal for that
    connection only; the real page can still connect.
    """
