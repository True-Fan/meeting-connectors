"""Polls the bot's Google Calendar and extracts joinable Google Meet events.

Polling rather than push notifications (Google Calendar supports webhook "watch" channels)
on purpose: watch channels need a publicly reachable HTTPS callback URL and expire on their
own schedule, which is extra infrastructure this service doesn't need to take on for a
single-calendar, minute-granularity use case. ``poll_interval_s`` trades a little latency
for a lot less moving parts; see README.md if push notifications are ever worth revisiting.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime
from typing import Any

from google.auth.exceptions import RefreshError
from googleapiclient.discovery import Resource, build
from googleapiclient.errors import HttpError

from app.config import Settings
from app.models import CalendarEvent, MissingConferenceDataError

logger = logging.getLogger(__name__)

# Matches the meeting code out of a Meet URL, e.g. "https://meet.google.com/veg-fkxv-rhg"
# -> "veg-fkxv-rhg". Meet codes are three lowercase-letter groups separated by hyphens.
_MEET_CODE_RE = re.compile(r"meet\.google\.com/([a-z]+-[a-z]+-[a-z]+)")


class CalendarSyncError(RuntimeError):
    """A poll failed. Callers should log and try again next interval rather than crash the
    scheduler loop over a transient Calendar API error."""


class CalendarService:
    """Thin async wrapper around the (synchronous) Google Calendar API client."""

    def __init__(self, credentials: Any, settings: Settings) -> None:
        self._settings = settings
        # discovery.build() and the resulting resource's .execute() are both blocking
        # network calls — every use below is dispatched through asyncio.to_thread so it
        # never stalls the scheduler's event loop.
        self._service: Resource = build(
            "calendar", "v3", credentials=credentials, cache_discovery=False
        )

    async def list_upcoming_meetings(self) -> list[CalendarEvent]:
        """Fetch events starting within the lookahead window that carry a Meet link.

        Events without conferencing, and cancelled events, are silently excluded — this is
        a list of things to *join*, not a mirror of the calendar.
        """
        try:
            response = await asyncio.to_thread(self._fetch_page)
        except RefreshError as exc:
            raise CalendarSyncError(f"credential refresh failed: {exc}") from exc
        except HttpError as exc:
            raise CalendarSyncError(f"Calendar API error: {exc}") from exc

        events: list[CalendarEvent] = []
        for raw in response.get("items", []):
            if raw.get("status") == "cancelled":
                continue
            try:
                events.append(_parse_event(raw))
            except MissingConferenceDataError:
                continue  # not a Meet event — e.g. a plain calendar block
            except (KeyError, ValueError) as exc:
                logger.warning("skipping malformed event %s: %s", raw.get("id"), exc)
        return events

    def _fetch_page(self) -> dict:
        now = datetime.now(UTC)
        time_max = now.timestamp() + self._settings.scheduling.lookahead_hours * 3600
        return (
            self._service.events()
            .list(
                calendarId=self._settings.calendar_id,
                timeMin=now.isoformat(),
                timeMax=datetime.fromtimestamp(time_max, tz=UTC).isoformat(),
                singleEvents=True,  # expand recurring events into concrete instances
                orderBy="startTime",
                maxResults=250,
            )
            .execute()
        )


def _parse_event(raw: dict) -> CalendarEvent:
    start_raw = raw.get("start", {})
    start_str = start_raw.get("dateTime")
    if start_str is None:
        # All-day events only have "date", never a Meet link worth joining at a precise time.
        raise MissingConferenceDataError("all-day event has no start time")
    start = datetime.fromisoformat(start_str)
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)

    meeting_url, meeting_code = _extract_meet_link(raw)

    return CalendarEvent(
        event_id=raw["id"],
        summary=raw.get("summary", "(no title)"),
        start=start,
        updated=raw.get("updated", ""),
        meeting_code=meeting_code,
        meeting_url=meeting_url,
        attendees=_extract_attendees(raw),
    )


def _extract_attendees(raw: dict) -> tuple[str, ...]:
    """Pull the invite list off a calendar event.

    ``displayName`` when Google has one, the email address otherwise — the bridge prefers
    whatever name Meet itself reports once somebody joins, so an address here is a serviceable
    placeholder rather than the final label.

    Three kinds of entry are dropped. ``resource`` entries are rooms and equipment, not people.
    ``self`` is the bot's own calendar account, which would otherwise be reported as an invitee
    who never joined while it is sitting in the meeting. Declined invitations are kept
    deliberately — "Priya was invited and did not come" is true and worth being able to say.
    """
    names: list[str] = []
    seen: set[str] = set()
    for attendee in raw.get("attendees", []) or ():
        if not isinstance(attendee, dict):
            continue
        if attendee.get("resource") or attendee.get("self"):
            continue
        label = str(attendee.get("displayName") or attendee.get("email") or "").strip()
        if not label or label.casefold() in seen:
            continue
        seen.add(label.casefold())
        names.append(label)
    return tuple(names)


def _extract_meet_link(raw: dict) -> tuple[str, str]:
    """Look in ``conferenceData`` first (the structured, current field), then fall back to
    ``hangoutLink`` (older events / simpler conferencing setups still populate only this)."""
    for entry_point in raw.get("conferenceData", {}).get("entryPoints", []):
        if entry_point.get("entryPointType") == "video":
            uri = entry_point.get("uri", "")
            match = _MEET_CODE_RE.search(uri)
            if match:
                return uri, match.group(1)

    hangout_link = raw.get("hangoutLink", "")
    match = _MEET_CODE_RE.search(hangout_link)
    if match:
        return hangout_link, match.group(1)

    raise MissingConferenceDataError(f"event {raw.get('id')} has no Google Meet link")
