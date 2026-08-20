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
    platform: str = "google_meet"
    """Which bridge connector serves this meeting.

    **Defaulted rather than required, which is what keeps this change invisible to the
    existing path.** Every event this service handled before Zoom was a Meet event, and every
    caller that builds a ``CalendarEvent`` without naming a platform still gets one. A Zoom
    meeting scheduled into the same calendar sets ``zoom_web``.

    Named ``platform`` because that is the field the bridge's ``POST /sessions`` takes; the
    string is passed through verbatim rather than translated, so there is one vocabulary."""
    passcode: str | None = None
    """The passcode a participant types, when the platform has one and the invite spelled it
    out. Always ``None`` for Meet, which has no equivalent. See ``meeting_link`` for why a
    Zoom ``pwd=`` token is deliberately not used here."""
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
    """Raised when an event names no meeting the bot could join.

    Caught and skipped by the calendar service — an event without conferencing isn't a
    meeting the bot has anything to join, not a failure worth surfacing.

    Named for Google Meet because that is all this service once handled; it now covers a Zoom
    link too. Kept under the old name rather than renamed, because the name appears in
    deployed log lines and the meaning is unchanged: there is nothing here to join.
    """
