"""Zoom authentication.

Two independent credential paths, deliberately not interchangeable (doc 003 §4):

* **RTMS ingest** — webhook signature verification and the handshake HMAC, from the
  general app's client id/secret and webhook secret token.
* **Meeting SDK publish** — a JWT signed with the Meeting SDK app's key/secret.
"""

from src.connectors.zoom.auth.rtms_signature import build_signature
from src.connectors.zoom.auth.sdk_jwt import (
    MeetingSdkJwt,
    SdkJwtFactory,
    decode_unverified_claims,
)
from src.connectors.zoom.auth.webhook_verifier import (
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    UrlValidationReply,
    WebhookVerifier,
)

__all__ = [
    "SIGNATURE_HEADER",
    "TIMESTAMP_HEADER",
    "MeetingSdkJwt",
    "SdkJwtFactory",
    "UrlValidationReply",
    "WebhookVerifier",
    "build_signature",
    "decode_unverified_claims",
]
