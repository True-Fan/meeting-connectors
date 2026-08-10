"""Talks to the existing meeting-connectors bridge — the only thing this service is
allowed to know about the bridge is its HTTP contract, mirroring the cURL request in the
project README:

    curl --location 'http://localhost:8000/sessions' \\
      --header 'content-type: application/json' \\
      --data '{"platform": "google_meet", "meeting_number": "veg-fkxv-rhg"}'
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from app.config import BridgeSettings

logger = logging.getLogger(__name__)


class BotTriggerError(RuntimeError):
    """The bridge could not be reached or rejected the request after all retries."""


async def trigger_bot_join(meeting_code: str, settings: BridgeSettings) -> None:
    """POST to the bridge to make the bot join ``meeting_code``.

    Retries transient failures (connection errors, 5xx) with a fixed backoff; a 4xx is
    treated as non-retryable since retrying an identical bad request just wastes the retry
    budget on a call that will fail the same way every time.
    """
    payload = {"platform": settings.platform, "meeting_number": meeting_code}
    last_exc: Exception | None = None

    async with httpx.AsyncClient(timeout=settings.timeout_s) as client:
        for attempt in range(1, settings.max_retries + 2):  # +1 initial try, +1 for range
            try:
                response = await client.post(
                    settings.url,
                    json=payload,
                    headers={"content-type": "application/json"},
                )
                if response.status_code < 400:
                    logger.info(
                        "bridge accepted join for %s (attempt %d, status %d)",
                        meeting_code,
                        attempt,
                        response.status_code,
                    )
                    return
                if response.status_code < 500:
                    raise BotTriggerError(
                        f"bridge rejected join for {meeting_code}: "
                        f"{response.status_code} {response.text}"
                    )
                last_exc = BotTriggerError(
                    f"bridge returned {response.status_code}: {response.text}"
                )
            except httpx.HTTPError as exc:
                last_exc = exc

            if attempt <= settings.max_retries:
                logger.warning(
                    "join attempt %d/%d for %s failed: %s — retrying in %.1fs",
                    attempt,
                    settings.max_retries + 1,
                    meeting_code,
                    last_exc,
                    settings.retry_backoff_s,
                )
                await asyncio.sleep(settings.retry_backoff_s)

    raise BotTriggerError(
        f"could not trigger bot for {meeting_code} after {settings.max_retries + 1} attempts"
    ) from last_exc
