"""Ask Zoom to start RTMS for a meeting we are joining.

Starting a meeting does not start RTMS. Zoom emits ``meeting.rtms_started`` — the
webhook carrying the only copy of the signaling URL ingest needs — solely when RTMS
was *explicitly* triggered, and it stops the stream again if nobody attaches within
about a minute.

Doing that by hand is a race the operator usually loses: trigger too early and the
stream is torn down before the session exists, too late and the session times out
waiting. Triggering it here removes the window — the session is registered and its
ingest leg already waiting before the request is sent.

Uses Server-to-Server OAuth, which is a **different credential set** from the General
App's client id and secret. The General App's id still matters: it names which app
RTMS should stream to, and Zoom rejects the call without it.
"""

from __future__ import annotations

import httpx
from pydantic import SecretStr

from src.connectors.zoom.exceptions import RtmsTriggerError
from src.infrastructure.logging import get_logger

logger = get_logger(__name__)


class RtmsTrigger:
    """Starts RTMS for a meeting over Zoom's REST API."""

    __slots__ = (
        "_account_id",
        "_api_base_url",
        "_app_client_id",
        "_client_id",
        "_client_secret",
        "_oauth_base_url",
        "_timeout_s",
        "_transport",
    )

    def __init__(
        self,
        *,
        account_id: str,
        client_id: str,
        client_secret: SecretStr,
        app_client_id: str,
        api_base_url: str = "https://api.zoom.us",
        oauth_base_url: str = "https://zoom.us",
        timeout_s: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._account_id = account_id
        self._client_id = client_id
        self._client_secret = client_secret
        self._app_client_id = app_client_id
        self._api_base_url = api_base_url.rstrip("/")
        self._oauth_base_url = oauth_base_url.rstrip("/")
        self._timeout_s = timeout_s
        # A seam for tests only: production passes nothing and gets httpx's own
        # networking. Lets the request contract be asserted without a live call.
        self._transport = transport

    async def start(self, meeting_number: str) -> None:
        """Ask Zoom to start RTMS for ``meeting_number``.

        Raises ``RtmsTriggerError`` on any failure. The caller treats that as
        non-fatal: RTMS may still be started by an account auto-start rule or by
        hand, and a session that never receives a webhook fails on its own timeout
        with a clearer message than this call could give.
        """
        async with httpx.AsyncClient(
            timeout=self._timeout_s, transport=self._transport
        ) as client:
            token = await self._fetch_token(client)
            await self._patch_status(client, token, meeting_number)

    async def _fetch_token(self, client: httpx.AsyncClient) -> str:
        """Exchange the S2S credentials for an access token.

        Fetched per call rather than cached: sessions start rarely, tokens expire,
        and a stale-token retry path would be more code than the request it saves.
        """
        try:
            response = await client.post(
                f"{self._oauth_base_url}/oauth/token",
                params={
                    "grant_type": "account_credentials",
                    "account_id": self._account_id,
                },
                auth=(self._client_id, self._client_secret.get_secret_value()),
            )
        except httpx.HTTPError as exc:
            raise RtmsTriggerError(f"token request failed: {exc}") from exc

        if response.status_code != httpx.codes.OK:
            raise RtmsTriggerError(
                f"token request rejected (status={response.status_code}): "
                f"{_safe_detail(response)}"
            )
        token = response.json().get("access_token", "")
        if not token:
            raise RtmsTriggerError("token response contained no access_token")
        return str(token)

    async def _patch_status(
        self, client: httpx.AsyncClient, token: str, meeting_number: str
    ) -> None:
        url = f"{self._api_base_url}/v2/live_meetings/{meeting_number}/rtms_app/status"
        try:
            response = await client.patch(
                url,
                headers={"Authorization": f"Bearer {token}"},
                json={"action": "start", "settings": {"client_id": self._app_client_id}},
            )
        except httpx.HTTPError as exc:
            raise RtmsTriggerError(f"rtms start request failed: {exc}") from exc

        # Zoom answers 200 or 204 depending on the meeting's state; both mean started.
        if response.status_code not in (httpx.codes.OK, httpx.codes.NO_CONTENT):
            raise RtmsTriggerError(
                f"rtms start rejected (status={response.status_code}): "
                f"{_safe_detail(response)}"
            )


def _safe_detail(response: httpx.Response) -> str:
    """Zoom's error message, truncated, never the credentials that produced it."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(body, dict):
        code = body.get("code")
        return f"code={code} {str(body.get('message', ''))[:200]}".strip()
    return str(body)[:200]
