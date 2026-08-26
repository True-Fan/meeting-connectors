"""Teams-web connector exceptions.

A separate hierarchy from the other connectors', for the reason theirs are separate from
each other: everything here fails at a browser and a DOM, and the recovery for a Teams
join form is not the recovery for a Zoom one.

The split that earns its keep is **recoverable versus fatal**, because it decides whether
the reconnect budget is spent or the session fails now.
"""

from __future__ import annotations


class TeamsWebError(Exception):
    """Base class for Teams-web connector errors."""


class TeamsWebJoinTimeoutError(TeamsWebError):
    """The join did not complete in time. Recoverable — rejoining usually works.

    The most common cause is a lobby nobody attended to, which is why
    ``TeamsWebSettings.join_timeout_s`` is generous: an organiser who has not yet clicked
    "Admit" is a slow join, not a failed one.
    """


class TeamsWebAdmissionError(TeamsWebError):
    """Teams refused us: entry denied, the meeting has ended, or the link is not valid.

    Fatal. An organiser who denied entry will deny it again, an expired link will not
    become valid, and repeatedly rejoining a meeting we were removed from is the behaviour
    that gets an account blocked.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"teams refused admission: {reason}")
        self.reason = reason


class TeamsWebJoinTargetError(TeamsWebError):
    """Nothing in the request says which meeting to join.

    Fatal and raised before the browser is driven anywhere, so the failure names the missing
    input rather than surfacing as a join timeout two minutes later. The connector accepts
    either a ``meetup-join`` link or a numeric meeting id with its passcode; this is what a
    request carrying neither gets.
    """
