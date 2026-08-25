"""RTMS keep-alive watchdog.

Deliberately lives inside ``connectors/zoom/rtms/`` and not in a shared "heartbeat"
module: ``msg_type 12``/``13`` is a Zoom wire detail, whereas *session liveness* is a
domain concern owned by ``services/session/``. Conflating the two was defect D6 in
doc 002 §1.2 — it would have leaked Zoom's protocol into the domain.

Behaviour: the server sends ``KEEP_ALIVE_REQ (12)`` and we echo its timestamp back in
``KEEP_ALIVE_RESP (13)``. If requests stop arriving inside the timeout, the
connection is treated as dead and the caller reconnects. Waiting to be dropped by
Zoom instead would only add latency to a failure we can already see.
"""

from __future__ import annotations

import time

from src.infrastructure.logging import get_logger

logger = get_logger(__name__)

DEFAULT_TIMEOUT_S = 60.0
"""Zoom does not publish its keep-alive interval, so this is generous. It bounds how
long a silently-dead socket can look alive; it is not a tight liveness SLA."""


class KeepAliveWatchdog:
    """Tracks whether keep-alive traffic is still flowing on one socket."""

    __slots__ = ("_last_seen", "_name", "_responses", "_timeout_s")

    def __init__(self, *, name: str, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        self._name = name
        self._timeout_s = timeout_s
        self._last_seen = time.monotonic()
        self._responses = 0

    @property
    def responses_sent(self) -> int:
        return self._responses

    def note_request(self) -> None:
        """Record that a keep-alive request arrived and was answered."""
        self._last_seen = time.monotonic()
        self._responses += 1

    def note_activity(self) -> None:
        """Record any inbound traffic.

        Media frames prove the socket is alive just as well as a keep-alive does, so
        a busy media socket is never declared dead for want of a ``msg_type 12``.
        """
        self._last_seen = time.monotonic()

    def seconds_since_activity(self) -> float:
        return time.monotonic() - self._last_seen

    def is_expired(self) -> bool:
        """True when nothing has been heard inside the timeout window."""
        return self.seconds_since_activity() > self._timeout_s

    def reset(self) -> None:
        """Restart the window. Call after a reconnect."""
        self._last_seen = time.monotonic()
