"""Teams join resolution.

The outbound anti-corruption boundary. A join URL is Microsoft's wire format with two
layers of URL encoding, so the failure modes below are all real ones seen in calendar
invites rather than invented edge cases.
"""

from __future__ import annotations

import json
from urllib.parse import quote

import pytest

from src.connectors.teams.exceptions import JoinUrlError
from src.connectors.teams.graph.join_url import (
    looks_like_join_url,
    normalise_meeting_id,
    parse_join_url,
    resolve_join_descriptor,
)
from src.connectors.teams.graph.models import JoinMode
from src.domain.meeting import MeetingContext, MeetingPlatform

TENANT = "72f988bf-86f1-41af-91ab-2d7cd011db47"
ORGANIZER = "8b081ef6-4792-4def-b2c9-c363a1bf41d5"
THREAD = "19:meeting_NTg3MjZhOTYtMTIzNC00NTY3@thread.v2"


def _join_url(
    *,
    thread: str = THREAD,
    tenant: str | None = TENANT,
    organizer: str | None = ORGANIZER,
    message: str = "0",
) -> str:
    context: dict[str, str] = {}
    if tenant is not None:
        context["Tid"] = tenant
    if organizer is not None:
        context["Oid"] = organizer
    encoded_context = quote(json.dumps(context))
    return (
        f"https://teams.microsoft.com/l/meetup-join/{quote(thread, safe='')}"
        f"/{message}?context={encoded_context}"
    )


def _teams_meeting(
    *, number: str = "", url: str | None = None, passcode: str | None = None
) -> MeetingContext:
    platform_data = {"meeting_url": url} if url else {}
    return MeetingContext(
        meeting_number=number,
        display_name="AI Avatar",
        passcode=passcode,
        platform_data=platform_data,
        platform=MeetingPlatform.TEAMS,
    )


# --------------------------------------------------------------------------- #
# URL parsing
# --------------------------------------------------------------------------- #


def test_parses_a_realistic_join_url() -> None:
    descriptor = parse_join_url(_join_url(), display_name="AI Avatar")

    assert descriptor.mode is JoinMode.CHAT_INFO
    assert descriptor.tenant_id == TENANT
    assert descriptor.chat_info is not None
    assert descriptor.chat_info.thread_id == THREAD
    assert descriptor.chat_info.message_id == "0"
    assert descriptor.organizer is not None
    assert descriptor.organizer.id == ORGANIZER
    assert descriptor.organizer.tenant_id == TENANT


def test_thread_id_survives_double_url_encoding() -> None:
    """The thread id contains ``:`` and ``@``, both percent-encoded in a real invite.
    Decoding one layer too few leaves ``19%3ameeting_...`` and Graph rejects the join."""
    descriptor = parse_join_url(_join_url(), display_name="x")
    assert descriptor.chat_info is not None
    assert descriptor.chat_info.thread_id.startswith("19:meeting_")
    assert "%" not in descriptor.chat_info.thread_id


def test_accepts_lowercase_context_keys() -> None:
    """Teams has emitted both ``Tid``/``Oid`` and ``tid``/``oid``."""
    context = quote(json.dumps({"tid": TENANT, "oid": ORGANIZER}))
    url = (
        f"https://teams.microsoft.com/l/meetup-join/{quote(THREAD, safe='')}"
        f"/0?context={context}"
    )
    descriptor = parse_join_url(url, display_name="x")
    assert descriptor.tenant_id == TENANT


def test_accepts_thread_v2_and_skype_suffixes() -> None:
    for suffix in ("thread.v2", "thread.skype", "thread.tacv2"):
        thread = f"19:meeting_abc@{suffix}"
        descriptor = parse_join_url(_join_url(thread=thread), display_name="x")
        assert descriptor.chat_info is not None
        assert descriptor.chat_info.thread_id == thread


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("not-a-url", "not an absolute URL"),
        ("https://teams.microsoft.com/l/chat/0/0", "meetup-join"),
    ],
)
def test_rejects_urls_that_are_not_join_links(url: str, expected: str) -> None:
    with pytest.raises(JoinUrlError, match=expected):
        parse_join_url(url, display_name="x")


def test_rejects_a_malformed_thread_id() -> None:
    url = _join_url(thread="not-a-thread-id")
    with pytest.raises(JoinUrlError, match="malformed Teams thread id"):
        parse_join_url(url, display_name="x")


def test_rejects_a_url_with_no_context() -> None:
    url = f"https://teams.microsoft.com/l/meetup-join/{quote(THREAD, safe='')}/0"
    with pytest.raises(JoinUrlError, match="no context parameter"):
        parse_join_url(url, display_name="x")


def test_rejects_a_context_missing_the_organizer() -> None:
    with pytest.raises(JoinUrlError, match="organizer id"):
        parse_join_url(_join_url(organizer=None), display_name="x")


def test_rejects_a_context_missing_the_tenant() -> None:
    with pytest.raises(JoinUrlError, match="tenant id"):
        parse_join_url(_join_url(tenant=None), display_name="x")


def test_rejects_a_context_that_is_not_json() -> None:
    url = (
        f"https://teams.microsoft.com/l/meetup-join/{quote(THREAD, safe='')}"
        "/0?context=not-json"
    )
    with pytest.raises(JoinUrlError, match="not valid JSON"):
        parse_join_url(url, display_name="x")


# --------------------------------------------------------------------------- #
# Descriptor resolution
# --------------------------------------------------------------------------- #


def test_numeric_meeting_id_is_preferred() -> None:
    """The route that reuses the existing API shape: an operator drives Teams and Zoom
    through the identical request body."""
    descriptor = resolve_join_descriptor(
        _teams_meeting(number="123456789012", passcode="abc123"), tenant_id=TENANT
    )

    assert descriptor.mode is JoinMode.MEETING_ID
    assert descriptor.join_meeting_id == "123456789012"
    assert descriptor.passcode == "abc123"
    assert descriptor.tenant_id == TENANT


def test_printed_meeting_id_spacing_is_stripped() -> None:
    """Teams prints "123 456 789 012". Pasting it verbatim must work."""
    descriptor = resolve_join_descriptor(
        _teams_meeting(number="123 456 789 012"), tenant_id=TENANT
    )
    assert descriptor.join_meeting_id == "123456789012"


def test_falls_back_to_the_join_url() -> None:
    descriptor = resolve_join_descriptor(_teams_meeting(url=_join_url()), tenant_id=TENANT)
    assert descriptor.mode is JoinMode.CHAT_INFO


def test_a_join_url_in_the_meeting_number_field_is_accepted() -> None:
    """A natural operator mistake, and everything needed is present — rejecting the
    request would be pedantry rather than safety."""
    descriptor = resolve_join_descriptor(_teams_meeting(number=_join_url()), tenant_id=TENANT)
    assert descriptor.mode is JoinMode.CHAT_INFO
    assert descriptor.chat_info is not None


def test_empty_passcode_becomes_none() -> None:
    """``exclude_none`` then omits the key entirely, which is how Graph is told "no
    passcode" rather than "an empty one"."""
    descriptor = resolve_join_descriptor(
        _teams_meeting(number="123456789012", passcode=""), tenant_id=TENANT
    )
    assert descriptor.passcode is None
    assert "passcode" not in descriptor.to_wire()


def test_rejects_a_meeting_with_neither_id_nor_url() -> None:
    with pytest.raises(JoinUrlError, match="cannot resolve a Teams join"):
        resolve_join_descriptor(_teams_meeting(), tenant_id=TENANT)


def test_display_name_override_wins() -> None:
    descriptor = resolve_join_descriptor(
        _teams_meeting(number="123456789012"), tenant_id=TENANT, display_name="Override"
    )
    assert descriptor.display_name == "Override"


# --------------------------------------------------------------------------- #
# Wire serialisation
# --------------------------------------------------------------------------- #


def test_wire_form_is_camel_case_for_graph() -> None:
    descriptor = resolve_join_descriptor(_teams_meeting(url=_join_url()), tenant_id=TENANT)
    wire = descriptor.to_wire()

    assert wire["tenantId"] == TENANT
    assert wire["displayName"] == "AI Avatar"
    assert wire["chatInfo"]["threadId"] == THREAD
    assert wire["chatInfo"]["messageId"] == "0"
    assert wire["organizer"]["id"] == ORGANIZER
    # No snake_case leaks into a payload Graph will read.
    assert not any("_" in key for key in wire)


def test_wire_form_omits_absent_optional_fields() -> None:
    descriptor = resolve_join_descriptor(_teams_meeting(number="123456789012"), tenant_id=TENANT)
    wire = descriptor.to_wire()

    assert wire["joinMeetingId"] == "123456789012"
    assert "chatInfo" not in wire
    assert "organizer" not in wire


def test_descriptor_validation_rejects_a_half_populated_chat_info_route() -> None:
    from src.connectors.teams.graph.models import TeamsJoinDescriptor

    with pytest.raises(ValueError, match="chatInfo and organizer"):
        TeamsJoinDescriptor(
            mode=JoinMode.CHAT_INFO, tenant_id=TENANT, display_name="x"
        )


def test_descriptor_validation_rejects_a_meeting_id_route_with_no_id() -> None:
    from src.connectors.teams.graph.models import TeamsJoinDescriptor

    with pytest.raises(ValueError, match="joinMeetingId"):
        TeamsJoinDescriptor(
            mode=JoinMode.MEETING_ID, tenant_id=TENANT, display_name="x"
        )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def test_looks_like_join_url() -> None:
    assert looks_like_join_url(_join_url())
    assert not looks_like_join_url("123456789012")


def test_normalise_meeting_id() -> None:
    assert normalise_meeting_id("123 456 789 012") == "123456789012"
    assert normalise_meeting_id("123456789012") == "123456789012"
