"""Zoom-web connector exceptions.

The split that earns its keep is **recoverable versus fatal**, because it decides
whether the reconnect budget is spent or the session fails now. Everything here fails
at a browser and a DOM, which is the only way this connector reaches Zoom.
"""

from __future__ import annotations


class ZoomWebError(Exception):
    """Base class for Zoom-web connector errors."""


class ZoomWebJoinTimeoutError(ZoomWebError):
    """The join did not complete in time. Recoverable — rejoining usually works."""


class ZoomWebAdmissionError(ZoomWebError):
    """Zoom refused us: wrong passcode, entry denied, or the meeting has ended.

    Fatal. A host who denied entry will deny it again, a rejected passcode will be
    rejected again, and repeatedly rejoining a meeting we were removed from is the
    behaviour that gets an account blocked.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"zoom refused admission: {reason}")
        self.reason = reason

