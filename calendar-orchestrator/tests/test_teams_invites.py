"""Microsoft Teams arriving by the routes this service already had, and what reaches the bridge.

The same four seams ``test_zoom_invites`` covers, because a Teams meeting can fail to be joined
at each of them and every failure looks identical from outside — the bot simply never turns up:

* the **invite email** — a link pasted into a message, or an Outlook invitation;
* the **calendar event** — a Teams meeting sitting on the bot's Google Calendar;
* the **Gmail query** — whether such a message is ever *fetched* to be parsed;
* the **bridge request** — the platform, passcode and join URL actually sent.

**Teams has no sender and no subject to lean on, which makes it the strictest test of the open
routes.** Zoom at least mails scheduled invitations from ``no-reply@zoom.us`` and composes
in-meeting ones under a fixed subject. Teams does neither: an invitation reaches a mailbox
either as an Outlook calendar invite (whose subject is the event's own title) or as a link
somebody pasted, from their own mailbox in both cases. So every test here runs on the
**default** settings with senders and subjects that appear nowhere in the codebase — a fixture
that had to be added to an allow-list to pass would be testing the allow-list.

The Zoom and Meet equivalents are re-asserted wherever they share a code path, because "Teams
works" is worth much less than "Teams works and the other two still do".
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from app import bot_client
from app.bot_client import trigger_bot_join
from app.calendar_service import _parse_event
from app.config import BridgeSettings, GmailSettings
from app.gmail_service import GmailService
from app.invite_parser import parse_invite
from app.meeting_link import (
    PLATFORM_GOOGLE_MEET,
    PLATFORM_TEAMS,
    PLATFORM_ZOOM,
    join_url_for,
)

SETTINGS = GmailSettings(enabled=True)

MEETING_ID = "9339756425487"
PASSCODE = "71cQWhQJ5X8fxHSmVy"
SHORT_LINK = f"https://teams.live.com/meet/{MEETING_ID}?p={PASSCODE}"

TEAMS_BODY = f"""Priya Sharma is inviting you to a Microsoft Teams meeting.

Join the meeting now
{SHORT_LINK}
Meeting ID: 933 975 642 5487
Passcode: {PASSCODE}
________________________________________
Need help? Visit https://teams.live.com/support
Get the app at https://teams.microsoft.com/downloads
"""
"""A personal ("Teams for Life") invitation — the shape this feature was asked for, and the
one whose link carries its own passcode."""

TENANT_LINK = (
    "https://teams.microsoft.com/l/meetup-join/19%3ameeting_YmM2MTk%40thread.v2/0"
    "?context=%7b%22Tid%22%3a%22aaa%22%2c%22Oid%22%3a%22bbb%22%7d"
)

TENANT_BODY = f"""________________________________________
Microsoft Teams
Need help?
Join the meeting now
{TENANT_LINK}
Meeting ID: 281 442 953 617
Passcode: aB3dE9
________________________________________
Dial in by phone
+1 323-555-0199,,123456789# United States, Los Angeles
Find a local number
Phone conference ID: 123 456 789#
For organizers: Meeting options | Reset dial-in PIN
________________________________________
"""
"""A work/school invitation as Outlook writes it. Its link carries a thread id rather than a
meeting number, so the number has to come out of the printed block."""

ORGANISER = '"Priya Sharma" <priya@example.org>'


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def _message(
    *,
    sender: str = ORGANISER,
    subject: str = "Weekly sync",
    body: str = TEAMS_BODY,
    snippet: str = "",
    internal_date: str = "1755000000000",
) -> dict:
    return {
        "id": "msg-t1",
        "threadId": "thread-t1",
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
# The invite email
# --------------------------------------------------------------------------- #


class TestTeamsInviteEmail:
    def test_a_personal_invitation_yields_the_id_and_the_passcode(self) -> None:
        invite = parse_invite(_message(), SETTINGS)

        assert invite is not None
        assert invite.platform == PLATFORM_TEAMS
        assert invite.meeting_code == MEETING_ID
        assert invite.passcode == PASSCODE
        assert invite.meeting_url == SHORT_LINK

    def test_teams_web_is_the_platform_not_the_graph_connector(self) -> None:
        """``teams`` is the Graph/media-SDK connector: an Azure AD app with admin-consented
        ``Calls.AccessMedia.All``, a tenant willing to grant it, and a Windows host. An invited
        meeting belongs to somebody else's tenant — often a personal account with no tenant at
        all — so none of the three is available and ``teams_web`` is the only route in."""
        invite = parse_invite(_message(), SETTINGS)

        assert invite is not None
        assert invite.platform == "teams_web"

    def test_a_tenant_invitation_takes_its_number_from_the_printed_block(self) -> None:
        invite = parse_invite(_message(body=TENANT_BODY), SETTINGS)

        assert invite is not None
        assert invite.platform == PLATFORM_TEAMS
        assert invite.meeting_code == "281442953617"
        assert invite.passcode == "aB3dE9"
        assert invite.meeting_url.startswith(
            "https://teams.microsoft.com/l/meetup-join/"
        )

    @pytest.mark.parametrize(
        "subject",
        ["Weekly sync", "Invitation: 1:1", "会議", "", "Re: Fwd: whatever", "URGENT!!!"],
    )
    def test_any_subject_at_all(self, subject: str) -> None:
        """**Teams has no subject marker and cannot have one.** An Outlook invitation is
        titled whatever the organiser typed, in whatever language; a pasted link has no title
        at all. The body is the only handle."""
        invite = parse_invite(_message(subject=subject), SETTINGS)

        assert invite is not None
        assert invite.meeting_code == MEETING_ID

    @pytest.mark.parametrize(
        "sender",
        [
            "priya@example.org",
            '"Some Person" <someone.else@example.com>',
            "a.host@a-company.example",
            "SHOUTY@EXAMPLE.COM",
            "no-display-name@example.invalid",
        ],
    )
    def test_the_sender_genuinely_does_not_matter(self, sender: str) -> None:
        """**Any mailbox, with nothing configured for it.**

        Parametrised over unrelated addresses rather than asserted once, because a single
        fixture cannot distinguish "any sender works" from "this sender happens to work" — and
        Microsoft never sends these from a system address, so the organiser's own mailbox is
        all there ever is.

        It also states the exposure from the other side: a stranger pasting a Teams invitation
        is let through exactly as a colleague is. There is no cryptographic difference — both
        are ordinary mail. ``max_invite_age_s`` and the join de-duplication are the bounds.
        """
        invite = parse_invite(_message(sender=sender), SETTINGS)

        assert invite is not None, f"{sender} should have been accepted on its body"
        assert invite.meeting_code == MEETING_ID

    def test_the_display_name_cannot_impersonate_a_trusted_sender(self) -> None:
        """Still true wherever the sender gate *is* what is being relied on.

        Asserted with a Meet body, because a Teams invitation block is accepted from anybody
        and so could not demonstrate anything about sender matching.
        """
        spoofed = _message(
            sender='"meetings-noreply@google.com" <attacker@example.com>',
            subject="Happening now: Weekly sync",
            body="Join at https://meet.google.com/abc-defg-hij",
        )

        assert parse_invite(spoofed, SETTINGS) is None

    def test_teams_mail_that_is_not_an_invitation_does_not_join(self) -> None:
        """Notification mail mentions Teams and links to it without inviting anybody."""
        body = (
            "You have missed activity in Microsoft Teams. "
            "Open the app at https://teams.microsoft.com/downloads to catch up."
        )

        assert parse_invite(_message(body=body), SETTINGS) is None


# --------------------------------------------------------------------------- #
# The body signature
# --------------------------------------------------------------------------- #


class TestTeamsInviteBody:
    @pytest.mark.parametrize(
        "body",
        [
            # The full personal block.
            TEAMS_BODY,
            # The full tenant block.
            TENANT_BODY,
            # A short link on its own: its ``?p=`` is the whole of what a join needs, and that
            # string only comes from the *Copy link* button.
            SHORT_LINK,
            # A passcodeless short link, rescued by the printed block underneath it.
            f"Join the meeting now\nhttps://teams.live.com/meet/{MEETING_ID}\n"
            "Meeting ID: 933 975 642 5487",
            # A tenant link plus a labelled line.
            f"{TENANT_LINK}\nPasscode: aB3dE9",
        ],
    )
    def test_what_counts_as_an_invitation(self, body: str) -> None:
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
            # A passcodeless link on its own is a mention, not an invitation.
            f"we used to meet at https://teams.live.com/meet/{MEETING_ID} by the way",
            f"old thread: {TENANT_LINK}",
            # Labels with no link.
            "Microsoft Teams\nMeeting ID: 281 442 953 617\nPasscode: aB3dE9",
            # Marketing and notification footers, which appear in plenty of unrelated mail.
            "Sent from Microsoft Teams. Get the app: https://teams.microsoft.com/downloads",
            "Need help? https://teams.live.com/support",
            "Nothing to do with meetings at all.",
        ],
    )
    def test_what_does_not_count_as_an_invitation(self, body: str) -> None:
        """**Where this route stops**, and it has to stop somewhere: the route accepts
        arbitrary senders, so "there is a Teams link in here" would put the bot in a meeting
        every time somebody quoted one."""
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
                _message(sender="anyone@example.org", subject="anything"),
                GmailSettings(enabled=True, accept_teams_invite_bodies=False),
            )
            is None
        )

    def test_switching_teams_off_leaves_zoom_working(self) -> None:
        """The two switches are independent, which is the point of there being two."""
        zoom_body = (
            "Join Zoom Meeting\nhttps://us05web.zoom.us/j/83843212151\n"
            "Meeting ID: 838 4321 2151\nPasscode: 139601"
        )

        invite = parse_invite(
            _message(sender="anyone@example.org", subject="anything", body=zoom_body),
            GmailSettings(enabled=True, accept_teams_invite_bodies=False),
        )

        assert invite is not None
        assert invite.platform == PLATFORM_ZOOM


# --------------------------------------------------------------------------- #
# An Outlook invitation, which arrives as an ics
# --------------------------------------------------------------------------- #


def _ics(start: datetime, end: datetime, *, description: str) -> str:
    return (
        "BEGIN:VCALENDAR\r\n"
        "METHOD:REQUEST\r\n"
        "BEGIN:VEVENT\r\n"
        f"DTSTART:{start:%Y%m%dT%H%M%SZ}\r\n"
        f"DTEND:{end:%Y%m%dT%H%M%SZ}\r\n"
        "SUMMARY:Weekly sync\r\n"
        f"DESCRIPTION:{description}\r\n"
        "ORGANIZER;CN=Priya Sharma:mailto:priya@example.org\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR"
    )


def _ics_message(start: datetime, end: datetime, *, description: str) -> dict:
    return {
        "id": "cal-t1",
        "threadId": "t",
        "snippet": "",
        "internalDate": "1755000000000",
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [
                {"name": "From", "value": ORGANISER},
                {"name": "Subject", "value": "Invitation: Weekly sync"},
            ],
            "parts": [
                {
                    "mimeType": "text/plain",
                    "body": {"data": _b64("Priya invited you to a Teams meeting.")},
                },
                {
                    "mimeType": "text/calendar",
                    "filename": "invite.ics",
                    "body": {"data": _b64(_ics(start, end, description=description))},
                },
            ],
        },
    }


class TestTeamsCalendarInvitationEmail:
    """A Teams meeting arriving as a calendar invitation — the route that needed no new code,
    asserted because "needed no new code" is a claim and not a fact until it is checked."""

    def test_an_invitation_for_a_meeting_in_progress_is_joined(self) -> None:
        now = datetime.now(UTC)
        message = _ics_message(
            now - timedelta(minutes=2),
            now + timedelta(hours=1),
            description=f"Join the meeting now\\n{SHORT_LINK}\\nPasscode: {PASSCODE}",
        )

        invite = parse_invite(message, SETTINGS)

        assert invite is not None
        assert invite.platform == PLATFORM_TEAMS
        assert invite.meeting_code == MEETING_ID
        assert invite.passcode == PASSCODE

    def test_an_invitation_for_next_week_is_left_to_the_calendar_poller(self) -> None:
        """**The gate that keeps the inbox path from joining days early.**

        The ics is the only part of a message that says *when* the meeting is, so it overrules
        the body signature — which would otherwise pass every other filter (fresh mail, real
        link, genuine organiser) and put the bot in a meeting a week early.
        """
        now = datetime.now(UTC)
        message = _ics_message(
            now + timedelta(days=6),
            now + timedelta(days=6, hours=1),
            description=f"Join the meeting now\\n{SHORT_LINK}",
        )

        assert parse_invite(message, SETTINGS) is None

    def test_a_folded_join_url_is_unfolded_before_it_is_read(self) -> None:
        """**Silent truncation, and the meeting number survives it — which is what makes it
        dangerous.** iCalendar wraps at 75 octets with a leading-space continuation, and a
        Teams link is longer than that. The id sits near the front of the URL, so a raw read
        gets the right meeting with a passcode cut in half: a join that fails at the passcode
        prompt for no visible reason.
        """
        now = datetime.now(UTC)
        folded = (
            "Join the meeting now\\nhttps://teams.live.com/meet/9339756425487?p=71cQ\r\n"
            " WhQJ5X8fxHSmVy\\nMeeting ID: 933 975 642 5487"
        )
        message = _ics_message(
            now - timedelta(minutes=2), now + timedelta(hours=1), description=folded
        )

        invite = parse_invite(message, SETTINGS)

        assert invite is not None
        assert invite.meeting_code == MEETING_ID
        assert invite.passcode == PASSCODE, "read a truncated passcode"


# --------------------------------------------------------------------------- #
# The calendar event
# --------------------------------------------------------------------------- #


def _event(**overrides) -> dict:
    raw = {
        "id": "evt-t1",
        "summary": "Weekly sync",
        "status": "confirmed",
        "updated": "2026-08-20T08:00:00Z",
        "start": {"dateTime": "2026-08-20T09:00:00Z"},
    }
    raw.update(overrides)
    return raw


class TestTeamsCalendarEvent:
    def test_a_link_in_the_location_field_is_joinable(self) -> None:
        """Where a Teams meeting copied into a Google Calendar event usually ends up."""
        event = _parse_event(_event(location=SHORT_LINK))

        assert event.platform == PLATFORM_TEAMS
        assert event.meeting_code == MEETING_ID
        assert event.passcode == PASSCODE
        assert event.meeting_url == SHORT_LINK

    def test_a_block_pasted_into_the_description_is_joinable(self) -> None:
        event = _parse_event(_event(description=TENANT_BODY))

        assert event.platform == PLATFORM_TEAMS
        assert event.meeting_code == "281442953617"
        assert event.passcode == "aB3dE9"

    def test_a_structured_entry_point_wins_and_its_password_field_is_read(self) -> None:
        """Google's own ``password`` field, for an add-on that fills it."""
        event = _parse_event(
            _event(
                conferenceData={
                    "entryPoints": [
                        {
                            "entryPointType": "video",
                            "uri": f"https://teams.live.com/meet/{MEETING_ID}",
                            "password": "aB3dE9",
                        },
                        {"entryPointType": "phone", "uri": "tel:+13235550199"},
                    ]
                }
            )
        )

        assert event.platform == PLATFORM_TEAMS
        assert event.meeting_code == MEETING_ID
        assert event.passcode == "aB3dE9"

    def test_the_passcode_is_taken_from_the_description_when_the_link_is_structured(
        self,
    ) -> None:
        """The generalised second sweep: link in ``conferenceData``, passcode in the body."""
        event = _parse_event(
            _event(
                conferenceData={
                    "entryPoints": [
                        {
                            "entryPointType": "video",
                            "uri": f"https://teams.live.com/meet/{MEETING_ID}",
                        }
                    ]
                },
                description="Passcode: aB3dE9",
            )
        )

        assert event.passcode == "aB3dE9"

    def test_a_stale_meet_link_in_the_description_does_not_outrank_the_teams_location(
        self,
    ) -> None:
        """``location`` is consulted before ``description``, so a copied agenda cannot win."""
        event = _parse_event(
            _event(
                location=SHORT_LINK,
                description="Agenda from last week: https://meet.google.com/abc-defg-hij",
            )
        )

        assert event.platform == PLATFORM_TEAMS

    def test_a_meet_event_is_unchanged(self) -> None:
        event = _parse_event(_event(hangoutLink="https://meet.google.com/abc-defg-hij"))

        assert event.platform == PLATFORM_GOOGLE_MEET
        assert event.meeting_code == "abc-defg-hij"
        assert event.passcode is None


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

    def test_both_teams_hosts_are_retrievable(self) -> None:
        """**The trap that makes an implemented feature do nothing.**

        A Teams invitation matches no ``from:`` (the organiser is arbitrary), no ``subject:``
        (the subject is the event's title) and no ``filename:ics`` when it was pasted rather
        than sent by Outlook. Without a body term the message is never *fetched*, so the parser
        that would have accepted it never sees it.

        Both hosts, because which one an invite carries is decided by the organiser's account
        type — personal or work/school — and nothing here can see that.
        """
        query = self._query()

        assert '"teams.live.com"' in query
        assert '"teams.microsoft.com"' in query

    def test_switching_the_teams_body_route_off_drops_its_terms(self) -> None:
        query = self._query(accept_teams_invite_bodies=False)

        assert "teams.live.com" not in query
        assert "teams.microsoft.com" not in query

    def test_zooms_term_is_untouched(self) -> None:
        assert '"zoom.us"' in self._query()
        assert '"zoom.us"' in self._query(accept_teams_invite_bodies=False)

    def test_a_wildcard_sender_still_carries_the_teams_terms(self) -> None:
        """``*`` drops the ``from:`` clauses, and the body terms are what stops the query
        from matching every unread message of the last day."""
        query = self._query(allowed_senders=("*",))

        assert "from:" not in query
        assert '"teams.live.com"' in query


# --------------------------------------------------------------------------- #
# What reaches the bridge
# --------------------------------------------------------------------------- #


@pytest.fixture
def bridge_payload(monkeypatch: pytest.MonkeyPatch):
    """Run ``trigger_bot_join`` against a fake bridge and return the JSON it posted."""

    async def run(number: str = MEETING_ID, **kwargs) -> dict:
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
        await trigger_bot_join(number, BridgeSettings(), **kwargs)
        return captured

    return run


class TestBridgeRequest:
    async def test_a_teams_join_carries_the_platform_passcode_and_url(
        self, bridge_payload
    ) -> None:
        payload = await bridge_payload(
            platform=PLATFORM_TEAMS, passcode=PASSCODE, meeting_url=SHORT_LINK
        )

        assert payload == {
            "platform": "teams_web",
            "meeting_number": MEETING_ID,
            "passcode": PASSCODE,
            "meeting_url": SHORT_LINK,
        }

    async def test_the_join_url_is_sent_for_teams_and_for_nothing_else(self) -> None:
        """**Why the asymmetry exists, stated where it is decided.**

        Meet and Zoom are joined by number and the bridge documents ``meeting_url`` as ignored
        for them, so sending it would change a request that works today for no gain. A Teams
        meeting id does not say whether it belongs to a personal or a work/school account, and
        the bridge resolves that by trying one join form, waiting several polls for it to make
        no progress, and re-navigating to the other. The URL turns that guess into a fact.
        """
        assert join_url_for(PLATFORM_TEAMS, SHORT_LINK) == SHORT_LINK
        assert join_url_for(PLATFORM_ZOOM, "https://us05web.zoom.us/j/83843212151") is None
        assert join_url_for(PLATFORM_GOOGLE_MEET, "https://meet.google.com/abc-defg-hij") is None

    async def test_an_absent_url_is_omitted_rather_than_sent_as_null(
        self, bridge_payload
    ) -> None:
        payload = await bridge_payload(platform=PLATFORM_TEAMS, passcode=PASSCODE)

        assert "meeting_url" not in payload

    async def test_a_zoom_join_posts_exactly_the_body_it_always_did(
        self, bridge_payload
    ) -> None:
        """The backward-compatibility guarantee: adding ``meeting_url`` must not change a
        request that already works."""
        payload = await bridge_payload(
            "83843212151", platform=PLATFORM_ZOOM, passcode="139601"
        )

        assert payload == {
            "platform": "zoom_web",
            "meeting_number": "83843212151",
            "passcode": "139601",
        }

    async def test_a_call_naming_no_platform_sends_what_it_always_did(
        self, bridge_payload
    ) -> None:
        payload = await bridge_payload()

        assert payload == {"platform": "google_meet", "meeting_number": MEETING_ID}

    async def test_a_teams_passcode_survives_the_round_trip_through_the_url(self) -> None:
        """Teams passcodes are base64-ish and can carry characters that mean something else in
        a query string. The link is rebuilt rather than copied, so both halves of the encoding
        are this module's responsibility.

        **``+`` stays a plus rather than becoming a space**, which is the one judgement call
        here: ``+`` means space only under form encoding, and a passcode is a token typed into
        a text box, not a form field. A literal ``+`` in the passcode is far likelier than a
        space in one — Teams does not put spaces in passcodes — so the reading that preserves
        it is the one that joins the meeting.
        """
        from app.meeting_link import find_meeting_link

        link = find_meeting_link(f"https://teams.live.com/meet/{MEETING_ID}?p=a+b/c%3Dd")

        assert link is not None
        assert link.passcode == "a+b/c=d"
        assert link.url == f"https://teams.live.com/meet/{MEETING_ID}?p=a%2Bb%2Fc%3Dd"

    async def test_a_tenant_link_reaches_the_bridge_as_the_meeting_number(
        self, bridge_payload
    ) -> None:
        """A ``meetup-join`` invite with no printed id sends the URL in both fields.

        The bridge requires a non-empty ``meeting_number`` and accepts a Teams URL there, so
        this is a join rather than a rejected request — and it is what the join de-duplication
        keys on.
        """
        payload = await bridge_payload(
            TENANT_LINK, platform=PLATFORM_TEAMS, meeting_url=TENANT_LINK
        )

        assert payload["meeting_number"] == TENANT_LINK
        assert payload["meeting_url"] == TENANT_LINK


# --------------------------------------------------------------------------- #
# The wiring between them
# --------------------------------------------------------------------------- #


class TestPollersPassTheJoinUrlOn:
    """**The seam where a correct parser and a correct bridge still add up to nothing.**

    ``meeting_link`` can resolve the URL and ``bot_client`` can send it, and the bot still
    joins slowly by guessing a form if neither poller actually threads the value through. That
    failure is invisible: the join succeeds, just after the bridge has spent four polls on the
    wrong page — so nothing in a log says the link was dropped.
    """

    async def test_the_inbox_poller_sends_the_url_it_parsed(
        self, monkeypatch, tmp_path
    ) -> None:
        from app import gmail_poller as module
        from app.config import Settings
        from app.gmail_poller import GmailPoller
        from app.gmail_state import ProcessedMessageStore

        sent: list[dict] = []

        async def fake_trigger(meeting_code, bridge_settings, **kwargs):
            sent.append({"meeting_code": meeting_code, **kwargs})

        async def fake_active(meeting_code, bridge_settings):
            return False

        monkeypatch.setattr(module, "trigger_bot_join", fake_trigger)
        monkeypatch.setattr(module, "has_active_session", fake_active)

        settings = Settings(
            google={"auth_mode": "oauth", "oauth_client_secret_file": "unused.json"},
            gmail=SETTINGS,
        )
        message = _message()
        message["internalDate"] = str(int(datetime.now(UTC).timestamp() * 1000))

        class FakeGmail:
            def build_query(self) -> str:
                return ""

            async def list_invite_candidates(self) -> list[str]:
                return ["msg-t1"]

            async def get_message(self, message_id: str) -> dict:
                return message

            async def mark_read(self, message_id: str) -> None:
                pass

        store = ProcessedMessageStore(tmp_path / "processed.json", 500)
        poller = GmailPoller(FakeGmail(), store, settings)

        assert await poller.poll_once() == 1
        assert sent == [
            {
                "meeting_code": MEETING_ID,
                "platform": PLATFORM_TEAMS,
                "passcode": PASSCODE,
                "meeting_url": SHORT_LINK,
            }
        ]

    async def test_the_calendar_scheduler_sends_the_url_it_parsed(
        self, monkeypatch, tmp_path
    ) -> None:
        from app import scheduler as module
        from app.config import Settings
        from app.models import CalendarEvent
        from app.state import TriggeredEventStore

        sent: list[dict] = []

        async def fake_trigger(meeting_code, bridge_settings, **kwargs):
            sent.append({"meeting_code": meeting_code, **kwargs})

        monkeypatch.setattr(module, "trigger_bot_join", fake_trigger)

        event = CalendarEvent(
            event_id="evt-t1",
            summary="Weekly sync",
            start=datetime.now(UTC),
            updated="2026-08-20T08:00:00Z",
            meeting_code=MEETING_ID,
            meeting_url=SHORT_LINK,
            platform=PLATFORM_TEAMS,
            passcode=PASSCODE,
        )
        state = TriggeredEventStore(tmp_path / "triggered.json")
        settings = Settings(
            google={"auth_mode": "oauth", "oauth_client_secret_file": "unused.json"}
        )

        await module._run_join(event=event, state=state, settings=settings)

        assert sent[0]["meeting_url"] == SHORT_LINK
        assert sent[0]["platform"] == PLATFORM_TEAMS
        assert sent[0]["passcode"] == PASSCODE

    async def test_a_meet_event_still_sends_no_url(self, monkeypatch, tmp_path) -> None:
        """The other half of the guarantee: the platforms that ignore ``meeting_url`` must not
        start receiving one."""
        from app import scheduler as module
        from app.config import Settings
        from app.models import CalendarEvent
        from app.state import TriggeredEventStore

        sent: list[dict] = []

        async def fake_trigger(meeting_code, bridge_settings, **kwargs):
            sent.append(kwargs)

        monkeypatch.setattr(module, "trigger_bot_join", fake_trigger)

        event = CalendarEvent(
            event_id="evt-m1",
            summary="Sync",
            start=datetime.now(UTC),
            updated="2026-08-20T08:00:00Z",
            meeting_code="abc-defg-hij",
            meeting_url="https://meet.google.com/abc-defg-hij",
        )

        await module._run_join(
            event=event,
            state=TriggeredEventStore(tmp_path / "triggered.json"),
            settings=Settings(
                google={"auth_mode": "oauth", "oauth_client_secret_file": "unused.json"}
            ),
        )

        assert sent[0]["meeting_url"] is None


