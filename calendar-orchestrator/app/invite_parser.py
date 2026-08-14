"""Decides whether one Gmail message is a live "join now" invite, and pulls the Meet code out.

Pure functions over the Gmail message dict — no network, no state. That is what makes the
risky part of this feature (does this email really mean "join a meeting"?) directly testable
against captured message payloads.

The filter is strict and ordered:

1. ``From:`` must be exactly an address in ``allowed_senders``. The Gmail query already
   restricts this, but it is re-checked here because the query is a performance filter and
   this is a *security* one — everything downstream puts a bot into a meeting.
2. The subject must contain one of ``subject_markers``. Meet sends plenty of other mail from
   the same address (recording ready, missed call) that must not trigger a join.
3. The body must contain a well-formed Meet code.
"""

from __future__ import annotations

import base64
import binascii
import logging
import quopri
import re
from dataclasses import dataclass
from email.utils import parseaddr

from app.config import GmailSettings

logger = logging.getLogger(__name__)

# A Meet code is three groups of letters: abc-defg-hij.
#
# Note this is stricter than `meet\.google\.com/([a-z-]+)`. The loose form also matches the
# ordinary links Google puts in the footer of these very emails — meet.google.com/support,
# /landing, /new — and each one would be handed to the bridge as a meeting number, sending
# the bot off to dial a meeting that does not exist.
#
# Same pattern as `calendar_service._MEET_CODE_RE`, so both paths agree on what a code is.
_MEET_CODE_RE = re.compile(r"meet\.google\.com/([a-z]+-[a-z]+-[a-z]+)")

# Meet codes are 3-4-3 letters. Used only to rank candidates, never to reject: Google has
# changed code shapes before, and refusing a real meeting because its code had an unexpected
# length would be a worse failure than joining one.
_CANONICAL_CODE_RE = re.compile(r"^[a-z]{3}-[a-z]{4}-[a-z]{3}$")


@dataclass(frozen=True, slots=True)
class InstantInvite:
    """A Meet invite email that warrants putting the bot in a meeting."""

    message_id: str
    thread_id: str
    sender: str
    subject: str
    meeting_code: str
    internal_date_ms: int
    """Gmail's own receive timestamp, used to ignore invites too old to still be live."""

    @property
    def meeting_url(self) -> str:
        return f"https://meet.google.com/{self.meeting_code}"


def parse_invite(message: dict, settings: GmailSettings) -> InstantInvite | None:
    """Return an ``InstantInvite`` if this message is one, else ``None``.

    ``None`` is a normal result — most mail is not a Meet invite — so it is logged at debug,
    not raised.
    """
    message_id = message.get("id", "")
    headers = _headers(message.get("payload", {}))
    sender = _sender_address(headers.get("from", ""))
    subject = headers.get("subject", "")

    allowed = {address.casefold() for address in settings.allowed_senders}
    if sender.casefold() not in allowed:
        logger.debug("message %s: sender %r not allow-listed", message_id, sender)
        return None

    if not _subject_matches(subject, settings.subject_markers):
        logger.debug("message %s: subject %r matches no marker", message_id, subject)
        return None

    code = _extract_meeting_code(message)
    if code is None:
        # Worth a warning rather than debug: sender and subject both said "invite", so a
        # missing code means Meet changed its template and the feature is quietly dead.
        logger.warning(
            "message %s from %s looked like an invite (%r) but carried no Meet link",
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
        meeting_code=code,
        internal_date_ms=_internal_date_ms(message),
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


def _subject_matches(subject: str, markers: tuple[str, ...]) -> bool:
    folded = subject.casefold()
    return any(marker.casefold() in folded for marker in markers)


def _extract_meeting_code(message: dict) -> str | None:
    """Find the Meet code in the message body, preferring canonical-looking codes.

    Searches every text part plus the snippet. When several distinct codes appear — the
    invite itself plus a quoted earlier thread, say — a canonically shaped one wins, and
    otherwise the first hit does.
    """
    candidates: list[str] = []
    for text in _iter_text(message.get("payload", {})):
        candidates.extend(_MEET_CODE_RE.findall(text))
    # The snippet is plain text Gmail has already extracted; a useful backstop when the body
    # is in an encoding this parser could not decode.
    candidates.extend(_MEET_CODE_RE.findall(message.get("snippet", "")))

    if not candidates:
        return None
    for code in candidates:
        if _CANONICAL_CODE_RE.match(code):
            return code
    return candidates[0]


def _iter_text(payload: dict):
    """Yield the decoded text of every text-ish part, walking nested multiparts.

    Both ``text/plain`` and ``text/html`` are read. Meet's invite mail is multipart and the
    join link is reliably in both, but which parts exist has changed over time, so reading
    everything is more durable than betting on one.
    """
    mime_type = str(payload.get("mimeType", ""))
    data = payload.get("body", {}).get("data")

    if data and (mime_type.startswith("text/") or not payload.get("parts")):
        decoded = _decode_body(data)
        if decoded:
            yield decoded

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
