"""Zoom webhook verification.

Zoom cryptographically signs every webhook, and production apps must verify before
processing. Two mechanisms, both implemented here:

**Request signature.** ``x-zm-signature: v0=<hex>`` over
``v0:{x-zm-request-timestamp}:{raw body}``, keyed by the webhook secret token.
Verified with ``hmac.compare_digest`` — never ``==``, which leaks timing.

**URL validation.** Zoom challenges the endpoint with ``endpoint.url_validation``
carrying a ``plainToken``; we reply with that token plus its HMAC.

The raw body bytes are required, not the parsed JSON: re-serialising changes
whitespace and key order, and the signature would never match.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass

from pydantic import SecretStr

from src.connectors.zoom.exceptions import WebhookVerificationError

SIGNATURE_HEADER = "x-zm-signature"
TIMESTAMP_HEADER = "x-zm-request-timestamp"
_SIGNATURE_PREFIX = "v0="

DEFAULT_MAX_SKEW_S = 300
"""Reject requests older than five minutes to bound replay."""


@dataclass(frozen=True, slots=True)
class UrlValidationReply:
    """The body Zoom expects in response to its endpoint challenge."""

    plain_token: str
    encrypted_token: str

    def as_dict(self) -> dict[str, str]:
        return {"plainToken": self.plain_token, "encryptedToken": self.encrypted_token}


class WebhookVerifier:
    """Verifies Zoom webhook signatures.

    Not a global: constructed by the DI container with the configured secret token.
    """

    __slots__ = ("_max_skew_s", "_secret_token")

    def __init__(self, secret_token: SecretStr, *, max_skew_s: int = DEFAULT_MAX_SKEW_S) -> None:
        self._secret_token = secret_token
        self._max_skew_s = max_skew_s

    def _digest(self, message: str) -> str:
        return hmac.new(
            key=self._secret_token.get_secret_value().encode("utf-8"),
            msg=message.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).hexdigest()

    def verify(
        self,
        *,
        body: bytes,
        signature: str | None,
        timestamp: str | None,
        now: float | None = None,
    ) -> None:
        """Verify a webhook request.

        Args:
            body: The **raw** request body. Must not be re-serialised.
            signature: ``x-zm-signature`` header value.
            timestamp: ``x-zm-request-timestamp`` header value.
            now: Injectable clock for tests.

        Raises:
            WebhookVerificationError: on any failure. The message never echoes the
                received signature, so a rejected probe learns nothing.
        """
        if not self._secret_token.get_secret_value():
            raise WebhookVerificationError("webhook secret token is not configured")
        if not signature:
            raise WebhookVerificationError(f"missing {SIGNATURE_HEADER} header")
        if not timestamp:
            raise WebhookVerificationError(f"missing {TIMESTAMP_HEADER} header")

        try:
            sent_at = int(timestamp)
        except ValueError as exc:
            raise WebhookVerificationError("request timestamp is not an integer") from exc

        current = time.time() if now is None else now
        if abs(current - sent_at) > self._max_skew_s:
            raise WebhookVerificationError(
                f"request timestamp outside {self._max_skew_s}s window"
            )

        message = f"v0:{timestamp}:{body.decode('utf-8', errors='strict')}"
        expected = _SIGNATURE_PREFIX + self._digest(message)

        if not hmac.compare_digest(expected, signature):
            raise WebhookVerificationError("signature mismatch")

    def build_url_validation_reply(self, plain_token: str) -> UrlValidationReply:
        """Answer Zoom's ``endpoint.url_validation`` challenge."""
        if not self._secret_token.get_secret_value():
            raise WebhookVerificationError("webhook secret token is not configured")
        return UrlValidationReply(
            plain_token=plain_token, encrypted_token=self._digest(plain_token)
        )
