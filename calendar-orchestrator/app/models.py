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
    attendees: tuple[str, ...] = ()
    """Who was invited, best display name first.

    **This is the authoritative invite list, and the reason the bot does not scrape one.** Meet
    shows invitees who have not joined only inside the People panel, only in some layouts, and
    only behind selectors that change without notice — whereas the calendar event that created
    the meeting simply carries them. Passing them to the bridge means "who was invited but never
    turned up" is answerable without the avatar opening a panel or the page doing any extra DOM
    work on the thread that encodes its video.

    Empty for an event with no attendee list, which is normal: a meeting someone created for
    themselves and shared a link to has none, and the bridge is told the list is absent rather
    than empty so it can say "unknown" instead of "nobody"."""


class MissingConferenceDataError(ValueError):
    """Raised when an event has no extractable Google Meet link.

    Caught and skipped by the calendar service — an event without conferencing isn't a
    meeting the bot has anything to join, not a failure worth surfacing.
    """
