"""Recognising a joinable meeting in free text.

**The negative cases carry most of the weight here**, for the reason ``test_invite_parser``
says: everything downstream of a match puts a bot into somebody's meeting, and both an invite
email and a calendar event are full of links that are not the meeting — a download page, a
support article, a sign-in link, last week's agenda pasted into the description.

The fixtures are real invite text with the identifying details changed, not minimal strings,
because the failures this module has to survive are all about the surrounding noise rather
than the link itself.
"""

from __future__ import annotations

import pytest
from app.meeting_link import (
    PLATFORM_GOOGLE_MEET,
    PLATFORM_ZOOM,
    find_meeting_link,
    find_zoom_passcode,
    restore_line_breaks,
)

ZOOM_INVITE = """Someone is inviting you to a scheduled Zoom meeting.

Topic: Someone's Zoom Meeting
Time: Aug 20, 2026 01:30 PM India

Join Zoom Meeting
https://us05web.zoom.us/j/83843212151?pwd=05sFhA1N0w3Xfy6onXiDf45agweVYL.1

Meeting ID: 838 4321 2151
Passcode: 139601

---
One tap mobile
+16699009128,,83843212151#,,,,*139601# US (San Jose)

Zoom is a registered trademark. Download at https://zoom.us/download
Need help? Visit https://zoom.us/support or sign in at https://zoom.us/signin
"""

MEET_INVITE = """Priya is inviting you to a video call.
Join: https://meet.google.com/veg-fkxv-rhg
Learn more at meet.google.com/support, or start one at meet.google.com/new
"""


class TestZoom:
    def test_a_real_invite_yields_number_and_typed_passcode(self) -> None:
        link = find_meeting_link(ZOOM_INVITE)

        assert link is not None
        assert link.platform == PLATFORM_ZOOM
        assert link.meeting_number == "83843212151"
        assert link.passcode == "139601"
        assert link.url.startswith("https://us05web.zoom.us/j/83843212151")

    def test_the_pwd_token_is_not_used_as_the_passcode(self) -> None:
        """**The distinction the bridge's joiner depends on.**

        ``pwd=`` is an encrypted token Zoom's own client exchanges for entry. The bridge types
        into the passcode box instead, which rejects it — so using the token here would turn
        "no passcode, ask the host" into "wrong passcode, refused", which is worse and looks
        like a different bug.
        """
        link = find_meeting_link(ZOOM_INVITE)

        assert link is not None
        assert link.passcode == "139601"
        assert "05sFhA1N0w3Xfy6onXiDf45agweVYL" not in (link.passcode or "")

    @pytest.mark.parametrize(
        "url",
        [
            "https://us05web.zoom.us/j/83843212151",
            "https://app.zoom.us/wc/83843212151/join",
            "https://us05web.zoom.us/s/83843212151",
            "https://zoom.us/j/83843212151",
            "zoommtg://us05web.zoom.us/join?confno=83843212151&pwd=xyz",
        ],
    )
    def test_every_join_link_shape_resolves_to_the_same_meeting(self, url: str) -> None:
        link = find_meeting_link(url)

        assert link is not None
        assert link.platform == PLATFORM_ZOOM
        assert link.meeting_number == "83843212151"

    @pytest.mark.parametrize(
        "text",
        [
            "Download at https://zoom.us/download",
            "Sign in at https://zoom.us/signin",
            "Help: https://zoom.us/support",
            "Plans at https://zoom.us/pricing",
            "https://zoom.us/",
            "Read https://blog.zoom.us/whats-new",
        ],
    )
    def test_a_footer_link_is_not_a_meeting(self, text: str) -> None:
        """The failure this pattern exists to prevent: dialling a support page as a meeting."""
        assert find_meeting_link(text) is None

    def test_a_meeting_id_in_prose_is_a_last_resort(self) -> None:
        """Some forwarded invites lose the link and keep the block underneath it."""
        link = find_meeting_link("Meeting ID: 838 4321 2151\nPasscode: abc123")

        assert link is not None
        assert link.meeting_number == "83843212151"
        assert link.passcode == "abc123"

    def test_the_one_tap_line_supplies_a_passcode_when_the_label_is_gone(self) -> None:
        """Mail clients reformat the passcode line away more often than the dial-in block."""
        assert find_zoom_passcode("+16699009128,,83843212151#,,,,*774411# US") == "774411"

    def test_no_passcode_is_none_rather_than_a_guess(self) -> None:
        link = find_meeting_link("Join https://us05web.zoom.us/j/83843212151")

        assert link is not None
        assert link.passcode is None


FLATTENED = (
    "Someone is inviting you to a scheduled Zoom meeting.Topic: Someone's "
    "Zoom MeetingJoin Zoom Meetinghttps://zoom.us/j/96353000755?pwd=W4TFgDjQeW5TCybp2lNvUQhPSLaiAa.1"
    "Meeting agendahttps://docs.zoom.us/agenda/doc/338b23ab-6e1b-4fb0-815e-2f1cf3648cb2"
    "Meeting chat linkhttps://zoom.us/launch/jc/96353000755Meeting ID: 963 5300 0755"
    "Passcode: 278999---One tap mobile+13052241968,,96353000755#,,,,*278999# US"
    "+13092053325,,96353000755#,,,,*278999# US---Join by SIP• 96353000755@zoomcrc.com"
    "Passcode: 278999Join instructionshttps://zoom.us/meetings/96353000755/invitations?"
    "signature=f5SneF9DmmJvrx9AQnUfz2-DEBXQcPuzkKuGYufXSTY"
)
"""A real invitation as Gmail's HTML view renders it: every newline between blocks gone."""


class TestFlattenedInvitations:
    """**Silent corruption when a renderer drops the newlines.**

    Zoom's invitation is a stack of one-line blocks and every pattern in the module ends at
    whitespace — because that is where a value ends in the format as written. Flattened,
    there is no whitespace to find and the patterns run on.

    Both failures produce *plausible* output, which is what makes them dangerous: a
    passcode-shaped string and a URL-shaped string. Nothing downstream can tell they are
    wrong; the bot joins and Zoom rejects the passcode.
    """

    def test_the_passcode_stops_where_the_value_stops(self) -> None:
        assert find_zoom_passcode(FLATTENED) == "278999", "ran into the next block"

    def test_the_join_url_does_not_swallow_the_next_label(self) -> None:
        link = find_meeting_link(FLATTENED)

        assert link is not None
        assert link.url == (
            "https://zoom.us/j/96353000755?pwd=W4TFgDjQeW5TCybp2lNvUQhPSLaiAa.1"
        )
        assert not link.url.endswith("Meeting"), "swallowed the following label"

    def test_the_meeting_number_and_passcode_are_both_right(self) -> None:
        link = find_meeting_link(FLATTENED)

        assert link is not None
        assert link.meeting_number == "96353000755"
        assert link.passcode == "278999"

    def test_the_sip_and_instruction_links_are_not_mistaken_for_the_meeting(self) -> None:
        """``/launch/jc/`` and ``/meetings/…/invitations`` are neither ``/j/`` nor ``/wc/``."""
        link = find_meeting_link(FLATTENED)

        assert link is not None
        assert "/launch/" not in link.url
        assert "invitations" not in link.url

    def test_restoring_breaks_leaves_intact_text_alone(self) -> None:
        """The same body often arrives twice — flattened as HTML and intact as plain text.

        Repairing one must not damage the other, so the repair only inserts a break where
        one is missing and is idempotent.
        """
        intact = (
            "Join Zoom Meeting\n"
            "https://zoom.us/j/96353000755?pwd=abc.1\n"
            "Meeting ID: 963 5300 0755\n"
            "Passcode: 278999\n"
        )

        assert restore_line_breaks(intact) == intact
        assert restore_line_breaks(restore_line_breaks(FLATTENED)) == restore_line_breaks(
            FLATTENED
        )

    def test_an_alphanumeric_passcode_survives_flattening(self) -> None:
        """The digits-only case could be rescued by the dial-in fallback; this one cannot."""
        body = (
            "https://us05web.zoom.us/j/85273228350?pwd=tok.1"
            "Meeting ID: 852 7322 8350Passcode: f4eVwN---One tap mobile+1305,,#"
        )

        assert find_zoom_passcode(body) == "f4eVwN"


class TestGoogleMeet:
    def test_a_real_invite_still_parses_exactly_as_before(self) -> None:
        link = find_meeting_link(MEET_INVITE)

        assert link is not None
        assert link.platform == PLATFORM_GOOGLE_MEET
        assert link.meeting_number == "veg-fkxv-rhg"
        assert link.url == "https://meet.google.com/veg-fkxv-rhg"
        assert link.passcode is None, "Meet has no passcode concept"

    def test_the_footer_links_are_still_rejected(self) -> None:
        assert find_meeting_link("meet.google.com/support meet.google.com/new") is None

    def test_a_canonical_code_wins_over_an_odd_one(self) -> None:
        text = "old: meet.google.com/ab-cd-ef then: meet.google.com/veg-fkxv-rhg"

        link = find_meeting_link(text)

        assert link is not None
        assert link.meeting_number == "veg-fkxv-rhg"


class TestPrecedence:
    def test_meet_wins_over_zoom_inside_one_text(self) -> None:
        """Meet's pattern is the stricter of the two, so an unambiguous Meet code should not
        lose to a Zoom link mentioned in the same prose."""
        link = find_meeting_link(
            "Join https://meet.google.com/veg-fkxv-rhg "
            "(we used to use https://us05web.zoom.us/j/83843212151)"
        )

        assert link is not None
        assert link.platform == PLATFORM_GOOGLE_MEET

    def test_an_earlier_source_wins_over_a_later_one(self) -> None:
        """**The bug that concatenating the sources would hide.**

        A calendar event passes its structured conferencing field first and its free-text
        description last. A Zoom event whose description still carries last week's Meet link
        must resolve to Zoom — joining everything into one string loses that, because Meet is
        tried before Zoom *within* a text and the two are no longer distinguishable.
        """
        link = find_meeting_link(
            "https://us05web.zoom.us/j/83843212151",  # conferenceData
            "Agenda copied from last week: https://meet.google.com/veg-fkxv-rhg",  # description
        )

        assert link is not None
        assert link.platform == PLATFORM_ZOOM
        assert link.meeting_number == "83843212151"

    def test_a_later_source_is_used_when_earlier_ones_are_empty(self) -> None:
        link = find_meeting_link("", "   ", "Join https://us05web.zoom.us/j/83843212151")

        assert link is not None
        assert link.meeting_number == "83843212151"

    def test_nothing_joinable_is_none(self) -> None:
        assert find_meeting_link("Lunch with Priya, 1pm, the usual place") is None
        assert find_meeting_link() is None
        assert find_meeting_link("") is None
