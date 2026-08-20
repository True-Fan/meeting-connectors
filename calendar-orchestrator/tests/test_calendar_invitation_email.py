"""A Google Calendar invitation arriving as mail.

**The third invite shape, and the only one with no handle on who sent it or what it is
called.** Google sends the invitation *as the organiser*, so the ``From:`` is an ordinary
personal address; and the subject is the event's own title — ``test zoom``, ``Standup``,
anything, in any language. Neither the allow-list nor the subject markers can catch it.

What it does have is an ``invite.ics`` part. That is a structural fact rather than a string
somebody chose, and it carries the one thing that makes this safe to act on: **when the
meeting is**.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

import pytest
from app.config import GmailSettings
from app.gmail_service import GmailService
from app.invite_parser import parse_invite
from app.meeting_link import PLATFORM_ZOOM

ORGANISER = '"Any Organiser" <organiser@example.com>'
EVENT_SUBJECT = "Invitation: test zoom @ Thu 20 Aug 2026 4:04pm - 5:04pm (IST)"

# The Zoom block Google Calendar puts in DESCRIPTION, folded at 75 octets exactly as
# iCalendar requires — which is what truncates the join URL if the reader does not unfold.
FOLDED_DESCRIPTION = (
    "Someone is inviting you to a scheduled Zoom meeting.\\nJoin Zo\r\n"
    " om Meeting\\nhttps://us05web.zoom.us/j/85273228350?pwd=JFncgISjOovGgLPwFSZ4gtO1\r\n"
    " MzEMNr.1\\nMeeting ID: 852 7322 8350\\nPasscode: f4eVwN"
)
FULL_URL = (
    "https://us05web.zoom.us/j/85273228350?pwd=JFncgISjOovGgLPwFSZ4gtO1MzEMNr.1"
)


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def _ics(start: datetime, end: datetime, *, method: str = "REQUEST") -> str:
    return (
        "BEGIN:VCALENDAR\r\n"
        f"METHOD:{method}\r\n"
        "BEGIN:VEVENT\r\n"
        f"DTSTART:{start:%Y%m%dT%H%M%SZ}\r\n"
        f"DTEND:{end:%Y%m%dT%H%M%SZ}\r\n"
        "SUMMARY:test zoom\r\n"
        f"DESCRIPTION:{FOLDED_DESCRIPTION}\r\n"
        "ORGANIZER;CN=Any Organiser:mailto:organiser@example.com\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR"
    )


def _message(
    start: datetime,
    end: datetime,
    *,
    sender: str = ORGANISER,
    subject: str = EVENT_SUBJECT,
    method: str = "REQUEST",
    with_ics: bool = True,
) -> dict:
    parts = [
        {
            "mimeType": "text/plain",
            "body": {"data": _b64("Someone is inviting you to a scheduled Zoom meeting.")},
        }
    ]
    if with_ics:
        parts.append(
            {
                "mimeType": "text/calendar",
                "filename": "invite.ics",
                "body": {"data": _b64(_ics(start, end, method=method))},
            }
        )
    return {
        "id": "cal-1",
        "threadId": "t",
        "snippet": "",
        "internalDate": "1755000000000",
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [
                {"name": "From", "value": sender},
                {"name": "Subject", "value": subject},
            ],
            "parts": parts,
        },
    }


@pytest.fixture
def now() -> datetime:
    return datetime.now(UTC)


DEFAULTS = GmailSettings(enabled=True)


class TestLiveInvitation:
    def test_an_invitation_for_a_meeting_in_progress_is_joined(self, now: datetime) -> None:
        """Works on the default configuration — there is nothing to allow-list."""
        invite = parse_invite(
            _message(now - timedelta(minutes=2), now + timedelta(hours=1)), DEFAULTS
        )

        assert invite is not None
        assert invite.platform == PLATFORM_ZOOM
        assert invite.meeting_code == "85273228350"
        assert invite.passcode == "f4eVwN"

    @pytest.mark.parametrize(
        "organiser",
        [
            "organiser@example.com",
            '"Some Person" <someone.else@example.org>',
            "team-lead@a-company.example",
            "MIXEDcase@Example.Com",
        ],
    )
    def test_any_organiser_can_send_it(self, now: datetime, organiser: str) -> None:
        """**The address is unknowable in advance, so it must not be part of the decision.**

        Google sends a calendar invitation as whoever created the event. Parametrised over
        unrelated addresses rather than asserted once, because a single fixture cannot
        distinguish "any organiser works" from "this organiser happens to work" — and nothing
        about these appears anywhere in the codebase.
        """
        invite = parse_invite(
            _message(
                now - timedelta(minutes=2), now + timedelta(hours=1), sender=organiser
            ),
            DEFAULTS,
        )

        assert invite is not None, f"{organiser} should have been accepted on the ics alone"
        assert invite.meeting_code == "85273228350"

    @pytest.mark.parametrize(
        "subject",
        ["test zoom", "Invitation: Standup @ Mon 1pm", "会議", "", "Re: Fwd: whatever"],
    )
    def test_any_event_title_works_as_the_subject(self, now: datetime, subject: str) -> None:
        """The subject is the event's own name, so it carries no information to filter on."""
        invite = parse_invite(
            _message(
                now - timedelta(minutes=2), now + timedelta(hours=1), subject=subject
            ),
            DEFAULTS,
        )

        assert invite is not None

    def test_the_join_url_survives_ics_line_folding(self, now: datetime) -> None:
        """**The silent corruption this format invites.**

        iCalendar wraps at 75 octets with a continuation line starting with a space, and a
        Zoom join URL is longer than that. Read without unfolding, the URL is truncated
        mid-token — and the meeting *number* still comes out right, because it sits near the
        front. So the bot joins the correct meeting holding a link that is wrong, and nothing
        anywhere reports a problem.
        """
        invite = parse_invite(
            _message(now - timedelta(minutes=2), now + timedelta(hours=1)), DEFAULTS
        )

        assert invite is not None
        assert invite.url == FULL_URL
        assert invite.url.endswith("MzEMNr.1"), "the token was truncated at the fold"

    def test_it_is_accepted_a_little_before_the_start(self, now: datetime) -> None:
        """Somebody creating the event minutes before the meeting is the ordinary case."""
        invite = parse_invite(
            _message(now + timedelta(minutes=3), now + timedelta(hours=1)), DEFAULTS
        )

        assert invite is not None


class TestSchedulingBelongsToTheCalendarPoller:
    """**The condition that stops the inbox path stealing the scheduler's job.**

    An invitation to next Tuesday arrives *now*. Every other filter in the poller would pass
    it — the mail is fresh, the link is real, the sender is the genuine organiser — and the
    bot would join a meeting six days early and sit there. So the email path claims only what
    it alone can do: react to a meeting already running.
    """

    def test_an_invitation_for_next_week_is_left_alone(self, now: datetime) -> None:
        assert (
            parse_invite(
                _message(now + timedelta(days=6), now + timedelta(days=6, hours=1)), DEFAULTS
            )
            is None
        )

    def test_an_invitation_for_a_finished_meeting_is_left_alone(self, now: datetime) -> None:
        assert (
            parse_invite(
                _message(now - timedelta(hours=3), now - timedelta(hours=2)), DEFAULTS
            )
            is None
        )

    def test_unreadable_timing_is_left_alone(self, now: datetime) -> None:
        """The scheduler owns anything this cannot positively identify as live."""
        message = _message(now, now + timedelta(hours=1))
        broken = (
            "BEGIN:VCALENDAR\r\nMETHOD:REQUEST\r\nBEGIN:VEVENT\r\n"
            "SUMMARY:x\r\nEND:VEVENT\r\nEND:VCALENDAR"
        )
        message["payload"]["parts"][1]["body"]["data"] = _b64(broken)

        assert parse_invite(message, DEFAULTS) is None


class TestWhatIsNotAnInvitation:
    def test_a_cancellation_does_not_join(self, now: datetime) -> None:
        """``METHOD:CANCEL`` carries the same event, link and times as the invitation.

        Acting on it would put the bot into a meeting that has just been called off.
        """
        assert (
            parse_invite(
                _message(
                    now - timedelta(minutes=2), now + timedelta(hours=1), method="CANCEL"
                ),
                DEFAULTS,
            )
            is None
        )

    def test_a_reply_does_not_join(self, now: datetime) -> None:
        assert (
            parse_invite(
                _message(
                    now - timedelta(minutes=2), now + timedelta(hours=1), method="REPLY"
                ),
                DEFAULTS,
            )
            is None
        )

    def test_the_same_mail_without_an_ics_is_sender_gated_again(self, now: datetime) -> None:
        """The ``.ics`` is the whole identification. Remove it and the ordinary rules apply —
        an arbitrary organiser with an event-title subject matches nothing."""
        assert (
            parse_invite(
                _message(
                    now - timedelta(minutes=2), now + timedelta(hours=1), with_ics=False
                ),
                DEFAULTS,
            )
            is None
        )

    def test_the_feature_can_be_switched_off(self, now: datetime) -> None:
        """Both open routes have to be closed to see it.

        With the ics route off but the body route on, the same message is still accepted —
        on the Zoom invitation block inside the event description rather than on the ics.
        """
        assert (
            parse_invite(
                _message(now - timedelta(minutes=2), now + timedelta(hours=1)),
                GmailSettings(
                    enabled=True,
                    accept_calendar_invitations=False,
                    accept_zoom_invite_bodies=False,
                ),
            )
            is None
        )


class TestRetrievability:
    def test_the_query_asks_for_ics_attachments(self) -> None:
        """**Without this the feature is implemented and never runs.**

        A calendar invitation matches no ``from:`` (the organiser is arbitrary) and no
        ``subject:`` (the subject is the event's title), so the message is never *fetched* —
        and the parser, which would have accepted it, never sees it.
        """
        service = GmailService.__new__(GmailService)
        service._settings = DEFAULTS

        assert "filename:ics" in service.build_query()

    def test_switching_the_feature_off_drops_the_term(self) -> None:
        service = GmailService.__new__(GmailService)
        service._settings = GmailSettings(enabled=True, accept_calendar_invitations=False)

        assert "filename:ics" not in service.build_query()
