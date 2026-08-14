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


async def trigger_bot_join(
    meeting_code: str,
    settings: BridgeSettings,
    *,
    attendees: tuple[str, ...] = (),
) -> None:
    """POST to the bridge to make the bot join ``meeting_code``.

    Retries transient failures (connection errors, 5xx) with a fixed backoff; a 4xx is
    treated as non-retryable since retrying an identical bad request just wastes the retry
    budget on a call that will fail the same way every time.

    ``attendees`` is the calendar event's invite list. It is sent as a **second, separate call**
    after the join succeeds, for two reasons: the join contract stays exactly the platform-blind
    ``{platform, meeting_number}`` the bridge README documents, and the invite list is optional
    enrichment — a bridge that rejects or has never heard of it must not cost us a meeting the
    bot would otherwise have joined. So that call is best-effort and never raises.
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
                    if attendees:
                        await _seed_invitees(client, response, attendees, settings)
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


async def _seed_invitees(
    client: httpx.AsyncClient,
    join_response: httpx.Response,
    attendees: tuple[str, ...],
    settings: BridgeSettings,
) -> None:
    """Tell the bridge who was invited, so it can answer "who never joined".

    Best-effort by design and never raises: the bot is already in the meeting by the time this
    runs, and losing the invite list costs one kind of question the avatar can answer. Failing
    the trigger over it would cost the meeting.

    Not retried, for the same reason. This is enrichment on a call that has already succeeded —
    a retry loop here would hold the scheduler's task open on something nobody is waiting for.
    """
    try:
        session_id = join_response.json().get("session_id")
    except ValueError:
        session_id = None
    if not session_id:
        logger.warning(
            "bridge accepted the join but returned no session_id; "
            "skipping the invite list (%d attendees)",
            len(attendees),
        )
        return

    # Derived from the configured sessions URL rather than separately configured, so the two
    # cannot drift apart or point at different hosts.
    url = f"{settings.url.rstrip('/')}/{session_id}/invitees"
    try:
        response = await client.post(
            url,
            json={"invitees": list(attendees)},
            headers={"content-type": "application/json"},
        )
    except httpx.HTTPError as exc:
        logger.warning("could not send the invite list to %s: %s", url, exc)
        return

    if response.status_code >= 400:
        logger.warning(
            "bridge rejected the invite list for session %s: %d %s",
            session_id,
            response.status_code,
            response.text[:200],
        )
        return
    logger.info("sent %d invitees for session %s", len(attendees), session_id)
