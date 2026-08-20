"""Reading the ``text/calendar`` part of a Google Calendar invitation.

**Why a calendar invitation needs its own handling at all.** The other two invite shapes are
recognised by who sent them (Zoom's system address) or what they are called (Zoom's fixed
in-meeting subject). A calendar invitation has neither handle: Google sends it *as the
organiser*, so the ``From:`` is an ordinary personal address, and the subject is the event's
own title — ``test zoom``, ``Standup``, anything at all, in any language.

What it does have is an ``invite.ics`` part, which is a structural fact rather than a string
somebody chose. That is the signal used here, and it is a far better one than either
alternative: it cannot be produced by accident, it does not depend on wording or locale, and
it carries the two things this service actually needs — the meeting link and **when the
meeting is**.

Two things in the format bite, and both are silent:

* **Line folding.** iCalendar wraps at 75 octets with a continuation line beginning with a
  space or tab. A Zoom join URL is longer than that, so an unfolded read truncates it
  mid-token — the meeting number often survives (it is near the front) while the URL does
  not, which produces a link that looks plausible and is wrong.
* **Escaping.** ``\\n``, ``\\,`` and ``\\;`` are literal two-character sequences inside a
  ``DESCRIPTION``, so the body arrives as one very long line unless they are undone.

Pure functions over strings, like ``meeting_link`` and for the same reason: the decisions
made here put a bot into somebody's meeting.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

_FOLD_RE = re.compile(r"\r?\n[ \t]")
"""A folded continuation: a line break followed by one space or tab, both of which are
removed along with the break."""

_DTSTART_RE = re.compile(r"^DTSTART(?P<params>;[^:]*)?:(?P<value>.+)$", re.MULTILINE)
_DTEND_RE = re.compile(r"^DTEND(?P<params>;[^:]*)?:(?P<value>.+)$", re.MULTILINE)
_TZID_RE = re.compile(r"TZID=([^;:]+)")

DEFAULT_DURATION = timedelta(hours=1)
"""Assumed length of an event whose ``DTEND`` is missing or unreadable.

Google always sends one, so this is a fallback rather than a normal path. An hour is chosen
because it is the common meeting length and because both directions of being wrong are mild:
too short means the bot declines to join the tail of a long meeting it was invited to late,
too long means it will accept an invitation for a meeting that has just ended.
"""


def unfold(text: str) -> str:
    """Undo iCalendar line folding and the escapes inside a text value.

    Order matters: unfolding must happen first, because a fold can land in the middle of an
    escape sequence and turn ``\\n`` into ``\\`` + newline + ``n``.
    """
    unfolded = _FOLD_RE.sub("", text)
    return (
        unfolded.replace("\\n", "\n")
        .replace("\\N", "\n")
        .replace("\\,", ",")
        .replace("\\;", ";")
    )


def is_invitation(text: str) -> bool:
    """Whether this really is a calendar invitation rather than some other ics.

    ``METHOD:REQUEST`` is what distinguishes an invitation from a cancellation
    (``METHOD:CANCEL``) or a reply to one (``METHOD:REPLY``) — a cancellation carries the
    same event, the same link and the same times, and acting on it would put the bot into a
    meeting that has just been called off.
    """
    if "BEGIN:VEVENT" not in text:
        return False
    method = re.search(r"^METHOD:(\w+)", text, re.MULTILINE)
    return method is None or method.group(1).upper() == "REQUEST"


def event_window(text: str) -> tuple[datetime, datetime] | None:
    """``(start, end)`` in UTC, or ``None`` when the event's timing cannot be read.

    ``None`` is deliberately **not** treated as "join now" by the caller. An invitation whose
    timing is unknown is one this service cannot say is happening, and the calendar poller —
    which reads Google's own parsed ``start`` field rather than an ics — is better placed to
    handle it.
    """
    start_match = _DTSTART_RE.search(text)
    if start_match is None:
        return None
    start = _parse_datetime(start_match.group("value"), start_match.group("params") or "")
    if start is None:
        return None

    end: datetime | None = None
    end_match = _DTEND_RE.search(text)
    if end_match is not None:
        end = _parse_datetime(end_match.group("value"), end_match.group("params") or "")
    if end is None or end <= start:
        end = start + DEFAULT_DURATION
    return start, end


def _parse_datetime(value: str, params: str) -> datetime | None:
    """One ``DTSTART``/``DTEND`` value into an aware UTC datetime.

    Three forms exist and only two are useful here:

    * ``20260820T103400Z`` — UTC, the form Google uses for a timed event.
    * ``20260820T160400`` with ``TZID=Asia/Kolkata`` — local time plus a zone, resolved
      through ``zoneinfo``. An unknown zone falls back to UTC rather than failing, because
      being a few hours out on a liveness check is recoverable and dropping the invite is not.
    * ``20260820`` with ``VALUE=DATE`` — an all-day event, which has no join moment and is
      rejected outright.
    """
    raw = value.strip()
    if "VALUE=DATE" in params.upper() and "T" not in raw:
        return None

    try:
        if raw.endswith("Z"):
            return datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        naive = datetime.strptime(raw, "%Y%m%dT%H%M%S")
    except ValueError:
        logger.debug("could not parse an ics timestamp: %r", raw)
        return None

    tz_match = _TZID_RE.search(params)
    if tz_match is None:
        return naive.replace(tzinfo=UTC)
    try:
        return naive.replace(tzinfo=ZoneInfo(tz_match.group(1).strip()))
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        logger.warning(
            "unknown timezone %r in a calendar invitation; assuming UTC",
            tz_match.group(1),
        )
        return naive.replace(tzinfo=UTC)


def is_happening_now(
    text: str, *, now: datetime | None = None, lead: timedelta = timedelta(minutes=5)
) -> bool:
    """Whether the invited meeting is live, or about to be.

    **This is the check that stops the inbox path stealing the scheduler's job.** An
    invitation to next Tuesday's standup arrives *now*, and every other filter in the poller
    would happily pass it — the mail is fresh, the link is real, the sender is the organiser.
    Joining on receipt would put the bot in a meeting six days early, and then leave it there.

    So the email path claims only what it is uniquely able to do: react to a meeting that is
    **already running**, which is exactly the case the calendar poller structurally cannot
    reach in time. Anything scheduled is left alone, for the poller to pick up at
    ``join_lead_time_s`` before it starts.

    Unreadable timing returns ``False`` for the same reason: the scheduler is the right owner
    of anything this cannot positively identify as live.
    """
    window = event_window(text)
    if window is None:
        return False
    start, end = window
    moment = now or datetime.now(UTC)
    return start - lead <= moment <= end
