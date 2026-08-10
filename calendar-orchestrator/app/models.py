"""Data shapes shared between the calendar service, scheduler, and bot client."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class CalendarEvent:
    """A single upcoming meeting extracted from a Google Calendar event.

    ``updated`` is Google's own last-modification timestamp for the event — it changes on
    any edit, including a time change, so it doubles as the signal for "does this job need
    rescheduling" without the scheduler having to diff individual fields itself.
    """

    event_id: str
    summary: str
    start: datetime
    """Always timezone-aware (UTC)."""
    updated: str
    """Google's RFC3339 ``updated`` field, kept as an opaque string for comparison only."""
    meeting_code: str
    meeting_url: str


class MissingConferenceDataError(ValueError):
    """Raised when an event has no extractable Google Meet link.

    Caught and skipped by the calendar service — an event without conferencing isn't a
    meeting the bot has anything to join, not a failure worth surfacing.
    """
