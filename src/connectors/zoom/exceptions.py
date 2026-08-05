"""Zoom connector exceptions."""

from __future__ import annotations


class ZoomError(Exception):
    """Base class for Zoom connector errors."""


class WebhookVerificationError(ZoomError):
    """A webhook's signature did not verify. Never log the received signature."""


class RtmsError(ZoomError):
    """Base class for RTMS errors."""


class RtmsHandshakeError(RtmsError):
    """A handshake was rejected or malformed."""

    def __init__(self, stage: str, status_code: int | None, reason: str | None) -> None:
        detail = reason or "no reason given"
        super().__init__(f"RTMS {stage} handshake failed (status={status_code}): {detail}")
        self.stage = stage
        self.status_code = status_code
        self.reason = reason


class RtmsProtocolError(RtmsError):
    """A message violated the protocol in a way we cannot continue from."""


class RtmsConnectionError(RtmsError):
    """The transport failed. Recoverable — the reconnect policy applies."""


class KeepAliveTimeoutError(RtmsError):
    """A keep-alive request went unanswered.

    Treated as fatal for the connection: Zoom will drop us anyway, so forcing a
    reconnect immediately is strictly better than waiting to be dropped
    (doc 003 §7.1).
    """


class PublisherError(ZoomError):
    """Base class for Meeting SDK publisher errors."""


class SidecarUnavailableError(PublisherError):
    """The sidecar is not reachable. Recoverable."""


class SidecarFatalError(PublisherError):
    """The sidecar reported a fatal condition — do not retry.

    Raised for e.g. ``HasRawdataLicense() == false``, which must fail the session
    loudly rather than silently publishing nothing (doc 003 §7.1).
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"sidecar fatal error [{code}]: {message}")
        self.code = code
