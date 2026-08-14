"""Filter and extraction tests — the part that decides, from an email alone, whether to put
a bot into a meeting.

The negative cases matter as much as the positive one: everything reaching
``trigger_bot_join`` came through here, so a parser that is too generous is the difference
between a helpful bot and one that dials a link out of an email footer.
"""

from __future__ import annotations

import base64
import quopri

from app.config import GmailSettings
from app.invite_parser import parse_invite

SETTINGS = GmailSettings(enabled=True)


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def _message(
    *,
    sender: str = '"Google Meet" <meetings-noreply@google.com>',
    subject: str = "Happening now: Weekly sync",
    body: str = "Join at https://meet.google.com/abc-defg-hij",
    snippet: str = "",
    internal_date: str = "1755000000000",
) -> dict:
    return {
        "id": "msg-1",
        "threadId": "thread-1",
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


def test_happening_now_invite_is_parsed():
    invite = parse_invite(_message(), SETTINGS)

    assert invite is not None
    assert invite.meeting_code == "abc-defg-hij"
    assert invite.meeting_url == "https://meet.google.com/abc-defg-hij"
    assert invite.sender == "meetings-noreply@google.com"
    assert invite.internal_date_ms == 1755000000000


def test_video_call_invite_subject_is_parsed():
    invite = parse_invite(_message(subject="Priya is inviting you to a video call"), SETTINGS)

    assert invite is not None
    assert invite.meeting_code == "abc-defg-hij"


def test_subject_match_is_case_insensitive():
    assert parse_invite(_message(subject="HAPPENING NOW: standup"), SETTINGS) is not None


def test_other_sender_is_rejected():
    """The security boundary: a human forwarding a Meet link must not drive the bot."""
    assert parse_invite(_message(sender="colleague@example.com"), SETTINGS) is None


def test_display_name_cannot_spoof_the_sender():
    """``From`` is parsed to a bare address, so a lookalike display name gains nothing."""
    message = _message(sender='"meetings-noreply@google.com" <attacker@evil.example>')

    assert parse_invite(message, SETTINGS) is None


def test_lookalike_domain_is_rejected():
    message = _message(sender="meetings-noreply@google.com.evil.example")

    assert parse_invite(message, SETTINGS) is None


def test_unrelated_meet_mail_is_ignored():
    """Meet uses the same sender for mail that must not trigger a join."""
    assert parse_invite(_message(subject="Your recording is ready"), SETTINGS) is None


def test_footer_links_are_not_treated_as_meeting_codes():
    """Why the code regex is stricter than ``meet\\.google\\.com/([a-z-]+)``.

    The loose version matches the support link Google puts in its own footer and hands
    "support" to the bridge as a meeting number.
    """
    message = _message(
        body="Learn more at https://meet.google.com/support or https://meet.google.com/landing"
    )

    assert parse_invite(message, SETTINGS) is None


def test_real_code_is_found_alongside_footer_links():
    message = _message(
        body=(
            "Join: https://meet.google.com/abc-defg-hij?authuser=0\n"
            "Help: https://meet.google.com/support\n"
        )
    )

    invite = parse_invite(message, SETTINGS)

    assert invite is not None
    assert invite.meeting_code == "abc-defg-hij"


def test_quoted_printable_soft_break_still_yields_a_code():
    """QP wraps at 76 chars; a split URL would otherwise silently match nothing."""
    raw = "Join now: https://meet.google.com/abc-defg-hij for the call"
    message = _message(body=quopri.encodestring(raw.encode(), quotetabs=False).decode())

    invite = parse_invite(message, SETTINGS)

    assert invite is not None
    assert invite.meeting_code == "abc-defg-hij"


def test_code_is_found_in_a_nested_multipart_html_body():
    message = {
        "id": "msg-2",
        "threadId": "thread-2",
        "snippet": "",
        "internalDate": "1755000000000",
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [
                {"name": "From", "value": "Google Meet <meetings-noreply@google.com>"},
                {"name": "Subject", "value": "Happening now: Design review"},
            ],
            "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64("No link in this part")}},
                {
                    "mimeType": "multipart/related",
                    "parts": [
                        {
                            "mimeType": "text/html",
                            "body": {
                                "data": _b64(
                                    '<a href="https://meet.google.com/xyz-abcd-efg">Join</a>'
                                )
                            },
                        }
                    ],
                },
            ],
        },
    }

    invite = parse_invite(message, SETTINGS)

    assert invite is not None
    assert invite.meeting_code == "xyz-abcd-efg"


def test_snippet_is_used_when_the_body_carries_no_link():
    message = _message(body="", snippet="Happening now https://meet.google.com/qrs-tuvw-xyz")

    invite = parse_invite(message, SETTINGS)

    assert invite is not None
    assert invite.meeting_code == "qrs-tuvw-xyz"


def test_invite_without_any_meet_link_is_ignored():
    assert parse_invite(_message(body="No link here at all"), SETTINGS) is None


def test_canonical_code_wins_over_a_malformed_one():
    message = _message(
        body="old: https://meet.google.com/a-b-c new: https://meet.google.com/abc-defg-hij"
    )

    invite = parse_invite(message, SETTINGS)

    assert invite is not None
    assert invite.meeting_code == "abc-defg-hij"


def test_missing_internal_date_is_zero_not_a_crash():
    message = _message()
    del message["internalDate"]

    invite = parse_invite(message, SETTINGS)

    assert invite is not None
    assert invite.internal_date_ms == 0


def test_allowed_senders_are_configurable():
    settings = GmailSettings(enabled=True, allowed_senders=("invites@corp.example",))

    assert parse_invite(_message(sender="invites@corp.example"), settings) is not None
    assert parse_invite(_message(), settings) is None
