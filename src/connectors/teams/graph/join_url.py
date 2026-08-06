"""Resolve a ``MeetingContext`` into a Graph join descriptor.

This is the Teams anti-corruption layer on the *outbound* side: it is the only place
that reads ``MeetingContext`` and speaks Graph, and the only place that knows what a
Teams join URL looks like.

Two routes, preferred in this order:

1. **Numeric meeting id + passcode** — what a Teams invite prints as "Meeting ID". It
   lands in the bridge's existing ``meeting_number`` and ``passcode`` fields, so an
   operator drives Teams through the identical ``POST /sessions`` shape they already
   use for Zoom. No new API surface, which is the whole reason to prefer it.

2. **Join URL** — ``platform_data["meeting_url"]``, parsed for the thread id and
   organizer. Needed because a calendar invite's link is often all anyone has.

A Teams join URL looks like::

    https://teams.microsoft.com/l/meetup-join/19%3ameeting_ABC123%40thread.v2/0
        ?context=%7b%22Tid%22%3a%22<tenant>%22%2c%22Oid%22%3a%22<organizer>%22%7d

so the thread id is a URL-encoded path segment and the tenant/organizer are inside a
URL-encoded JSON ``context`` parameter. Both layers of encoding are real and both are
handled below; the format is Microsoft's and is treated as a wire contract, which is
why every failure mode raises with the offending part named rather than returning
``None`` and failing three hops later.
"""

from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, unquote, urlparse

from src.connectors.teams.exceptions import JoinUrlError
from src.connectors.teams.graph.models import (
    ChatInfo,
    JoinMode,
    OrganizerIdentity,
    TeamsJoinDescriptor,
)
from src.domain.meeting import MeetingContext

_MEETUP_JOIN_PATH = re.compile(r"/l/meetup-join/(?P<thread>[^/?]+)(?:/(?P<message>[^/?]*))?")

_THREAD_ID = re.compile(r"^19:[^@]+@thread\.(v2|skype|tacv2)$")

_NUMERIC_MEETING_ID = re.compile(r"^\d[\d\s]{7,}$")
"""A Teams "Meeting ID" is 9-12 digits, commonly printed in space-separated groups.
Deliberately not a Zoom meeting-number check: the two happen to overlap in shape,
which is exactly why the *platform* is carried explicitly on the context rather than
being sniffed from the id."""

_MEETING_URL_KEY = "meeting_url"
"""The single ``platform_data`` key this connector reads. Zoom writes
``rtms_stream_id`` and ``signaling_url`` into the same dict for its own sessions;
neither connector may read the other's keys."""


def looks_like_join_url(value: str) -> bool:
    """True when ``value`` is a Teams meetup-join link rather than a meeting id."""
    return "meetup-join" in value


def normalise_meeting_id(raw: str) -> str:
    """Strip the presentation spacing out of a printed Teams meeting id."""
    return re.sub(r"\s+", "", raw)


def parse_join_url(url: str, *, display_name: str) -> TeamsJoinDescriptor:
    """Parse a Teams meetup-join URL into a ``CHAT_INFO`` descriptor.

    Raises:
        JoinUrlError: the URL is not a meetup-join link, or is missing the thread id,
            tenant, or organizer that Graph requires.
    """
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        raise JoinUrlError(f"not an absolute URL: {url!r}")

    path_match = _MEETUP_JOIN_PATH.search(unquote(parsed.path))
    if path_match is None:
        raise JoinUrlError(
            "URL does not contain a /l/meetup-join/<threadId> segment; "
            "it may be a Teams 'launcher' or short link rather than a join URL"
        )

    thread_id = unquote(path_match.group("thread"))
    if not _THREAD_ID.match(thread_id):
        raise JoinUrlError(f"malformed Teams thread id: {thread_id!r}")

    # The path segment after the thread id is the message id. Graph accepts "0" for
    # "the meeting itself", which is what a join link normally carries.
    message_id = path_match.group("message") or "0"
    message_id = unquote(message_id) or "0"

    context = _parse_context(parsed.query)
    tenant_id = _require(context, ("Tid", "tid"), "tenant id (context.Tid)")
    organizer_id = _require(context, ("Oid", "oid"), "organizer id (context.Oid)")

    return TeamsJoinDescriptor(
        mode=JoinMode.CHAT_INFO,
        tenant_id=tenant_id,
        display_name=display_name,
        chat_info=ChatInfo(thread_id=thread_id, message_id=message_id),
        organizer=OrganizerIdentity(id=organizer_id, tenant_id=tenant_id),
    )


def resolve_join_descriptor(
    meeting: MeetingContext, *, tenant_id: str, display_name: str | None = None
) -> TeamsJoinDescriptor:
    """Resolve how to join the meeting described by ``meeting``.

    Args:
        meeting: The session's meeting context.
        tenant_id: Configured tenant, used for the meeting-id route and as the
            fallback when a join URL omits its own.
        display_name: Overrides ``meeting.display_name`` when supplied.

    Raises:
        JoinUrlError: neither a usable meeting id nor a parseable join URL is present.
    """
    name = display_name or meeting.display_name
    number = (meeting.meeting_number or "").strip()
    url = str(meeting.platform_data.get(_MEETING_URL_KEY) or "").strip()

    # A join URL supplied in the meeting-number field is a natural operator mistake;
    # accept it rather than rejecting a request that carries everything we need.
    if not url and looks_like_join_url(number):
        url, number = number, ""

    if number and not looks_like_join_url(number):
        candidate = normalise_meeting_id(number)
        if _NUMERIC_MEETING_ID.match(number) or candidate.isdigit():
            return TeamsJoinDescriptor(
                mode=JoinMode.MEETING_ID,
                tenant_id=tenant_id,
                display_name=name,
                join_meeting_id=candidate,
                passcode=meeting.passcode or None,
            )

    if url:
        descriptor = parse_join_url(url, display_name=name)
        # A URL always carries its own tenant; fall back only if it somehow did not.
        if not descriptor.tenant_id:
            return descriptor.model_copy(update={"tenant_id": tenant_id})
        return descriptor

    raise JoinUrlError(
        "cannot resolve a Teams join: supply either a numeric meeting id in "
        "meeting_number (with passcode if the meeting has one) or a meetup-join URL "
        "in meeting_url"
    )


def _parse_context(query: str) -> dict[str, str]:
    """Decode the URL-encoded JSON ``context`` query parameter."""
    values = parse_qs(query).get("context") or []
    if not values:
        raise JoinUrlError("join URL has no context parameter (tenant and organizer)")
    try:
        decoded = json.loads(unquote(values[0]))
    except (ValueError, TypeError) as exc:
        raise JoinUrlError(f"join URL context is not valid JSON: {exc}") from exc
    if not isinstance(decoded, dict):
        raise JoinUrlError("join URL context is not a JSON object")
    return {str(k): str(v) for k, v in decoded.items() if v is not None}


def _require(context: dict[str, str], keys: tuple[str, ...], label: str) -> str:
    for key in keys:
        value = context.get(key)
        if value:
            return value
    raise JoinUrlError(f"join URL context is missing the {label}")
