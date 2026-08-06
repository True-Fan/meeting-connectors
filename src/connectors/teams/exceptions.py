"""Teams connector exceptions.

Deliberately a separate hierarchy from ``connectors.zoom.exceptions``: the two
connectors share no failure modes worth unifying (RTMS handshakes and Graph call
rejections have nothing in common), and a shared base class would be an abstraction
neither one asked for. Where a domain concept is violated, these translate into
``domain.exceptions`` at the boundary.
"""

from __future__ import annotations


class TeamsError(Exception):
    """Base class for Teams connector errors."""


class TeamsConfigurationError(TeamsError):
    """The connector is misconfigured and cannot join anything.

    Raised at build time rather than join time so a missing tenant id fails the
    session creation request instead of a live meeting.
    """


class JoinUrlError(TeamsError):
    """A Teams meeting join URL could not be parsed into a Graph join descriptor."""


class TeamsSidecarError(TeamsError):
    """Base class for failures on the link to the Windows media sidecar."""


class SidecarUnavailableError(TeamsSidecarError):
    """The sidecar is unreachable or the link dropped. Recoverable."""


class SidecarProtocolError(TeamsSidecarError):
    """The byte stream desynced or violated the wire contract.

    Fatal for the connection by design: a desynced binary stream cannot be realigned
    with any confidence, so the link is torn down and rebuilt (doc 005 §6).
    """


class SidecarFatalError(TeamsSidecarError):
    """The sidecar reported a condition that retrying cannot fix.

    Raised for e.g. a rejected Azure AD credential, a missing
    ``Calls.AccessMedia.All`` consent, or an uninitialisable media platform. These
    must fail the session loudly rather than burning the reconnect budget on an
    error that will recur identically ten times.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"teams sidecar fatal error [{code}]: {message}")
        self.code = code
        self.message = message


class CallJoinError(TeamsSidecarError):
    """The Graph call could not be created or was dropped by the service."""
