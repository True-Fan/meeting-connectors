"""Decides whether one Gmail message is a live "join now" invite, and pulls the meeting out.

Pure functions over the Gmail message dict — no network, no state. That is what makes the
risky part of this feature (does this email really mean "join a meeting"?) directly testable
against captured message payloads.

**Every message must name a meeting** — a Meet code, a Zoom join link or a Teams join link,
found by ``meeting_link``. That is the one universal step, and it is also where the *platform*
is decided, which is why neither Zoom nor Teams needed a second poller, a second state file or
a second code path: an invite is an invite, and only the link's shape varies.

What differs is how a message earns the right to be read at all, and there are three routes
because the invite shapes differ in what can be trusted about them:

1. **A calendar invitation**, identified by its ``invite.ics`` part. Google sends these *as
   the organiser*, so the ``From:`` is a personal address and the subject is the event's own
   title in whatever language it was written — neither is a handle. The ics is a structural
   fact instead, and it carries the event's *timing*, which is what makes it safe: only a
   meeting already in progress is acted on, and anything scheduled is left to the calendar
   poller (``ics.is_happening_now``).
2. **An in-meeting Zoom invite**, identified by its fixed subject. Composed from the host's
   own mailbox, so again there is no address to allow-list.
3. **An invitation body**, identified by the block the platform generates — a join link plus
   its labelled ``Meeting ID:`` / ``Passcode:`` lines. This is the route for a meeting pasted
   into a message or added to a calendar event, where the subject is the *event's* title and
   the sender is the organiser's own mailbox, so neither says anything. It is the only route
   Teams has that does not depend on knowing the organiser in advance.
4. **Everything else**, which is sender-gated as it always was: ``From:`` must match
   ``allowed_senders`` *and* the subject must contain a marker. The Gmail query already
   restricts the sender, but it is re-checked here because the query is a performance filter
   and this is a *security* one.

Routes 1-3 accept arbitrary senders by design, and each is switchable — see
``GmailSettings.accept_calendar_invitations``, ``any_sender_subject_markers``,
``accept_zoom_invite_bodies`` and ``accept_teams_invite_bodies`` for what that costs.
"""

from __future__ import annotations

import base64
import binascii
import logging
import quopri
from dataclasses import dataclass, replace
from datetime import timedelta
from email.utils import parseaddr

from app.config import GmailSettings
from app.ics import is_happening_now, is_invitation, unfold
from app.meeting_link import (
    PLATFORM_GOOGLE_MEET,
    MeetingLink,
    find_meeting_link,
    find_passcode,
    has_teams_invite_block,
    has_zoom_invite_block,
)

logger = logging.getLogger(__name__)

# The Meet code pattern used to be defined here *and* in ``calendar_service``, with a comment
# on each saying the two had to agree. Both now call ``meeting_link``, which also taught this
# path to recognise Zoom without either file gaining a second pattern to keep in step.


@dataclass(frozen=True, slots=True)
class InstantInvite:
    """An invite email that warrants putting the bot in a meeting."""

    message_id: str
    thread_id: str
    sender: str
    subject: str
    meeting_code: str
    internal_date_ms: int
    """Gmail's own receive timestamp, used to ignore invites too old to still be live."""
    platform: str = PLATFORM_GOOGLE_MEET
    """Which bridge connector serves it. Defaulted so every existing construction of this
    type — including in tests written before Zoom — keeps meaning what it meant."""
    passcode: str | None = None
    """Zoom's typed passcode when the invite spelled it out; always ``None`` for Meet."""
    url: str = ""
    """The link as it appeared in the mail. Empty for an invite matched by meeting id alone."""

    @property
    def meeting_url(self) -> str:
        """The join URL, reconstructed for Meet and taken as-is for anything else.

        Meet's is derived rather than stored because a Meet code *is* the URL, and deriving
        it was this property's whole job before other platforms existed. A Zoom link cannot
        be rebuilt from its number — the subdomain is part of it — so that one is carried.
        """
        if self.platform == PLATFORM_GOOGLE_MEET:
            return f"https://meet.google.com/{self.meeting_code}"
        return self.url


def parse_invite(message: dict, settings: GmailSettings) -> InstantInvite | None:
    """Return an ``InstantInvite`` if this message is one, else ``None``.

    ``None`` is a normal result — most mail is not a Meet invite — so it is logged at debug,
    not raised.
    """
    message_id = message.get("id", "")
    headers = _headers(message.get("payload", {}))
    sender = _sender_address(headers.get("from", ""))
    subject = headers.get("subject", "")

    # **Three routes in, and two of them exist because the sender is unknowable.** Checked
    # before the sender, because each can decide whether the sender matters at all.
    #
    #   1. a calendar invitation, recognised by its ``invite.ics`` part — Google sends these
    #      *as the organiser*, so the address is a personal one and the subject is the event's
    #      own title, in whatever language the organiser wrote it;
    #   2. an in-meeting Zoom invite, recognised by its fixed subject — composed from the
    #      host's mailbox, so again no address to allow-list;
    #   3. an invitation body, recognised by the block Zoom or Teams generates;
    #   4. everything else, which is still sender-gated exactly as it always was.
    reason = _open_route(message, subject, settings)
    if reason is None:
        if not _subject_matches(subject, settings.subject_markers):
            logger.debug("message %s: subject %r matches no marker", message_id, subject)
            return None
        if not sender_allowed(sender, settings.allowed_senders):
            logger.debug("message %s: sender %r not allow-listed", message_id, sender)
            return None
    else:
        # Logged at info, not debug: these are the paths where an arbitrary sender moved the
        # bot, so they should be visible in an ordinary log without anyone enabling anything.
        logger.info(
            "message %s: accepting %r from %s — %s",
            message_id,
            subject,
            sender,
            reason,
        )

    link = _extract_meeting_link(message)
    if link is None:
        # Worth a warning rather than debug: sender and subject both said "invite", so a
        # missing link means the platform changed its template and the feature is quietly
        # dead — which otherwise looks exactly like nobody having sent an invite.
        logger.warning(
            "message %s from %s looked like an invite (%r) but carried no joinable link",
            message_id,
            sender,
            subject,
        )
        return None

    return InstantInvite(
        message_id=message_id,
        thread_id=message.get("threadId", ""),
        sender=sender,
        subject=subject,
        meeting_code=link.meeting_number,
        internal_date_ms=_internal_date_ms(message),
        platform=link.platform,
        passcode=link.passcode,
        url=link.url,
    )


def _internal_date_ms(message: dict) -> int:
    """Gmail's ``internalDate`` (epoch milliseconds, as a string) -> int.

    Preferred over the ``Date:`` header, which is set by the sender and can be wrong or
    absent; ``internalDate`` is Gmail's own record of when the message arrived.
    """
    try:
        return int(message.get("internalDate", 0))
    except (TypeError, ValueError):
        return 0


def _headers(payload: dict) -> dict[str, str]:
    """Header name -> value, names lowercased. Later duplicates lose, matching how a mail
    client resolves a repeated ``Subject:``."""
    result: dict[str, str] = {}
    for header in payload.get("headers", []) or ():
        name = str(header.get("name", "")).casefold()
        if name and name not in result:
            result[name] = str(header.get("value", ""))
    return result


def _sender_address(from_header: str) -> str:
    """Bare address out of a ``From:`` header.

    Meet sends ``"Google Meet" <meetings-noreply@google.com>``, so a substring test against
    the raw header would be trivially spoofable — a display name reading
    ``meetings-noreply@google.com`` is something anyone can set. ``parseaddr`` takes the
    actual address.
    """
    return parseaddr(from_header)[1].strip()


def _open_route(message: dict, subject: str, settings: GmailSettings) -> str | None:
    """Why this message may be acted on without an allow-listed sender, or ``None``.

    Returns the human-readable reason so the caller can log *which* route let a message
    through — with three of them and all accepting arbitrary senders, "it was accepted" is
    not enough to audit after the fact.
    """
    calendar = (
        find_calendar_text(message.get("payload", {}))
        if settings.accept_calendar_invitations
        else None
    )

    # **An ics, when there is one, is the whole decision — including the decision to
    # refuse.** It is the only part of a message that says *when* the meeting is, so nothing
    # below is allowed to overrule it: a body-signature match on a scheduled invitation would
    # walk straight past the timing gate and join six days early, which is precisely the bug
    # that gate exists for.
    if calendar is not None:
        if not is_invitation(calendar):
            logger.debug("ics is a cancellation or a reply, not an invitation; ignoring")
            return None
        if not is_happening_now(
            calendar, lead=timedelta(seconds=settings.calendar_invite_lead_s)
        ):
            logger.debug(
                "calendar invitation %r is not happening now; leaving it to the "
                "calendar poller",
                subject,
            )
            return None
        return "calendar invitation for a meeting happening now"

    if _subject_matches(subject, settings.any_sender_subject_markers):
        return "subject is an in-meeting invite marker"

    # **The body, for everything the sender and the subject cannot identify.** A Zoom meeting
    # added to a calendar event carries the *event's* title as its subject — whatever the
    # organiser typed — and comes from their own mailbox, so neither field holds any signal.
    # The invite block does: a join link plus a labelled ``Meeting ID:`` or ``Passcode:``
    # line, which is what Zoom generates and what survives being pasted, forwarded or
    # reformatted.
    #
    # Teams is read from the body for the same reason and with one addition of its own: its
    # short link carries the passcode in the URL, so a bare link with ``?p=`` is already the
    # whole of an invitation. See ``has_teams_invite_block``.
    texts = _message_text(message)
    if settings.accept_zoom_invite_bodies and has_zoom_invite_block(*texts):
        return "body contains a Zoom invitation block"
    if settings.accept_teams_invite_bodies and has_teams_invite_block(*texts):
        return "body contains a Teams invitation block"
    return None


def sender_allowed(sender: str, allowed: tuple[str, ...]) -> bool:
    """Whether mail from ``sender`` may put the bot in a meeting.

    **This is the security boundary of the whole feature**, so it is worth being explicit
    about what each entry form grants. Three are accepted, and they widen in that order:

    * ``someone@example.com`` — that one mailbox. What Meet's system sender needs.
    * ``@example.com`` — anybody at that domain. What an **in-meeting Zoom invite** needs:
      clicking *Invite → Email* in a running meeting composes the mail from the *host's own
      mailbox*, so it arrives from a colleague rather than from Zoom, and there is no system
      address to allow-list. A company domain is the proportionate answer — it grants the
      people who already share your Workspace, and nobody else.
    * ``*`` — anybody at all. **Read the next paragraph before using it.**

    With ``*`` the only remaining filter is the subject marker and the presence of a link,
    and a subject is something any sender chooses freely. It therefore means *anyone who can
    email the bot can put it into any meeting they name*, including a stranger who guesses
    the address. That is a legitimate configuration for a bot whose address is not published
    — a local test mailbox, a private alias — and a bad one for anything reachable from
    outside. It is spelled ``*`` rather than a ``allow_any: true`` flag precisely so it shows
    up in a diff of the sender list, next to the addresses it is replacing.

    Matching is on the **parsed** address, never the raw header, because a display name
    reading ``no-reply@zoom.us`` is something anyone can set — see ``_sender_address``.
    """
    address = sender.strip().casefold()
    if not address:
        return False
    domain = address.rpartition("@")[2]

    for raw_entry in allowed:
        entry = raw_entry.strip().casefold()
        if not entry:
            continue
        if entry == "*":
            return True
        if entry.startswith("@"):
            if domain and domain == entry[1:]:
                return True
        elif entry == address:
            return True
    return False


def _subject_matches(subject: str, markers: tuple[str, ...]) -> bool:
    folded = subject.casefold()
    return any(marker.casefold() in folded for marker in markers)


def _extract_meeting_link(message: dict) -> MeetingLink | None:
    """Find the meeting named in the message body, on whichever platform hosts it.

    Every text part is read, plus the snippet — plain text Gmail has already extracted, and a
    useful backstop when the body is in an encoding this parser could not decode.

    **The parts are searched as one text rather than one at a time**, which is the opposite of
    what ``calendar_service`` does and is right for the opposite reason. A calendar event's
    fields differ in authority, so their order expresses precedence. A mail's parts do not:
    ``text/plain`` and ``text/html`` are two renderings of the same invite, and a passcode
    that appears in one may be formatted out of the other. Joining them means a link found in
    the HTML can still be paired with a passcode that survived only in the plain text.
    """
    body = "\n".join(_message_text(message))

    link = find_meeting_link(body)
    if link is None or link.passcode is not None:
        return link

    # A second sweep for the passcode alone. Zoom's mail puts the join link in a block of its
    # own and the passcode several lines below it, Teams does the same, and some senders strip
    # one part or the other — so the link matching in one rendering and the passcode in another
    # is ordinary rather than exceptional. ``find_passcode`` answers ``None`` for Meet, which
    # has no passcode to look for, so the original path reaches the same result it always did.
    passcode = find_passcode(link.platform, body)
    return link if passcode is None else replace(link, passcode=passcode)


def _message_text(message: dict) -> list[str]:
    """Every readable text of a message: its parts, then Gmail's own snippet.

    Factored out because two callers need exactly the same view and must not drift: the
    route that decides *whether* a body looks like an invitation, and the extraction that
    then reads the meeting out of it. A body signature matched against one text and a link
    pulled from another would be a message accepted on evidence it does not contain.

    The snippet is plain text Gmail has already extracted — a useful backstop when the body
    is in an encoding this parser could not decode.
    """
    parts = list(_iter_text(message.get("payload", {})))
    parts.append(str(message.get("snippet", "")))
    return [part for part in parts if part]


def find_calendar_text(payload: dict) -> str | None:
    """The decoded, unfolded ``text/calendar`` part, or ``None`` if there is not one.

    Its presence is what identifies a Google Calendar invitation — a structural fact rather
    than a string somebody chose, which is the whole reason it is preferred to matching the
    subject. Google attaches it as ``invite.ics``; some senders label the part
    ``application/ics``, so both are accepted.
    """
    mime_type = str(payload.get("mimeType", "")).lower()
    filename = str(payload.get("filename", "")).lower()
    data = payload.get("body", {}).get("data")

    if data and (
        mime_type.startswith("text/calendar")
        or (mime_type in ("application/ics", "application/octet-stream")
        and filename.endswith(".ics"))
    ):
        decoded = _decode_body(data)
        if decoded and "BEGIN:VCALENDAR" in decoded:
            return unfold(decoded)

    for part in payload.get("parts", []) or ():
        found = find_calendar_text(part)
        if found is not None:
            return found
    return None


def _iter_text(payload: dict):
    """Yield the decoded text of every text-ish part, walking nested multiparts.

    ``text/plain``, ``text/html`` and ``text/calendar`` are all read. Meet's invite mail is
    multipart and the join link is reliably in both text parts, but which parts exist has
    changed over time, so reading everything is more durable than betting on one.

    **The calendar part is unfolded before it is yielded**, and skipping that quietly corrupts
    the link. iCalendar wraps at 75 octets with a continuation line starting with a space, and
    a Zoom join URL is longer than that — so a raw read truncates it mid-token. The meeting
    *number* usually survives, because it sits near the front of the URL, which is what makes
    this failure so easy to miss: the bot joins the right meeting with a link that is wrong,
    and any later use of that URL fails for no visible reason.
    """
    mime_type = str(payload.get("mimeType", ""))
    data = payload.get("body", {}).get("data")

    if data and (mime_type.startswith("text/") or not payload.get("parts")):
        decoded = _decode_body(data)
        if decoded:
            yield unfold(decoded) if "BEGIN:VCALENDAR" in decoded else decoded

    for part in payload.get("parts", []) or ():
        yield from _iter_text(part)


def _decode_body(data: str) -> str:
    """base64url -> text, tolerating the two things that routinely break naive decoding."""
    try:
        # Gmail omits base64 padding; "==" is always enough and never harmful, since
        # b64decode ignores surplus padding.
        raw = base64.urlsafe_b64decode(data + "==")
    except (binascii.Error, ValueError) as exc:
        logger.debug("could not base64-decode a message part: %s", exc)
        return ""

    return _undo_quoted_printable(raw.decode("utf-8", errors="replace"))


def _undo_quoted_printable(text: str) -> str:
    """Undo quoted-printable if the part still carries it.

    This matters more than it looks. QP wraps lines at 76 characters with a trailing ``=``
    soft break, and a wrapped Meet URL becomes ``meet.google.com/abc-de=\\nfg-hij`` — which
    the code regex does not match, so a perfectly valid invite silently yields no meeting.
    Gmail usually applies the transfer decoding itself, hence the sniff rather than an
    unconditional decode.
    """
    if "=3D" not in text and "=\n" not in text and "=\r\n" not in text:
        return text
    try:
        return quopri.decodestring(text.encode("utf-8", errors="replace")).decode(
            "utf-8", errors="replace"
        )
    except (ValueError, binascii.Error):
        return text
