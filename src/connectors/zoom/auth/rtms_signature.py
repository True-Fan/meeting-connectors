"""RTMS handshake signature.

Per Zoom's published protocol flow::

    signature = HMAC_SHA256(
        key     = client_secret,
        message = f"{client_id},{meeting_uuid},{rtms_stream_id}",
    ).hexdigest()

The same signature is presented on both the signaling and the media handshake.
"""

from __future__ import annotations

import hashlib
import hmac

from pydantic import SecretStr


def build_signature(
    *,
    client_id: str,
    client_secret: SecretStr,
    meeting_uuid: str,
    rtms_stream_id: str,
) -> str:
    """Return the hex-encoded HMAC-SHA256 handshake signature."""
    message = f"{client_id},{meeting_uuid},{rtms_stream_id}"
    return hmac.new(
        key=client_secret.get_secret_value().encode("utf-8"),
        msg=message.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()
