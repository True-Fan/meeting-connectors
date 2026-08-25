"""Meeting SDK JWT.

A wholly separate credential path from RTMS (doc 003 §4.2): signed with the Meeting
SDK app's key/secret, not the general app's client credentials.

Signed **in Python and handed to the sidecar** in ``CONTROL_JOIN``, so secrets live
in exactly one process and the C++ binary holds no long-lived credential. Tokens are
short-lived by default for the same reason.

HS256 is hand-rolled rather than pulling PyJWT: it is base64url over two JSON objects
plus one HMAC, and the whole implementation is testable in a few lines. One fewer
dependency in a service that already vendors a native SDK.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass

from pydantic import SecretStr

DEFAULT_TTL_S = 1800
"""Zoom rejects a shorter window: both ``exp`` and ``tokenExp`` must be at least 1800s
after ``iat`` (Meeting SDK auth spec). 30 minutes is the shortest valid — and therefore
the safest against a leak — TTL, not a security margin we chose ourselves."""

_ROLE_PARTICIPANT = 0
_ROLE_HOST = 1


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _segment(payload: dict[str, object]) -> str:
    return _b64url(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))


@dataclass(frozen=True, slots=True)
class MeetingSdkJwt:
    """A signed Meeting SDK token and its expiry."""

    token: str
    expires_at: int

    def __str__(self) -> str:  # pragma: no cover - guard against accidental logging
        return f"<MeetingSdkJwt exp={self.expires_at}>"


class SdkJwtFactory:
    """Mints Meeting SDK JWTs.

    Injected rather than global so tests can pin the clock and assert claims.
    """

    __slots__ = ("_sdk_key", "_sdk_secret", "_ttl_s")

    def __init__(self, sdk_key: str, sdk_secret: SecretStr, *, ttl_s: int = DEFAULT_TTL_S) -> None:
        self._sdk_key = sdk_key
        self._sdk_secret = sdk_secret
        self._ttl_s = ttl_s

    def mint(
        self,
        *,
        meeting_number: str,
        as_host: bool = False,
        now: int | None = None,
    ) -> MeetingSdkJwt:
        """Mint a token for one meeting join.

        Raises:
            ValueError: credentials are not configured.
        """
        secret = self._sdk_secret.get_secret_value()
        if not self._sdk_key or not secret:
            raise ValueError("Meeting SDK key/secret are not configured")

        issued_at = int(time.time()) if now is None else now
        expires_at = issued_at + self._ttl_s

        header = {"alg": "HS256", "typ": "JWT"}
        claims: dict[str, object] = {
            "appKey": self._sdk_key,
            "sdkKey": self._sdk_key,
            "mn": meeting_number,
            "role": _ROLE_HOST if as_host else _ROLE_PARTICIPANT,
            "iat": issued_at,
            "exp": expires_at,
            "tokenExp": expires_at,
        }

        signing_input = f"{_segment(header)}.{_segment(claims)}"
        signature = hmac.new(
            key=secret.encode("utf-8"),
            msg=signing_input.encode("ascii"),
            digestmod=hashlib.sha256,
        ).digest()

        return MeetingSdkJwt(
            token=f"{signing_input}.{_b64url(signature)}", expires_at=expires_at
        )


def decode_unverified_claims(token: str) -> dict[str, object]:
    """Decode a token's claims **without** verifying it.

    For tests and diagnostics only. Never use for authorisation.
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("not a three-segment JWT")
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    decoded: dict[str, object] = json.loads(base64.urlsafe_b64decode(padded))
    return decoded
