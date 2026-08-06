"""Zoom app-install OAuth callback.

Installing or authorizing this app (including sharing it for beta test) always sends
Zoom's Add-App flow through a standard OAuth redirect — ``?code=...&state=...`` — no
matter what the app actually does with that code afterwards. This bridge is one of the
"afterwards": RTMS ingest is a signature over the General App's Client ID/Secret, and
the Meeting SDK JWT is signed the same way (doc 001 §4.1-4.2) — neither exchanges an
OAuth access token at runtime. This endpoint exists purely so that redirect has
somewhere sane to land, instead of at ``/webhooks/zoom/``, which is POST-only and
405s a `GET ...?code=...` (the exact failure mode doc 003 §1.5 was written to avoid).
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from src.infrastructure.logging import get_logger

logger = get_logger(__name__)

_AUTHORIZED_BODY = b"<html><body>Zoom app authorized. You can close this window.</body></html>"
_DENIED_BODY = b"<html><body>Authorization was not completed.</body></html>"


def build_router() -> APIRouter:
    """Build the OAuth callback router.

    A factory, matching ``webhook/router.py``, even though this one has no
    dependencies today — so adding one later (e.g. a real token exchange) does not
    change the calling convention in ``containers.py``.
    """
    router = APIRouter(tags=["zoom-oauth"])

    @router.get("/callback", summary="Zoom app-install redirect target")
    async def callback(code: str | None = None, state: str | None = None) -> Response:
        # Never log the code itself: it is short-lived but still a credential.
        logger.info("zoom.oauth.callback_received", authorized=code is not None, state=state)
        body = _AUTHORIZED_BODY if code else _DENIED_BODY
        return Response(content=body, media_type="text/html", status_code=status.HTTP_200_OK)

    return router
