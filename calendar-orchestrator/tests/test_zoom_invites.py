"""Zoom arriving by the two routes this service already had, and what reaches the bridge.

Three seams, because a Zoom meeting can fail to be joined at three different places and each
one looks the same from outside (the bot simply never turns up):

* the **invite email** — a direct "join now" invite, the case the calendar structurally
  cannot see;
* the **calendar event** — a Zoom meeting scheduled through the Google Calendar add-on;
* the **bridge request** — the platform and passcode actually sent.

The Google Meet equivalents of the first two are covered by ``test_invite_parser`` and are
deliberately re-asserted here where they share a code path with Zoom, because "Zoom works" is
worth much less than "Zoom works and Meet still does".
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest
from app import bot_client
from app.bot_client import trigger_bot_join
from app.calendar_service import _parse_event
from app.config import BridgeSettings, GmailSettings
from app.gmail_service import GmailService
from app.invite_parser import parse_invite
from app.meeting_link import PLATFORM_GOOGLE_MEET, PLATFORM_ZOOM
from app.models import MissingConferenceDataError

SETTINGS = GmailSettings(enabled=True)

ZOOM_BODY = """Someone is inviting you to a scheduled Zoom meeting.

Join Zoom Meeting
https://us05web.zoom.us/j/83843212151?pwd=05sFhA1N0w3Xfy6onXiDf45agweVYL.1

Meeting ID: 838 4321 2151
Passcode: 139601

Download at https://zoom.us/download
"""


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def _message(
    *,
    sender: str = '"Zoom" <no-reply@zoom.us>',
    subject: str = "Someone is inviting you to a scheduled Zoom meeting",
    body: str = ZOOM_BODY,
    snippet: str = "",
    internal_date: str = "1755000000000",
) -> dict:
    return {
        "id": "msg-z1",
        "threadId": "thread-z1",
        "snippet": snippet,
        "internalDate": internal_date,
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "From", "value": sender},
                {"name": "Subject", "value": subject},
            ],
            "body": {"data": _b64(body)},
        },
    }


# --------------------------------------------------------------------------- #
# Direct invite, by email
# --------------------------------------------------------------------------- #


class TestZoomInviteEmail:
    def test_a_zoom_invite_is_parsed_with_its_passcode(self) -> None:
        invite = parse_invite(_message(), SETTINGS)

        assert invite is not None
        assert invite.platform == PLATFORM_ZOOM
        assert invite.meeting_code == "83843212151"
        assert invite.passcode == "139601"
        assert invite.meeting_url.startswith("https://us05web.zoom.us/j/83843212151")

    def test_a_human_forwarding_a_zoom_invite_is_accepted(self) -> None:
        """**The behaviour was inverted deliberately, and this is where it is recorded.**

        Zoom mails the *host* an invitation which they then paste to attendees, so genuine
        invite text arrives from ordinary human addresses far more often than it arrives from
        Zoom. Requiring an allow-listed sender therefore rejected most real invitations, which
        is what the body route exists to fix.

        The cost is the plain reading of the same sentence: any sender who mails the bot a
        Zoom invitation block can put it in that meeting.
        """
        assert parse_invite(_message(sender="colleague@example.com"), SETTINGS) is not None

    def test_the_display_name_cannot_impersonate_a_trusted_sender(self) -> None:
        """Still true wherever the sender gate is what is being relied on.

        Asserted with a Meet body, because a Zoom invitation block is accepted from anybody
        and so could not demonstrate anything about sender matching.
        """
        spoofed = _message(
            sender='"meetings-noreply@google.com" <attacker@example.com>',
            subject="Happening now: Weekly sync",
            body="Join at https://meet.google.com/abc-defg-hij",
        )

        assert parse_invite(spoofed, SETTINGS) is None

    def test_zoom_mail_that_is_not_an_invite_does_not_join(self) -> None:
        """One sender address covers recordings, missed calls and storage warnings too.

        The body matters as much as the subject here: this fixture used to carry a full
        invite block, so it was passing on the subject check while the body would have
        matched anyway. A real recording notice has a link to the recording and no
        ``Meeting ID:`` line.
        """
        notice = _message(
            subject="Your cloud recording is ready",
            body="Your recording is available at https://zoom.us/rec/share/abc123",
        )

        assert parse_invite(notice, SETTINGS) is None

    def test_an_invite_whose_link_is_only_in_the_html_part_still_pairs_with_the_passcode(
        self,
    ) -> None:
        """The parts are two renderings of one invite, so they are searched as one text.

        Senders strip or reformat one part or the other; a link surviving in the HTML and a
        passcode surviving in the plain text is ordinary rather than exceptional.
        """
        message = {
            "id": "msg-z2",
            "threadId": "t",
            "snippet": "",
            "internalDate": "1755000000000",
            "payload": {
                "mimeType": "multipart/alternative",
                "headers": [
                    {"name": "From", "value": "no-reply@zoom.us"},
                    {"name": "Subject", "value": "Zoom meeting invitation - Standup"},
                ],
                "parts": [
                    {
                        "mimeType": "text/plain",
                        "body": {"data": _b64("Meeting ID: 838 4321 2151\nPasscode: 139601")},
                    },
                    {
                        "mimeType": "text/html",
                        "body": {
                            "data": _b64(
                                '<a href="https://us05web.zoom.us/j/83843212151">Join</a>'
                            )
                        },
                    },
                ],
            },
        }

        invite = parse_invite(message, SETTINGS)

        assert invite is not None
        assert invite.meeting_code == "83843212151"
        assert invite.passcode == "139601"

    def test_a_meet_invite_is_unchanged_by_zoom_support(self) -> None:
        """The regression that matters: Meet still parses exactly as it did."""
        invite = parse_invite(
            _message(
                sender='"Google Meet" <meetings-noreply@google.com>',
                subject="Happening now: Weekly sync",
                body="Join at https://meet.google.com/abc-defg-hij",
            ),
            SETTINGS,
        )

        assert invite is not None
        assert invite.platform == PLATFORM_GOOGLE_MEET
        assert invite.meeting_code == "abc-defg-hij"
        assert invite.passcode is None
        assert invite.meeting_url == "https://meet.google.com/abc-defg-hij"


# --------------------------------------------------------------------------- #
# Direct invite from inside a running meeting
# --------------------------------------------------------------------------- #

IN_MEETING_BODY = """Someone is inviting you to a scheduled Zoom meeting.

Topic: Someone's Zoom Meeting
Join Zoom Meeting
https://us05web.zoom.us/j/85666054587?pwd=QaFba9b6J4dQqbb8yln4apjVV8pnbE.1

Meeting agenda
https://docs.zoom.us/agenda/doc/1000492c-8c6d-4ba9-8247-03904f5472fa

Meeting chat link
https://us05web.zoom.us/launch/jc/85666054587

Meeting ID: 856 6605 4587
Passcode: 2A4veB
"""
"""Captured verbatim from a real *Invite → Email* sent out of a running meeting."""

IN_MEETING_SUBJECT = "Please join Zoom meeting in progress"
HOST_SENDER = '"Any Host" <whoever@example.com>'


class TestInMeetingInvite:
    """**The case the calendar structurally cannot see**, and the one that most needs to work.

    Somebody in a running meeting clicks *Invite → Email*. No calendar event is created, so
    the only artifact is a mail — and Zoom composes it from the **host's own mailbox**, not
    from ``no-reply@zoom.us``. The sender is therefore whoever happens to be running the
    meeting: unknowable in advance, and never the same twice.

    So every test here runs on the **default** settings and senders that appear nowhere in
    the codebase. A fixture that had to be added to an allow-list to pass would be testing
    the allow-list rather than this feature.
    """

    def test_the_body_yields_the_meeting_and_its_passcode(self) -> None:
        invite = parse_invite(
            _message(
                sender=HOST_SENDER,
                subject=IN_MEETING_SUBJECT,
                body=IN_MEETING_BODY,
            ),
            SETTINGS,  # defaults: nothing about this sender is configured
        )

        assert invite is not None
        assert invite.platform == PLATFORM_ZOOM
        assert invite.meeting_code == "85666054587"
        assert invite.passcode == "2A4veB"

    def test_the_agenda_and_chat_links_are_not_mistaken_for_the_meeting(self) -> None:
        """This body carries three zoom.us links and only one of them is joinable.

        ``docs.zoom.us/agenda/doc/...`` and ``/launch/jc/...`` are neither ``/j/``, ``/wc/``
        nor ``/s/``, which is exactly what the path requirement in the pattern is for.
        """
        invite = parse_invite(
            _message(sender=HOST_SENDER, subject=IN_MEETING_SUBJECT, body=IN_MEETING_BODY),
            SETTINGS,
        )

        assert invite is not None
        assert invite.meeting_code == "85666054587"
        assert "agenda" not in invite.meeting_url
        assert "launch" not in invite.meeting_url

    def test_an_alphanumeric_passcode_survives(self) -> None:
        """Zoom passcodes are not always digits — this one is ``2A4veB``.

        A digits-only pattern would have read the meeting id's trailing digits instead, or
        nothing, and the join would fail at the passcode prompt with no clue why.
        """
        invite = parse_invite(
            _message(sender=HOST_SENDER, subject=IN_MEETING_SUBJECT, body=IN_MEETING_BODY),
            SETTINGS,
        )

        assert invite is not None
        assert invite.passcode == "2A4veB"

    def test_it_works_out_of_the_box_from_an_arbitrary_sender(self) -> None:
        """**The subject is the handle, because there is no sender to allow-list.**

        Zoom composes this from the host's own mailbox, so the address is whoever happens to
        be running the meeting — unknowable in advance. ``any_sender_subject_markers`` is
        what lets it through, and the default configuration carries it.
        """
        invite = parse_invite(
            _message(sender=HOST_SENDER, subject=IN_MEETING_SUBJECT, body=IN_MEETING_BODY),
            GmailSettings(enabled=True),  # defaults, no allow-list change
        )

        assert invite is not None
        assert invite.meeting_code == "85666054587"
        assert invite.passcode == "2A4veB"

    @pytest.mark.parametrize(
        "sender",
        [
            "whoever@example.com",
            '"Some Person" <someone.else@example.org>',
            "a.host@a-company.example",
            "SHOUTY@EXAMPLE.COM",
            "no-display-name@example.invalid",
        ],
    )
    def test_the_sender_genuinely_does_not_matter(self, sender: str) -> None:
        """**Any mailbox, with nothing configured for it.**

        Parametrised over unrelated addresses rather than asserted once, because a single
        fixture cannot distinguish "any sender works" from "this sender happens to work" —
        and the whole point of the route is that the address is unknowable in advance.

        It also states the exposure, which is the same fact seen from the other side: a
        stranger using this subject is let through exactly as the real host is. There is no
        cryptographic difference between the two — both are ordinary mail.
        """
        invite = parse_invite(
            _message(sender=sender, subject=IN_MEETING_SUBJECT, body=IN_MEETING_BODY),
            SETTINGS,
        )

        assert invite is not None, f"{sender} should have been accepted on subject alone"
        assert invite.meeting_code == "85666054587"

    def test_emptying_the_marker_list_turns_it_off(self) -> None:
        """The off switch, for a deployment that would rather lose the feature.

        Both open routes have to be closed to see it: with the body route still on, the same
        message is accepted on its invite block regardless of the subject.
        """
        assert (
            parse_invite(
                _message(sender=HOST_SENDER, subject=IN_MEETING_SUBJECT, body=IN_MEETING_BODY),
                GmailSettings(
                    enabled=True,
                    any_sender_subject_markers=(),
                    accept_zoom_invite_bodies=False,
                ),
            )
            is None
        )

    def test_everything_else_is_still_sender_gated(self) -> None:
        """Only this subject skips the allow-list; the rest of the filter is untouched."""
        settings = GmailSettings(enabled=True, allowed_senders=("@yourcompany.com",))
        scheduled = "Priya is inviting you to a scheduled Zoom meeting"
        # A Meet body, deliberately: Zoom bodies have their own route, so using one here
        # would accept every sender and the gate under test would never be reached.
        body = "Join at https://meet.google.com/abc-defg-hij"

        assert (
            parse_invite(
                _message(
                    sender="Priya <priya@yourcompany.com>",
                    subject=scheduled,
                    body=body,
                ),
                settings,
            )
            is not None
        )
        assert (
            parse_invite(
                _message(
                    sender="attacker@elsewhere.example",
                    subject=scheduled,
                    body=body,
                ),
                settings,
            )
            is None
        )

    def test_a_domain_entry_is_not_a_suffix_match(self) -> None:
        """``@company.com`` must not admit ``@notcompany.com`` or ``@company.com.evil.net``."""
        settings = GmailSettings(enabled=True, allowed_senders=("@company.com",))
        scheduled = "Priya is inviting you to a scheduled Zoom meeting"
        body = "Join at https://meet.google.com/abc-defg-hij"  # no Zoom body route

        for spoof in ("bad@notcompany.com", "bad@company.com.evil.net", "bad@evil.com"):
            assert (
                parse_invite(
                    _message(sender=spoof, subject=scheduled, body=body),
                    settings,
                )
                is None
            ), spoof

    def test_the_body_must_still_contain_a_real_meeting(self) -> None:
        """The subject grants a hearing, not a join."""
        assert (
            parse_invite(
                _message(
                    sender=HOST_SENDER,
                    subject=IN_MEETING_SUBJECT,
                    body="Come to my meeting! https://zoom.us/download",
                ),
                GmailSettings(enabled=True),
            )
            is None
        )

    def test_the_subject_does_not_have_to_look_like_anything(self) -> None:
        """An invitation is recognised by its body, so the subject can be arbitrary.

        This is the property the calendar case needs: a Zoom meeting added to a Google
        Calendar event arrives under the *event's* title, which is whatever the organiser
        typed.
        """
        assert (
            parse_invite(
                _message(
                    sender=HOST_SENDER,
                    subject="Re: notes from yesterday",
                    body=IN_MEETING_BODY,
                ),
                SETTINGS,
            )
            is not None
        )

    def test_a_mere_mention_of_a_zoom_link_is_not_an_invitation(self) -> None:
        """**Where the body route stops**, and the reason it needs more than a link.

        Somebody reminiscing about an old meeting has a Zoom URL in their mail and no
        intention of summoning a bot. The labelled ``Meeting ID:`` / ``Passcode:`` line is
        what separates an invitation from a mention.
        """
        assert (
            parse_invite(
                _message(
                    sender=HOST_SENDER,
                    subject="Re: notes from yesterday",
                    body="we used to meet at https://us05web.zoom.us/j/85666054587 by the way",
                ),
                SETTINGS,
            )
            is None
        )


# --------------------------------------------------------------------------- #
# Scheduled invite, by calendar
# --------------------------------------------------------------------------- #


def _event(**overrides) -> dict:
    raw = {
        "id": "evt-1",
        "summary": "Standup",
        "status": "confirmed",
        "updated": "2026-08-20T08:00:00Z",
        "start": {"dateTime": "2026-08-20T09:00:00Z"},
    }
    raw.update(overrides)
    return raw


class TestZoomCalendarEvent:
    def test_the_zoom_add_on_entry_point_is_used_with_its_password_field(self) -> None:
        """How a Zoom meeting scheduled from Google Calendar actually looks.

        The add-on writes a proper ``conferenceData`` entry point and puts the passcode in
        Google's own ``password`` field rather than in the description.
        """
        event = _parse_event(
            _event(
                conferenceData={
                    "entryPoints": [
                        {
                            "entryPointType": "video",
                            "uri": "https://us05web.zoom.us/j/83843212151?pwd=tok.1",
                            "password": "139601",
                        },
                        {"entryPointType": "phone", "uri": "tel:+16699009128"},
                    ]
                }
            )
        )

        assert event.platform == PLATFORM_ZOOM
        assert event.meeting_code == "83843212151"
        assert event.passcode == "139601"

    def test_a_link_pasted_into_the_description_is_joinable(self) -> None:
        """A meeting somebody scheduled by pasting the invite, with no add-on involved."""
        event = _parse_event(_event(description=ZOOM_BODY))

        assert event.platform == PLATFORM_ZOOM
        assert event.meeting_code == "83843212151"
        assert event.passcode == "139601"

    def test_a_link_in_the_location_field_is_joinable(self) -> None:
        event = _parse_event(_event(location="https://us05web.zoom.us/j/83843212151"))

        assert event.platform == PLATFORM_ZOOM
        assert event.meeting_code == "83843212151"

    def test_the_passcode_is_taken_from_the_description_when_the_link_is_structured(
        self,
    ) -> None:
        """The add-on splits them exactly this way on some tenants."""
        event = _parse_event(
            _event(
                conferenceData={
                    "entryPoints": [
                        {
                            "entryPointType": "video",
                            "uri": "https://us05web.zoom.us/j/83843212151",
                        }
                    ]
                },
                description="Passcode: 139601",
            )
        )

        assert event.passcode == "139601"

    def test_a_stale_meet_link_in_the_description_does_not_outrank_the_zoom_conference(
        self,
    ) -> None:
        """Structured fields are facts; a description is whatever somebody pasted."""
        event = _parse_event(
            _event(
                conferenceData={
                    "entryPoints": [
                        {
                            "entryPointType": "video",
                            "uri": "https://us05web.zoom.us/j/83843212151",
                        }
                    ]
                },
                description="Agenda copied from last week: https://meet.google.com/abc-defg-hij",
            )
        )

        assert event.platform == PLATFORM_ZOOM
        assert event.meeting_code == "83843212151"

    def test_a_meet_event_is_unchanged(self) -> None:
        event = _parse_event(_event(hangoutLink="https://meet.google.com/abc-defg-hij"))

        assert event.platform == PLATFORM_GOOGLE_MEET
        assert event.meeting_code == "abc-defg-hij"
        assert event.passcode is None

    def test_an_event_with_no_conferencing_is_skipped(self) -> None:
        with pytest.raises(MissingConferenceDataError):
            _parse_event(_event(description="Bring the roadmap deck"))


# --------------------------------------------------------------------------- #
# The body signature
# --------------------------------------------------------------------------- #


class TestZoomInviteBody:
    """**The route for everything the sender and the subject cannot identify.**

    A Zoom meeting added to a Google Calendar event arrives under the *event's* title —
    whatever the organiser typed, in any language — from their own mailbox. Between them the
    two envelope fields carry no signal at all. What is invariant is the invite text.
    """

    @pytest.mark.parametrize(
        "subject",
        ["test zoom", "sync-up", "会議", "", "Re: Fwd: whatever", "URGENT!!!"],
    )
    def test_any_subject_at_all(self, subject: str) -> None:
        invite = parse_invite(
            _message(sender="anyone@example.org", subject=subject, body=IN_MEETING_BODY),
            SETTINGS,
        )

        assert invite is not None
        assert invite.meeting_code == "85666054587"
        assert invite.passcode == "2A4veB"

    @pytest.mark.parametrize(
        "body",
        [
            # The full block.
            IN_MEETING_BODY,
            # Link plus the Meeting ID line, no passcode — a meeting without one.
            "Join Zoom Meeting\nhttps://us05web.zoom.us/j/85666054587\n"
            "Meeting ID: 856 6605 4587",
            # Link plus a passcode line, no Meeting ID line.
            "https://us05web.zoom.us/j/85666054587\nPasscode: 2A4veB",
        ],
    )
    def test_the_block_is_a_link_plus_a_labelled_line(self, body: str) -> None:
        assert (
            parse_invite(
                _message(sender="anyone@example.org", subject="anything", body=body),
                SETTINGS,
            )
            is not None
        )

    @pytest.mark.parametrize(
        "body",
        [
            # A link on its own is a mention, not an invitation.
            "we used to meet at https://us05web.zoom.us/j/85666054587 by the way",
            # Labels with no link.
            "Meeting ID: 856 6605 4587\nPasscode: 2A4veB",
            # Zoom's marketing footer, which appears in plenty of unrelated mail.
            "Sent from Zoom. Download at https://zoom.us/download",
            "Nothing to do with meetings at all.",
        ],
    )
    def test_what_does_not_count_as_an_invitation(self, body: str) -> None:
        """**Where this route stops.**

        Requiring a labelled line *and* a join link is what separates an invitation from
        somebody merely mentioning a meeting — which is the difference between a useful bot
        and one that turns up uninvited whenever a Zoom URL is quoted.
        """
        assert (
            parse_invite(
                _message(sender="anyone@example.org", subject="anything", body=body),
                SETTINGS,
            )
            is None
        )

    def test_it_can_be_switched_off(self) -> None:
        assert (
            parse_invite(
                _message(
                    sender="anyone@example.org", subject="anything", body=IN_MEETING_BODY
                ),
                GmailSettings(enabled=True, accept_zoom_invite_bodies=False),
            )
            is None
        )


# --------------------------------------------------------------------------- #
# The Gmail query
# --------------------------------------------------------------------------- #


class TestQuery:
    """The query is a *performance* filter — ``parse_invite`` is the security one — but a
    query that returns the wrong set still stops the bot turning up."""

    def _query(self, **kwargs) -> str:
        service = GmailService.__new__(GmailService)  # no credentials, no API client needed
        service._settings = GmailSettings(enabled=True, **kwargs)
        return service.build_query()

    def test_addresses_and_domains_both_become_from_clauses(self) -> None:
        query = self._query(allowed_senders=("no-reply@zoom.us", "@yourcompany.com"))

        assert "from:no-reply@zoom.us" in query
        assert "from:@yourcompany.com" in query

    def test_a_wildcard_narrows_by_subject_instead_of_returning_the_whole_inbox(self) -> None:
        """**Dropping the clause and keeping nothing would quietly break the feature.**

        Every unread message of the last day would match, and ``max_results`` caps a poll at
        ten — so on a busy inbox a real invite is pushed out of the window and the bot simply
        stops turning up, with nothing in the log to say why.
        """
        query = self._query(allowed_senders=("*",))

        assert "from:" not in query
        assert 'subject:"please join zoom meeting in progress"' in query.casefold()
        assert "is:unread" in query

    def test_the_default_query_still_narrows_by_sender(self) -> None:
        query = self._query()

        assert "from:meetings-noreply@google.com" in query
        assert "from:no-reply@zoom.us" in query
        assert "newer_than:1d" in query

    def test_a_pasted_invitation_is_retrievable_without_sender_subject_or_ics(self) -> None:
        """**The third instance of the same trap.**

        The body route accepts a message on its text alone, so nothing about its envelope
        can select it — no ``from:``, no ``subject:``, and no ``filename:ics`` if the invite
        was pasted rather than sent by Google Calendar. Without a body term the message is
        never fetched, the parser never sees it, and the feature is dead while looking fine.
        """
        assert '"zoom.us"' in self._query()

    def test_switching_the_body_route_off_drops_its_term(self) -> None:
        assert '"zoom.us"' not in self._query(accept_zoom_invite_bodies=False)

    def test_the_in_meeting_subject_is_retrievable_without_any_matching_sender(self) -> None:
        """**The bug this prevents is the whole feature silently doing nothing.**

        An in-meeting invite comes from the host's mailbox, so no ``from:`` clause can match
        it. Without a ``subject:`` term in the query the message is never *fetched* — and the
        parser, which would have accepted it, is never given the chance. Everything would
        look correct and the bot would simply never turn up.
        """
        query = self._query().casefold()

        assert 'subject:"please join zoom meeting in progress"' in query


# --------------------------------------------------------------------------- #
# What reaches the bridge
# --------------------------------------------------------------------------- #


@pytest.fixture
def bridge_payload(monkeypatch: pytest.MonkeyPatch):
    """Run ``trigger_bot_join`` against a fake bridge and return the JSON it posted.

    The real ``httpx.AsyncClient`` is constructed inside ``bot_client``, so it is replaced
    there rather than globally — a mock transport is enough to capture the request without
    the module knowing it is under test, and nothing else in the process is affected.
    """

    async def run(**kwargs) -> dict:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(json.loads(request.content))
            return httpx.Response(202, json={"session_id": "ses_1"})

        transport = httpx.MockTransport(handler)
        real_client = httpx.AsyncClient

        def build(*args, **kw):
            kw["transport"] = transport
            return real_client(*args, **kw)

        monkeypatch.setattr(bot_client.httpx, "AsyncClient", build)
        await trigger_bot_join("83843212151", BridgeSettings(), **kwargs)
        return captured

    return run


class TestBridgeRequest:
    async def test_a_zoom_join_carries_the_platform_and_passcode(
        self, bridge_payload
    ) -> None:
        payload = await bridge_payload(platform=PLATFORM_ZOOM, passcode="139601")

        assert payload == {
            "platform": "zoom_web",
            "meeting_number": "83843212151",
            "passcode": "139601",
        }

    async def test_zoom_web_is_the_platform_not_the_sdk_connector(
        self, bridge_payload
    ) -> None:
        """``zoom`` is the Meeting-SDK connector and needs the *host's* account to have RTMS
        enabled. An invited meeting is by definition somebody else's, so that entitlement is
        exactly what is unavailable — ``zoom_web`` joins as an ordinary browser participant."""
        payload = await bridge_payload(platform=PLATFORM_ZOOM, passcode=None)

        assert payload["platform"] == "zoom_web"

    async def test_an_absent_passcode_is_omitted_rather_than_sent_as_null(
        self, bridge_payload
    ) -> None:
        payload = await bridge_payload(platform=PLATFORM_ZOOM)

        assert "passcode" not in payload

    async def test_a_call_naming_no_platform_sends_what_it_always_did(
        self, bridge_payload
    ) -> None:
        """The backward-compatibility guarantee: existing callers are byte-for-byte unchanged."""
        payload = await bridge_payload()

        assert payload == {"platform": "google_meet", "meeting_number": "83843212151"}
