"""Recognising a joinable meeting in free text, whichever platform it belongs to.

Pure functions over strings — no network, no state, no Google objects. That is deliberate
and it is the same argument ``invite_parser`` makes for itself: the risky question here is
*"does this text really name a meeting the bot should join?"*, and everything downstream of a
wrong answer puts a bot into somebody's call. A pure function is the only shape that lets
that question be tested exhaustively against captured invites.

Both entry points into this service — a calendar event and an invite email — end up asking
the same question about the same kinds of string, so they ask it here rather than each
carrying half a regex. Before this existed, ``calendar_service`` and ``invite_parser`` held
two copies of the Meet pattern with a comment on each saying they had to agree.

**Meet is tried before Zoom, and the order is not arbitrary.** A Zoom invite pasted into a
Google Calendar event still carries Google's own footer links, and a Meet event's description
can mention Zoom in prose. Meet's pattern is the stricter of the two (three letter groups, no
digits anywhere), so trying it first means an unambiguous Meet code always wins and Zoom is
only consulted when there is no Meet link at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

PLATFORM_GOOGLE_MEET = "google_meet"
PLATFORM_ZOOM = "zoom_web"
"""The bridge's ``zoom_web`` connector — a browser joining as an ordinary participant — and
not ``zoom``, which is the Meeting-SDK connector and needs the meeting to be hosted on an
RTMS-enabled account. An invite that arrives by mail or calendar is by definition a meeting
somebody else scheduled, so that entitlement is exactly what is not available."""

# A Meet code is three groups of letters: abc-defg-hij.
#
# Stricter than `meet\.google\.com/([a-z-]+)`, which also matches the ordinary links Google
# puts in the footer of these very emails — /support, /landing, /new — each of which would be
# handed to the bridge as a meeting number, sending the bot to dial a meeting that does not
# exist.
_MEET_CODE_RE = re.compile(r"meet\.google\.com/([a-z]+-[a-z]+-[a-z]+)")

# Meet codes are 3-4-3 letters. Used only to rank candidates, never to reject: Google has
# changed code shapes before, and refusing a real meeting because its code had an unexpected
# length would be a worse failure than joining one.
_CANONICAL_MEET_RE = re.compile(r"^[a-z]{3}-[a-z]{4}-[a-z]{3}$")

# A Zoom join link. The path segment is the whole security of this pattern:
#
#   /j/   the ordinary join link in every invite
#   /wc/  the web-client link, which is what this bridge itself navigates to
#   /s/   the host's start link, which carries the same meeting number
#
# Requiring one of those *and* a run of digits is what keeps `zoom.us/signin`,
# `zoom.us/download`, `zoom.us/pricing` and the rest of an invite's footer out. Meeting
# numbers are 9-11 digits today; the range is a little wider so a format change costs nothing.
_ZOOM_URL_RE = re.compile(
    r"https?://(?:[a-z0-9-]+\.)*zoom\.us/(?:j|wc|s)/(\d{9,12})\S*",
    re.IGNORECASE,
)

# Zoom's own protocol handler, which some clients put in the invite alongside the https link.
_ZOOM_MTG_RE = re.compile(r"zoommtg://[^\s<>\"']*?confno=(\d{9,12})", re.IGNORECASE)

# "Meeting ID: 841 3054 9760" — the human-readable form, used only when no link was found.
# Zoom groups the digits for legibility and the grouping varies, so the digits are extracted
# and rejoined rather than matched as a fixed shape.
# ``\uff1a`` is the fullwidth colon Zoom uses in its CJK-locale invites, written as an
# escape so it is visible rather than an indistinguishable character in the source.
_ZOOM_ID_TEXT_RE = re.compile(
    r"meeting\s*id\s*[:\uff1a]?\s*((?:\d[\s\-]*){9,14})", re.IGNORECASE
)

# "Passcode: 139601", "Password: abc123", and the localised colon Zoom uses in some regions.
#
# **This is the passcode a human types, and it is not the `pwd=` in the join URL.** That
# parameter is an encrypted token Zoom's own client exchanges for entry; typing it into the
# passcode box fails. The bridge's joiner fills that box (`input#input-for-pwd`), so the
# literal below is the only value that works — which is why a Zoom link with a `pwd=` token
# and no passcode line still yields ``passcode=None`` rather than something plausible.
_ZOOM_PASSCODE_RE = re.compile(
    r"(?:pass\s?code|password)\s*[:\uff1a]\s*([^\s<>\"'&]{1,32})",
    re.IGNORECASE,
)

# The dial-in one-tap line: "+16699009128,,84130549760#,,,,*139601#". The digits between
# "*" and "#" are the passcode, and this is the fallback for an invite whose passcode line
# was stripped by a mail client but whose dial-in block survived.
_ZOOM_ONETAP_PASSCODE_RE = re.compile(r"\*(\d{4,12})#")


# The labels Zoom puts at the head of each block of its invitation. Used to put the line
# breaks back when a renderer has taken them out — see `restore_line_breaks`.
_BLOCK_LABELS = (
    "topic:",
    "join zoom meeting",
    "meeting agenda",
    "meeting chat link",
    "meeting id",
    "passcode",
    "password",
    "one tap mobile",
    "join by sip",
    "join by telephone",
    "join instructions",
    "dial by your location",
    "find your local number",
)

_LABEL_BREAK_RE = re.compile(
    "(?<=\\S)(?=(?:" + "|".join(re.escape(label) for label in _BLOCK_LABELS) + "))",
    re.IGNORECASE,
)
_URL_BREAK_RE = re.compile(r"(?<=\S)(?=https?://)")
_RULE_BREAK_RE = re.compile(r"(?<=\S)(?=-{3,})")


def restore_line_breaks(text: str) -> str:
    """Put the invitation's block structure back after a renderer has flattened it.

    **This is the difference between reading the passcode and reading nonsense**, and the
    failure is entirely silent. Zoom's invitation is a stack of one-line blocks, and some
    renderings — Gmail's HTML view among them — drop every newline between them, so the body
    arrives as one unbroken string::

        Meeting ID: 963 5300 0755Passcode: 278999---One tap mobile+13052241968,,...

    Every pattern in this module ends at whitespace, because that is where a value ends in
    the format as written. With no whitespace to find, they run on: the passcode reads as
    ``278999---One``, and a join URL swallows the label that follows it
    (``...SLaiAa.1Meeting``). Both are *plausible* — a passcode-shaped string and a
    URL-shaped string — so nothing downstream can tell they are wrong. The bot then joins
    with a passcode Zoom rejects.

    Three markers put the boundaries back, and they are chosen because each is a place the
    format guarantees a break: the start of a known block label, the start of a URL, and the
    ``---`` rules Zoom uses between sections. A break is inserted only where one is missing
    (``(?<=\\S)``), so text that survived intact is untouched.

    Idempotent, and safe on text that never lost its breaks — which matters because the same
    body often arrives twice, once flattened as HTML and once intact as plain text.
    """
    if not text:
        return text
    restored = _URL_BREAK_RE.sub("\n", text)
    restored = _LABEL_BREAK_RE.sub("\n", restored)
    return _RULE_BREAK_RE.sub("\n", restored)


@dataclass(frozen=True, slots=True)
class MeetingLink:
    """A meeting the bot can be asked to join, and everything the bridge needs to do it."""

    platform: str
    """``google_meet`` or ``zoom_web`` — the bridge's own platform identifier, sent verbatim."""

    meeting_number: str
    """Meet's ``abc-defg-hij`` code, or Zoom's numeric meeting id with the spacing removed."""

    passcode: str | None = None
    """Zoom's typed passcode, when the invite spelled it out. ``None`` for Meet, which has no
    equivalent, and for a Zoom invite that carried only an encrypted ``pwd=`` token."""

    url: str = ""
    """The link as it appeared. Carried for logging and for the bridge's optional
    ``meeting_url``; the join itself goes by number on both platforms."""

    @property
    def is_zoom(self) -> bool:
        return self.platform == PLATFORM_ZOOM


def find_meeting_link(*texts: str) -> MeetingLink | None:
    """The first joinable meeting named in ``texts``, or ``None``.

    **Each argument is searched in full before the next is looked at**, which is how a caller
    expresses precedence between sources: a calendar event passes its structured
    ``conferenceData`` first and its free-text description last, so a stale link in a copied
    agenda can never outrank the field Google filled in itself.

    Concatenating the sources first — which this did — quietly loses that. A Zoom event whose
    description still carried last week's Meet link would resolve to Meet, because Meet is
    tried before Zoom *within* a text and the two were no longer distinguishable once joined.

    Within one text Meet does win over Zoom, for the reason in the module docstring: its
    pattern is the stricter of the two, so an unambiguous Meet code should not lose to a
    Zoom link mentioned in the same prose.

    ``None`` is an ordinary result rather than an error: most calendar events are not
    meetings and most mail is not an invite.
    """
    for raw in texts:
        if not raw:
            continue
        # Every pattern below ends at whitespace, so a flattened rendering must have its
        # boundaries restored first or they run past the values they are reading.
        text = restore_line_breaks(raw)
        link = _find_meet(text) or _find_zoom(text)
        if link is not None:
            return link
    return None


def _find_meet(text: str) -> MeetingLink | None:
    """A Google Meet code, preferring a canonically shaped one.

    When several distinct codes appear — the invite itself plus a quoted earlier thread — a
    3-4-3 code wins, and otherwise the first hit does.
    """
    candidates = _MEET_CODE_RE.findall(text)
    if not candidates:
        return None
    code = next((c for c in candidates if _CANONICAL_MEET_RE.match(c)), candidates[0])
    return MeetingLink(
        platform=PLATFORM_GOOGLE_MEET,
        meeting_number=code,
        passcode=None,
        url=f"https://meet.google.com/{code}",
    )


def _find_zoom(text: str) -> MeetingLink | None:
    """A Zoom meeting number, from a link if there is one and from prose if there is not."""
    url = ""
    number = ""

    match = _ZOOM_URL_RE.search(text)
    if match:
        number, url = match.group(1), match.group(0)
    else:
        match = _ZOOM_MTG_RE.search(text)
        if match:
            number, url = match.group(1), match.group(0)

    if not number:
        # **Prose only as a last resort.** "Meeting ID:" is far looser than a URL path and
        # appears in plenty of mail that is not a Zoom invite; reaching this line means no
        # link of any kind was present, which for a real invite is already unusual.
        text_match = _ZOOM_ID_TEXT_RE.search(text)
        if text_match:
            digits = re.sub(r"\D", "", text_match.group(1))
            if 9 <= len(digits) <= 12:
                number = digits

    if not number:
        return None

    return MeetingLink(
        platform=PLATFORM_ZOOM,
        meeting_number=re.sub(r"\D", "", number),
        passcode=find_zoom_passcode(text),
        url=url,
    )


# ``\uff1a`` as an escape, for the reason the other patterns use one: a literal
# fullwidth colon is indistinguishable from an ASCII one in the source.
_ZOOM_ID_LABEL_RE = re.compile(r"meeting\s*id\s*[:\uff1a]", re.IGNORECASE)


def has_zoom_invite_block(*texts: str) -> bool:
    """Whether this text *is* a Zoom invitation, rather than merely mentioning a Zoom link.

    **The signature is the block Zoom generates**, not any single element of it: a join link,
    plus at least one of the labelled ``Meeting ID:`` / ``Passcode:`` lines that always
    accompany it. That combination is what a person pastes when they invite somebody, and it
    is what survives being copied into a calendar event, forwarded, or reformatted by a mail
    client.

    **Why the body has to be the handle here.** A Zoom meeting added to a Google Calendar
    event arrives with a subject that is the *event's own title* — whatever the organiser
    typed, in whatever language — so no subject marker can match it, and no sender can be
    predicted either. Between them, the sender and the subject carry no usable signal at all.
    What is invariant is the invite text itself.

    Deliberately stricter than "there is a Zoom link somewhere". A colleague writing *"we
    used to meet at https://zoom.us/j/123456789"* has a link and no invite block, and should
    not move the bot. Requiring a labelled line as well is what separates an invitation from
    a mention.
    """
    joined = restore_line_breaks("\n".join(text for text in texts if text))
    if not joined:
        return False
    if not (_ZOOM_URL_RE.search(joined) or _ZOOM_MTG_RE.search(joined)):
        return False
    return bool(
        _ZOOM_ID_LABEL_RE.search(joined) or _ZOOM_PASSCODE_RE.search(joined)
    )


def find_zoom_passcode(*texts: str) -> str | None:
    """The passcode a participant would type, or ``None``.

    Explicitly **not** the ``pwd=`` query parameter on a join link: that is an encrypted
    token for Zoom's own client and is rejected by the passcode field the bridge types into.
    Returning it would be worse than returning nothing — the join would fail with a wrong
    passcode rather than stopping to ask for one.

    ``None`` is normal and not a failure. A meeting can have no passcode at all, and one whose
    invite only carried the token is joinable if it has a waiting room the host admits from.
    """
    joined = restore_line_breaks("\n".join(text for text in texts if text))
    if not joined:
        return None

    match = _ZOOM_PASSCODE_RE.search(joined)
    if match:
        return match.group(1).strip() or None

    onetap = _ZOOM_ONETAP_PASSCODE_RE.search(joined)
    if onetap:
        return onetap.group(1)
    return None
