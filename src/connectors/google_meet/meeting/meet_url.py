"""Resolve a ``MeetingContext`` into a Google Meet URL.

The outbound anti-corruption boundary for this connector, and the counterpart to
``connectors/teams/graph/join_url.py``: the only module that reads ``MeetingContext`` and
speaks Google Meet, and the only one that knows what a Meet link looks like.

Two routes, preferred in this order — the same ordering Teams uses, for the same reason:

1. **Meeting code** in ``meeting_number``, e.g. ``abc-defg-hij``. An operator drives Meet
   through the identical ``POST /sessions`` shape they already use for Zoom and Teams. No
   new API surface, which is the whole reason to prefer it.
2. **Join URL** in ``platform_data["meeting_url"]``, because a calendar invite's link is
   often all anyone has.

**Meet has no passcode, and that absence is meaningful.** Zoom and Teams both take one,
so ``MeetingContext.passcode`` exists and Meet sessions will sometimes arrive with it
populated by an operator working from muscle memory. Admission to a Meet conference is
controlled by the host and by Workspace policy, never by a secret the joiner supplies, so
a passcode here is not a credential we failed to use — it is a sign the request was
written for the wrong platform. It is reported rather than ignored, because silently
dropping it would let a genuinely mistaken request look like it worked.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

from src.connectors.google_meet.exceptions import MeetUrlError
from src.domain.meeting import MeetingContext
from src.infrastructure.logging import get_logger

logger = get_logger(__name__)

MEET_HOST = "meet.google.com"
_MEET_HOSTS = frozenset({MEET_HOST, "www.meet.google.com", "g.co", "meet.google.co.uk"})

_MEETING_CODE = re.compile(r"^[a-z]{3}-[a-z]{4}-[a-z]{3}$")
"""A Google Meet meeting code: three letters, four letters, three letters, lowercase.

Fixed by Google and worth matching exactly rather than loosely. A permissive pattern
would accept a Zoom meeting number or a Teams meeting id and navigate to a Meet URL that
404s — which surfaces as an unexplained join timeout several layers away from the
mistake."""

_UNDASHED_CODE = re.compile(r"^[a-z]{10}$")
"""The same code with its separators stripped, which is how it appears when copied out of
some clients. Re-dashed rather than rejected: it is unambiguous, since the grouping is
always 3-4-3."""

_LOOKUP_PATH = re.compile(r"^/lookup/([A-Za-z0-9_-]{3,64})$")
"""``/lookup/<nickname>`` — a named meeting rather than a coded one. Passed through
unresolved: only Google can map a nickname to a conference, and it does so on
navigation. So the URL is preserved verbatim and the browser resolves it."""

_SHORT_PATH = re.compile(r"^/meet/([A-Za-z0-9_-]{3,64})$")
"""``g.co/meet/<name>`` short links. Also resolved by navigation."""

_MEETING_URL_KEY = "meeting_url"
"""The single ``platform_data`` key this connector reads.

Zoom writes ``rtms_stream_id`` and ``signaling_url`` into the same dict, and Teams reads
this same key for its own URL. No connector may read another's keys, and sharing the name
for "the join link an operator pasted" is a coincidence of naming, not shared state —
each connector parses the value with its own grammar and rejects the other's."""


@dataclass(frozen=True, slots=True)
class MeetJoinTarget:
    """Where to navigate, and what we know about it."""

    url: str
    meeting_code: str | None
    """``None`` for a nickname or short link, which only Google can resolve."""

    is_lookup: bool = False

    def __str__(self) -> str:
        return self.meeting_code or self.url


def normalise_meeting_code(raw: str) -> str | None:
    """Return the canonical dashed meeting code, or ``None`` if this is not one."""
    candidate = raw.strip().lower().replace(" ", "")
    if _MEETING_CODE.match(candidate):
        return candidate
    if _UNDASHED_CODE.match(candidate):
        return f"{candidate[:3]}-{candidate[3:7]}-{candidate[7:]}"
    return None


def looks_like_meet_url(value: str) -> bool:
    """True when ``value`` is a Meet link rather than a bare meeting code."""
    lowered = value.strip().lower()
    return MEET_HOST in lowered or lowered.startswith(("http://", "https://"))


def parse_meet_url(url: str) -> MeetJoinTarget:
    """Parse a Google Meet URL into a navigation target.

    Accepts a scheme-less host (``meet.google.com/abc-defg-hij``) because that is how the
    link is often pasted, and rejecting it would fail a request that carries everything
    needed.

    Raises:
        MeetUrlError: not a Google Meet host, or the path is not a meeting code, a
            lookup, or a short link.
    """
    raw = url.strip()
    parsed = urlparse(raw if "//" in raw else f"https://{raw}")

    host = (parsed.netloc or "").lower().split(":")[0]
    if host not in _MEET_HOSTS:
        raise MeetUrlError(
            f"{host or raw!r} is not a Google Meet host; expected {MEET_HOST}"
        )

    path = parsed.path or "/"

    lookup = _LOOKUP_PATH.match(path) or _SHORT_PATH.match(path)
    if lookup is not None:
        # Query and fragment are dropped for a coded meeting below, but kept here: a
        # lookup link's parameters can carry the authuser that selects which signed-in
        # account resolves the nickname, and dropping it would resolve as the wrong one.
        rebuilt = f"https://{host}{path}"
        if parsed.query:
            rebuilt = f"{rebuilt}?{parsed.query}"
        return MeetJoinTarget(url=rebuilt, meeting_code=None, is_lookup=True)

    code = normalise_meeting_code(path.strip("/"))
    if code is None:
        raise MeetUrlError(
            f"Meet URL path {path!r} is not a meeting code (expected abc-defg-hij), a "
            "/lookup/<nickname>, or a /meet/<name> short link"
        )

    # Rebuilt from the code rather than passed through. Meet links accumulate tracking
    # and hint parameters (``hs``, ``ijlm``, ``authuser``, ``pli``) that can steer the
    # browser to a different account or an interstitial; the canonical URL joins the same
    # conference with none of that.
    return MeetJoinTarget(url=canonical_url(code), meeting_code=code)


def canonical_url(meeting_code: str) -> str:
    """The URL for a meeting code."""
    return f"https://{MEET_HOST}/{meeting_code}"


def resolve_join_target(meeting: MeetingContext) -> MeetJoinTarget:
    """Resolve where to navigate for ``meeting``.

    Raises:
        MeetUrlError: neither a usable meeting code nor a parseable Meet URL is present.
    """
    number = (meeting.meeting_number or "").strip()
    url = str(meeting.platform_data.get(_MEETING_URL_KEY) or "").strip()

    if meeting.passcode:
        # See the module docstring: Meet has no joiner-supplied secret, so this is
        # evidence of a request written for another platform. Loud, but not fatal — the
        # code or URL is what matters and it may well be correct.
        logger.warning(
            "meet_url.passcode_ignored",
            note="Google Meet admission is controlled by the host, not by a passcode; "
            "the supplied passcode cannot be used and may indicate the request was "
            "written for Zoom or Teams",
        )

    # A URL pasted into the meeting-number field is a natural operator mistake. Accept it
    # rather than rejecting a request that carries everything we need.
    if not url and looks_like_meet_url(number):
        url, number = number, ""

    if number:
        code = normalise_meeting_code(number)
        if code is not None:
            return MeetJoinTarget(url=canonical_url(code), meeting_code=code)
        if not url:
            raise MeetUrlError(
                f"meeting_number {number!r} is not a Google Meet code (expected "
                "abc-defg-hij); supply the code, or a meet.google.com link in meeting_url"
            )

    if url:
        return parse_meet_url(url)

    raise MeetUrlError(
        "cannot resolve a Google Meet join: supply the meeting code in meeting_number "
        "(abc-defg-hij) or a meet.google.com link in meeting_url"
    )
