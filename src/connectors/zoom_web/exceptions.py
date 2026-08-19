"""Zoom-web connector exceptions.

Separate from ``connectors/zoom/exceptions`` even though both reach Zoom: this one
fails at a browser and a DOM, that one at an RTMS handshake and a native sidecar.

The split that earns its keep is **recoverable versus fatal**, because it decides
whether the reconnect budget is spent or the session fails now.
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


class VirtualMicUnavailableError(ZoomWebError):
    """No virtual microphone to publish through.

    Fatal and deliberately loud. Zoom publishes from a *device*; an injected track is
    consumed and never transmitted, which was measured rather than assumed. So
    without the device the avatar joins, reports healthy, and is silent — the exact
    failure this exception exists to make impossible.
    """
