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

**Teams goes between them, and that placement is load-bearing.** Zoom's last-resort pattern
reads a bare ``Meeting ID: 281 442 953 617`` out of prose, and Teams prints its meeting id in
exactly that form — so a Teams invitation offered to ``_find_zoom`` first resolves to a
``zoom_web`` join of a meeting number Zoom has never heard of. Teams' own patterns are
host-anchored (``teams.live.com`` / ``teams.microsoft.com``), so they cannot match a Zoom or
Meet invite and cost that path nothing; asking them first is what keeps the Zoom prose
fallback from claiming a meeting that is not Zoom's.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote, unquote

PLATFORM_GOOGLE_MEET = "google_meet"
PLATFORM_ZOOM = "zoom_web"
"""The bridge's ``zoom_web`` connector — a browser joining as an ordinary participant — and
not ``zoom``, which is the Meeting-SDK connector and needs the meeting to be hosted on an
RTMS-enabled account. An invite that arrives by mail or calendar is by definition a meeting
somebody else scheduled, so that entitlement is exactly what is not available."""

PLATFORM_TEAMS = "teams_web"
"""The bridge's ``teams_web`` connector, and **never** ``teams``, for a stronger version of
the argument ``PLATFORM_ZOOM`` makes.

``teams`` is the Graph/media-SDK connector: it needs an Azure AD app with admin-consented
``Calls.AccessMedia.All``, a tenant willing to grant it, and a Windows host. A meeting that
arrives as an invite belongs to somebody else's tenant — often a personal ("Teams for Life")
account with no tenant at all — so none of those three is available. ``teams_web`` drives
Chromium and joins as an anonymous guest, which is the only route an invited meeting has."""

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
#
# Shared with Teams rather than copied: both platforms print the same ``Passcode:`` line and
# both feed it to a text input, so one definition is one thing to keep right. What differs is
# the *fallbacks* around it, which is why each platform still has its own function below.
_PASSCODE_LINE_RE = re.compile(
    r"(?:pass\s?code|password)\s*[:\uff1a]\s*([^\s<>\"'&]{1,32})",
    re.IGNORECASE,
)

# The dial-in one-tap line: "+16699009128,,84130549760#,,,,*139601#". The digits between
# "*" and "#" are the passcode, and this is the fallback for an invite whose passcode line
# was stripped by a mail client but whose dial-in block survived.
_ZOOM_ONETAP_PASSCODE_RE = re.compile(r"\*(\d{4,12})#")


# --------------------------------------------------------------------------- #
# Microsoft Teams
# --------------------------------------------------------------------------- #

# The two hosts Teams join links live on, and they are not interchangeable:
# ``teams.live.com`` is a personal / free ("Teams for Life") account, ``teams.microsoft.com``
# is work/school. Both are accepted because an invite can come from either and the bridge's
# ``teams_web`` joiner navigates whichever URL it is handed.
_TEAMS_HOST = r"(?:[a-z0-9-]+\.)*(?:teams\.live\.com|teams\.microsoft\.com)"

# The short form, and the one this feature was asked for:
#
#     https://teams.live.com/meet/9339756425487?p=71cQWhQJ5X8fxHSmVy
#
# ``9339756425487`` is the meeting id and ``71cQWhQJ5X8fxHSmVy`` is the passcode. The same
# shape exists on ``teams.microsoft.com/meet/<id>?p=<passcode>`` for work/school accounts.
#
# **The host and the ``/meet/`` segment are the whole security of this pattern**, exactly as
# ``/j/`` is for Zoom. Without them, ``teams.microsoft.com/downloads``,
# ``.../meetingOptions/...`` and the "Need help?" link in every invite footer would each be
# handed to the bridge as a meeting.
#
# Ids run 9-15 digits: personal ids are 13, work/school 12, and the range is wider than either
# because a format change should cost nothing. The tail is captured only to read ``p=`` out of
# it — the URL sent to the bridge is rebuilt from the parts, never taken from the text.
_TEAMS_MEET_URL_RE = re.compile(
    rf"(?P<scheme>https?)://(?P<host>{_TEAMS_HOST})/meet/(?P<id>\d{{9,15}})"
    r"(?P<tail>[^\s<>\"']*)",
    re.IGNORECASE,
)

# The classic work/school link:
#
#     https://teams.microsoft.com/l/meetup-join/19%3ameeting_…%40thread.v2/0?context=%7b…%7d
#
# It carries a thread id and a tenant rather than a meeting number, so there is nothing to
# rebuild it from and the matched text is carried as-is (trimmed). This is the shape an Outlook
# calendar invitation for a tenant meeting uses, which is why it is here at all: the numeric
# id in such an invite is printed in the body, not in the link.
_TEAMS_MEETUP_JOIN_RE = re.compile(
    rf"https?://{_TEAMS_HOST}/l/meetup-join/[^\s<>\"']+",
    re.IGNORECASE,
)

# ``?p=`` on a Teams join link. **Unlike Zoom's ``pwd=``, this one *is* the typed passcode** —
# it is the same string the invite prints on its ``Passcode:`` line, which is why reading it
# out of the URL is correct here and would be a bug there. ``&amp;`` is matched too, because a
# URL lifted out of an HTML part still carries the entity.
_TEAMS_URL_PASSCODE_RE = re.compile(
    r"(?:[?&]|&amp;)p=([^\s<>\"'&]{1,64})", re.IGNORECASE
)

# "Meeting ID: 281 442 953 617" as Teams prints it, in the grouping it prints it in.
#
# **Only ever consulted once a Teams link has already been found**, which is what keeps it
# from colliding with Zoom's identical label — see the module docstring. Its job is to supply
# the numeric id for a ``meetup-join`` link, which does not carry one.
_TEAMS_ID_TEXT_RE = re.compile(
    # The fullwidth colon is written as ``\uff1a`` for the reason every other pattern here
    # writes it that way: as a literal it is indistinguishable from an ASCII colon in source.
    r"meeting\s*id\s*[:\uff1a]?\s*((?:\d[\s\-]*){9,18})",
    re.IGNORECASE,
)

# What makes a body a Teams *invitation* rather than a message that mentions Teams — see
# ``has_teams_invite_block``.
_TEAMS_INVITE_MARKERS = (
    "join the meeting now",
    "microsoft teams",
    "meeting id",
    "passcode",
    "password",
)

# Trailing characters a URL picks up from the prose around it: a sentence's full stop, a
# closing bracket from "(join here: …)", a comma in a list. Stripped rather than excluded from
# the pattern because every one of them is legal *inside* a URL and only suspicious at the end.
_URL_TRAILING_JUNK = ".,;:!?)]}>'\""


# The labels Zoom and Teams put at the head of each block of their invitations. Used to put
# the line breaks back when a renderer has taken them out — see `restore_line_breaks`.
#
# One list rather than one per platform, because the repair is applied before anything knows
# which platform the text belongs to, and a label that does not occur simply never matches.
_BLOCK_LABELS = (
    # Zoom
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
    # Teams. ``Passcode:`` is the line that needs these: in a flattened Teams invitation the
    # block after it is "Dial in by phone", so without a break the passcode reads
    # ``aB3dE9Dial`` — a plausible-looking value that Teams rejects, which is the same silent
    # corruption the Zoom labels above exist to prevent.
    "microsoft teams",
    "need help?",
    "join the meeting now",
    "dial in by phone",
    "find a local number",
    "reset dial-in pin",
    "for organizers",
    "meeting options",
    "learn more",
)

_LABEL_BREAK_RE = re.compile(
    "(?<=\\S)(?=(?:" + "|".join(re.escape(label) for label in _BLOCK_LABELS) + "))",
    re.IGNORECASE,
)
_URL_BREAK_RE = re.compile(r"(?<=\S)(?=https?://)")
# ``---`` is Zoom's section rule; a run of underscores is Teams'. Both are places the format
# guarantees a break, which is the only property this pattern needs of them.
#
# **The lookbehind excludes the rule character itself, and that is not cosmetic.** Written as
# ``(?<=\S)``, every position *inside* the run also qualifies — there are still three more
# ahead and the character behind is non-space — so Teams' forty-underscore rule came back as
# forty separate lines. Zoom's ``---`` hid the flaw by being exactly three long.
_RULE_BREAK_RE = re.compile(r"(?<=[^\s\-])(?=-{3,})|(?<=[^\s_])(?=_{3,})")


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
    """``google_meet``, ``zoom_web`` or ``teams_web`` — the bridge's own platform identifier,
    sent verbatim."""

    meeting_number: str
    """Meet's ``abc-defg-hij`` code, or Zoom's or Teams' numeric meeting id with the spacing
    removed.

    For a Teams ``meetup-join`` link, which carries a thread id rather than a number and whose
    invite may not print one either, this is the join URL itself. The bridge accepts a Teams
    URL in ``meeting_number`` for exactly that reason (see ``CreateSessionRequest``), and it
    keeps the field non-empty, which the bridge requires."""

    passcode: str | None = None
    """The typed passcode, when the invite spelled it out. ``None`` for Meet, which has no
    equivalent, and for a Zoom invite that carried only an encrypted ``pwd=`` token."""

    url: str = ""
    """The link as it appeared, or — for the Teams short form — rebuilt from its parts.

    **Teams is the one platform where this is load-bearing rather than informational.** Meet
    and Zoom are joined by number; a Teams meeting id does not say which Teams it belongs to,
    so the bridge's joiner has to guess a form, wait for it to fail, and re-navigate. Handing
    it the URL skips all of that, which is why ``join_url_for`` sends this one on."""

    @property
    def is_zoom(self) -> bool:
        return self.platform == PLATFORM_ZOOM

    @property
    def is_teams(self) -> bool:
        return self.platform == PLATFORM_TEAMS


def find_meeting_link(*texts: str) -> MeetingLink | None:
    """The first joinable meeting named in ``texts``, or ``None``.

    **Each argument is searched in full before the next is looked at**, which is how a caller
    expresses precedence between sources: a calendar event passes its structured
    ``conferenceData`` first and its free-text description last, so a stale link in a copied
    agenda can never outrank the field Google filled in itself.

    Concatenating the sources first — which this did — quietly loses that. A Zoom event whose
    description still carried last week's Meet link would resolve to Meet, because Meet is
    tried before Zoom *within* a text and the two were no longer distinguishable once joined.

    Within one text Meet wins over Teams and Teams over Zoom, for the reasons in the module
    docstring: Meet's pattern is the strictest, and Zoom's is the only one that will read a
    meeting id out of bare prose — a fallback that would otherwise claim the identically
    labelled id line in a Teams invitation.

    ``None`` is an ordinary result rather than an error: most calendar events are not
    meetings and most mail is not an invite.
    """
    for raw in texts:
        if not raw:
            continue
        # Every pattern below ends at whitespace, so a flattened rendering must have its
        # boundaries restored first or they run past the values they are reading.
        text = restore_line_breaks(raw)
        link = _find_meet(text) or _find_teams(text) or _find_zoom(text)
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

    if not number and not _names_teams(text):
        # **Prose only as a last resort.** "Meeting ID:" is far looser than a URL path and
        # appears in plenty of mail that is not a Zoom invite; reaching this line means no
        # link of any kind was present, which for a real invite is already unusual.
        #
        # **And not at all when the text says Microsoft Teams.** Teams prints its id in the
        # identical ``Meeting ID: 281 442 953 617`` form, so a Teams invitation whose link a
        # mail client stripped would otherwise be read as a Zoom meeting number and dialled on
        # ``zoom_web`` — a meeting Zoom has never heard of, failing in a way that points at the
        # wrong platform entirely. Refusing costs a real Zoom invite nothing: those say "Zoom".
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


def _names_teams(text: str) -> bool:
    """Whether this text is talking about Microsoft Teams.

    Used only to hold Zoom's prose fallback back — never to *claim* a meeting for Teams, which
    always requires a link. A cheap, one-directional guard against a wrong-platform join.
    """
    folded = text.casefold()
    return "microsoft teams" in folded or "teams.live.com" in folded


def _find_teams(text: str) -> MeetingLink | None:
    """A Teams meeting, from either of the two link shapes Microsoft ships.

    **A link is required — there is no prose fallback here, deliberately.** Teams' printed
    ``Meeting ID:`` line is indistinguishable from Zoom's, so reading a Teams meeting out of
    prose alone would mean guessing which platform a bare number belongs to. The invites this
    feature is for always carry the link, so the ambiguous case is one nothing needs.
    """
    short = _TEAMS_MEET_URL_RE.search(text)
    if short:
        meeting_id = short.group("id")
        # ``p=`` first because it is on the link that names *this* meeting; the printed line is
        # the fallback for a renderer that dropped the query, or for the work/school short form
        # where the passcode is only ever printed.
        passcode = _teams_url_passcode(short.group("tail")) or find_teams_passcode(text)
        # **Rebuilt from the parts rather than carried.** The matched tail is whatever followed
        # the id in whichever rendering this text came from — an HTML entity, a tracking
        # parameter, a stray bracket — and the bridge navigates this string. The scheme, host,
        # id and passcode are everything a join needs, so anything else is noise that can only
        # break it.
        url = f"{short.group('scheme').lower()}://{short.group('host').lower()}/meet/{meeting_id}"
        if passcode:
            url = f"{url}?p={quote(passcode, safe='')}"
        return MeetingLink(
            platform=PLATFORM_TEAMS,
            meeting_number=meeting_id,
            passcode=passcode,
            url=url,
        )

    meetup = _TEAMS_MEETUP_JOIN_RE.search(text)
    if meetup is None:
        return None

    url = meetup.group(0).rstrip(_URL_TRAILING_JUNK)
    # The numeric id is printed in the body of a tenant invitation, not in the link. Safe to
    # read here where it would not be above: a ``meetup-join`` URL has already established
    # that this text is a Teams invitation, so the label cannot be Zoom's.
    number = _teams_prose_id(text)
    return MeetingLink(
        platform=PLATFORM_TEAMS,
        # The URL as the identifier when the body printed no id. It is what the bridge will
        # navigate anyway, and it keeps ``meeting_number`` non-empty — which the bridge
        # requires and which the join-deduplication keys on.
        meeting_number=number or url,
        passcode=find_teams_passcode(text),
        url=url,
    )


def _teams_url_passcode(tail: str) -> str | None:
    """The ``p=`` passcode from a Teams join link's query, or ``None``.

    ``unquote`` and deliberately not ``unquote_plus``: ``+`` means space only under *form*
    encoding, and this is a token typed into a text box rather than a submitted field. Teams
    does not put spaces in passcodes, so a literal ``+`` is by far the likelier reading and
    turning it into a space would produce a passcode the meeting rejects.
    """
    match = _TEAMS_URL_PASSCODE_RE.search(tail)
    if match is None:
        return None
    return unquote(match.group(1)).strip() or None


def _teams_prose_id(text: str) -> str | None:
    """The digits of a printed ``Meeting ID:`` line, or ``None``."""
    match = _TEAMS_ID_TEXT_RE.search(text)
    if match is None:
        return None
    digits = re.sub(r"\D", "", match.group(1))
    return digits if 9 <= len(digits) <= 15 else None


def has_teams_invite_block(*texts: str) -> bool:
    """Whether this text *is* a Teams invitation, rather than merely mentioning Teams.

    The Teams counterpart of ``has_zoom_invite_block``, and it exists for the same reason: a
    Teams meeting reaches the inbox with the *event's own title* as its subject, sent from the
    organiser's own mailbox, so neither the sender nor the subject carries any signal. The
    invite text is what is invariant.

    **Two ways to qualify, and the first has no equivalent on Zoom.** A Teams short link
    carries its passcode in the URL (``/meet/<id>?p=<passcode>``), and that string is not
    something that turns up in prose — it is what the *Copy link* button produces and it is
    the whole of what a join needs. So a passcode-bearing link is an invitation on its own.
    Otherwise the rule is Zoom's: a join link **plus** a labelled line from the block Teams
    generates (``Join the meeting now``, ``Meeting ID:``, ``Passcode:``).

    Deliberately not "there is a Teams link somewhere": a colleague writing *"we used to meet
    at teams.microsoft.com/l/meetup-join/…"* has a link and no invitation, and should not move
    the bot.
    """
    joined = restore_line_breaks("\n".join(text for text in texts if text))
    if not joined:
        return False

    short = _TEAMS_MEET_URL_RE.search(joined)
    if short is not None and _teams_url_passcode(short.group("tail")) is not None:
        return True
    if short is None and _TEAMS_MEETUP_JOIN_RE.search(joined) is None:
        return False

    folded = joined.casefold()
    return any(marker in folded for marker in _TEAMS_INVITE_MARKERS)


def find_teams_passcode(*texts: str) -> str | None:
    """The passcode a Teams participant would type, or ``None``.

    Only the printed ``Passcode:`` line, and no dial-in fallback: Teams' phone block carries a
    *conference id* and a dial-in PIN, neither of which is the meeting passcode, so reading a
    number out of it would produce a confident wrong answer where ``None`` is the honest one.

    ``None`` is normal. A meeting can have no passcode, and one whose invite carried only a
    ``meetup-join`` link needs none — that URL is its own credential.
    """
    joined = restore_line_breaks("\n".join(text for text in texts if text))
    if not joined:
        return None
    match = _PASSCODE_LINE_RE.search(joined)
    if match is None:
        return None
    return match.group(1).strip() or None


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
        _ZOOM_ID_LABEL_RE.search(joined) or _PASSCODE_LINE_RE.search(joined)
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

    match = _PASSCODE_LINE_RE.search(joined)
    if match:
        return match.group(1).strip() or None

    onetap = _ZOOM_ONETAP_PASSCODE_RE.search(joined)
    if onetap:
        return onetap.group(1)
    return None


def find_passcode(platform: str, *texts: str) -> str | None:
    """The typed passcode for ``platform``, or ``None``.

    A dispatcher so the two callers that already hold a resolved ``MeetingLink`` — the calendar
    poller and the invite parser, both of which sweep a second time for a passcode the link
    itself did not carry — do not each grow a chain of platform tests. Before this they had one
    apiece for Zoom, and adding Teams would have made that two apiece with nothing keeping them
    in step.

    ``None`` for Google Meet, which has no passcode concept, and for any platform not named
    here — the honest answer in both cases, and the same one a missing passcode gives.
    """
    if platform == PLATFORM_ZOOM:
        return find_zoom_passcode(*texts)
    if platform == PLATFORM_TEAMS:
        return find_teams_passcode(*texts)
    return None


def join_url_for(platform: str, url: str) -> str | None:
    """The join URL to send the bridge alongside the meeting number, or ``None``.

    **Only Teams gets one, and the asymmetry is the point.** Meet and Zoom are joined by
    number and the bridge documents ``meeting_url`` as ignored for them, so sending it would
    change a request that works today for no gain. Teams is the opposite: a bare meeting id
    does not say whether it belongs to a personal (``teams.live.com``) or a work/school
    (``teams.microsoft.com``) account, and the bridge's joiner resolves that by trying one form,
    waiting several polls for it to make no progress, and re-navigating to the other. Handing it
    the URL the invite actually contained turns that guess into a fact.

    Returning ``None`` rather than an empty string because that is what ``trigger_bot_join``
    tests to decide whether the key appears in the payload at all.
    """
    if platform != PLATFORM_TEAMS:
        return None
    return url.strip() or None
