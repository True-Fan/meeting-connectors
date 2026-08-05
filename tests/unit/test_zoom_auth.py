"""Zoom authentication: webhook verification, RTMS signature, Meeting SDK JWT."""

from __future__ import annotations

import hashlib
import hmac
import time

import orjson
import pytest
from pydantic import SecretStr

from src.connectors.zoom.auth.rtms_signature import build_signature
from src.connectors.zoom.auth.sdk_jwt import SdkJwtFactory, decode_unverified_claims
from src.connectors.zoom.auth.webhook_verifier import (
    DEFAULT_MAX_SKEW_S,
    WebhookVerifier,
)
from src.connectors.zoom.exceptions import WebhookVerificationError

SECRET = SecretStr("webhook-secret-token")


def sign(body: bytes, timestamp: str, secret: str = "webhook-secret-token") -> str:
    message = f"v0:{timestamp}:{body.decode()}"
    digest = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    return f"v0={digest}"


class TestRtmsSignature:
    def test_matches_documented_formula(self) -> None:
        """HMAC_SHA256(secret, "client_id,meeting_uuid,stream_id") as hex."""
        expected = hmac.new(
            b"cs", b"cid,uuid-1,stream-1", hashlib.sha256
        ).hexdigest()
        actual = build_signature(
            client_id="cid",
            client_secret=SecretStr("cs"),
            meeting_uuid="uuid-1",
            rtms_stream_id="stream-1",
        )
        assert actual == expected

    def test_is_deterministic(self) -> None:
        args = {
            "client_id": "cid",
            "client_secret": SecretStr("cs"),
            "meeting_uuid": "u",
            "rtms_stream_id": "s",
        }
        assert build_signature(**args) == build_signature(**args)

    def test_differs_per_stream(self) -> None:
        base = {"client_id": "cid", "client_secret": SecretStr("cs"), "meeting_uuid": "u"}
        assert build_signature(**base, rtms_stream_id="a") != build_signature(
            **base, rtms_stream_id="b"
        )


class TestWebhookVerifier:
    @pytest.fixture
    def verifier(self) -> WebhookVerifier:
        return WebhookVerifier(SECRET)

    def test_accepts_a_valid_signature(self, verifier: WebhookVerifier) -> None:
        body = orjson.dumps({"event": "meeting.rtms_started"})
        now = int(time.time())
        verifier.verify(body=body, signature=sign(body, str(now)), timestamp=str(now), now=now)

    def test_rejects_a_tampered_body(self, verifier: WebhookVerifier) -> None:
        now = int(time.time())
        signature = sign(b'{"event":"a"}', str(now))
        with pytest.raises(WebhookVerificationError, match="signature mismatch"):
            verifier.verify(
                body=b'{"event":"b"}', signature=signature, timestamp=str(now), now=now
            )

    def test_rejects_wrong_secret(self) -> None:
        body = b"{}"
        now = int(time.time())
        signature = sign(body, str(now), secret="other-secret")
        with pytest.raises(WebhookVerificationError, match="signature mismatch"):
            WebhookVerifier(SECRET).verify(
                body=body, signature=signature, timestamp=str(now), now=now
            )

    def test_rejects_missing_headers(self, verifier: WebhookVerifier) -> None:
        with pytest.raises(WebhookVerificationError, match="missing"):
            verifier.verify(body=b"{}", signature=None, timestamp="1")
        with pytest.raises(WebhookVerificationError, match="missing"):
            verifier.verify(body=b"{}", signature="v0=x", timestamp=None)

    def test_rejects_stale_timestamp(self, verifier: WebhookVerifier) -> None:
        """Bounds replay of a captured request."""
        now = int(time.time())
        stale = now - DEFAULT_MAX_SKEW_S - 1
        body = b"{}"
        with pytest.raises(WebhookVerificationError, match="window"):
            verifier.verify(
                body=body, signature=sign(body, str(stale)), timestamp=str(stale), now=now
            )

    def test_rejects_future_timestamp(self, verifier: WebhookVerifier) -> None:
        now = int(time.time())
        future = now + DEFAULT_MAX_SKEW_S + 1
        body = b"{}"
        with pytest.raises(WebhookVerificationError, match="window"):
            verifier.verify(
                body=body, signature=sign(body, str(future)), timestamp=str(future), now=now
            )

    def test_rejects_non_integer_timestamp(self, verifier: WebhookVerifier) -> None:
        with pytest.raises(WebhookVerificationError, match="integer"):
            verifier.verify(body=b"{}", signature="v0=x", timestamp="not-a-number")

    def test_unconfigured_secret_rejects_everything(self) -> None:
        """Fail closed: an unconfigured verifier must not accept traffic."""
        with pytest.raises(WebhookVerificationError, match="not configured"):
            WebhookVerifier(SecretStr("")).verify(
                body=b"{}", signature="v0=x", timestamp=str(int(time.time()))
            )

    def test_error_never_echoes_the_received_signature(self, verifier: WebhookVerifier) -> None:
        """A rejected probe must learn nothing about what was expected."""
        now = int(time.time())
        secret_looking = "v0=deadbeef" * 4
        with pytest.raises(WebhookVerificationError) as info:
            verifier.verify(
                body=b"{}", signature=secret_looking, timestamp=str(now), now=now
            )
        assert "deadbeef" not in str(info.value)

    def test_url_validation_reply(self, verifier: WebhookVerifier) -> None:
        reply = verifier.build_url_validation_reply("plain-abc")
        expected = hmac.new(
            b"webhook-secret-token", b"plain-abc", hashlib.sha256
        ).hexdigest()
        assert reply.plain_token == "plain-abc"
        assert reply.encrypted_token == expected
        assert reply.as_dict() == {"plainToken": "plain-abc", "encryptedToken": expected}


class TestSdkJwt:
    @pytest.fixture
    def factory(self) -> SdkJwtFactory:
        return SdkJwtFactory(sdk_key="sdk-key", sdk_secret=SecretStr("sdk-secret"), ttl_s=300)

    def test_claims(self, factory: SdkJwtFactory) -> None:
        token = factory.mint(meeting_number="1234567890", now=1_000_000)
        claims = decode_unverified_claims(token.token)
        assert claims["appKey"] == "sdk-key"
        assert claims["sdkKey"] == "sdk-key"
        assert claims["mn"] == "1234567890"
        assert claims["role"] == 0
        assert claims["iat"] == 1_000_000
        assert claims["exp"] == 1_000_300
        assert claims["tokenExp"] == 1_000_300
        assert token.expires_at == 1_000_300

    def test_host_role(self, factory: SdkJwtFactory) -> None:
        token = factory.mint(meeting_number="1", as_host=True, now=0)
        assert decode_unverified_claims(token.token)["role"] == 1

    def test_signature_verifies(self, factory: SdkJwtFactory) -> None:
        token = factory.mint(meeting_number="1", now=0).token
        header_b64, claims_b64, signature_b64 = token.split(".")
        import base64

        expected = hmac.new(
            b"sdk-secret", f"{header_b64}.{claims_b64}".encode(), hashlib.sha256
        ).digest()
        padded = signature_b64 + "=" * (-len(signature_b64) % 4)
        assert base64.urlsafe_b64decode(padded) == expected

    def test_header_is_hs256(self, factory: SdkJwtFactory) -> None:
        import base64
        import json

        header_b64 = factory.mint(meeting_number="1", now=0).token.split(".")[0]
        padded = header_b64 + "=" * (-len(header_b64) % 4)
        assert json.loads(base64.urlsafe_b64decode(padded)) == {"alg": "HS256", "typ": "JWT"}

    def test_no_base64_padding(self, factory: SdkJwtFactory) -> None:
        """JWT uses unpadded base64url."""
        assert "=" not in factory.mint(meeting_number="1", now=0).token

    def test_unconfigured_credentials_raise(self) -> None:
        with pytest.raises(ValueError, match="not configured"):
            SdkJwtFactory(sdk_key="", sdk_secret=SecretStr("")).mint(meeting_number="1")

    def test_repr_does_not_leak_the_token(self, factory: SdkJwtFactory) -> None:
        """Structured logging makes an accidental interpolation an easy leak."""
        token = factory.mint(meeting_number="1", now=0)
        assert token.token not in str(token)
