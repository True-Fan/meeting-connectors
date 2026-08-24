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
    PLATFORM_TEAMS,
    PLATFORM_ZOOM,
    find_meeting_link,
    find_teams_passcode,
    find_zoom_passcode,
    has_teams_invite_block,
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

TEAMS_PERSONAL_INVITE = """Priya Sharma is inviting you to a Microsoft Teams meeting.

Join the meeting now
https://teams.live.com/meet/9339756425487?p=71cQWhQJ5X8fxHSmVy
Meeting ID: 933 975 642 5487
Passcode: 71cQWhQJ5X8fxHSmVy
________________________________________
Need help? Visit https://teams.live.com/support or get the app at
https://teams.microsoft.com/downloads
"""
"""A personal ("Teams for Life") invitation — the shape this feature was asked for."""

TEAMS_TENANT_INVITE = """________________________________________
Microsoft Teams
Need help?
Join the meeting now
https://teams.microsoft.com/l/meetup-join/19%3ameeting_YmM2MTk%40thread.v2/0?context=%7b%22Tid%22%3a%22aaa%22%2c%22Oid%22%3a%22bbb%22%7d
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
"""A work/school invitation, as Outlook writes it. The link carries a thread id rather than a
number, so the numeric id has to come out of the printed block."""


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


class TestTeams:
    def test_the_short_link_yields_the_id_and_the_passcode_from_the_url(self) -> None:
        """``teams.live.com/meet/<id>?p=<passcode>`` carries everything a join needs."""
        link = find_meeting_link(
            "Join here: https://teams.live.com/meet/9339756425487?p=71cQWhQJ5X8fxHSmVy"
        )

        assert link is not None
        assert link.platform == PLATFORM_TEAMS
        assert link.meeting_number == "9339756425487"
        assert link.passcode == "71cQWhQJ5X8fxHSmVy"
        assert link.url == (
            "https://teams.live.com/meet/9339756425487?p=71cQWhQJ5X8fxHSmVy"
        )

    def test_teams_web_is_the_platform_not_the_graph_connector(self) -> None:
        """``teams`` needs an admin-consented Azure app and a Windows host, in the *organiser's*
        tenant. An invited meeting is by definition somebody else's — often a personal account
        with no tenant at all — so ``teams_web`` is the only route that exists."""
        link = find_meeting_link(TEAMS_PERSONAL_INVITE)

        assert link is not None
        assert link.platform == "teams_web"

    def test_a_full_personal_invitation_parses(self) -> None:
        link = find_meeting_link(TEAMS_PERSONAL_INVITE)

        assert link is not None
        assert link.meeting_number == "9339756425487"
        assert link.passcode == "71cQWhQJ5X8fxHSmVy"

    def test_the_p_parameter_is_the_typed_passcode_unlike_zooms_pwd(self) -> None:
        """**The asymmetry worth stating, because the two look alike.**

        Zoom's ``pwd=`` is an encrypted token its own client exchanges for entry and is rejected
        by the passcode box. Teams' ``p=`` *is* the passcode — the same string the invitation
        prints on its ``Passcode:`` line — so reading it out of the URL is correct here and
        would be a bug there.
        """
        link = find_meeting_link(TEAMS_PERSONAL_INVITE)

        assert link is not None
        assert link.passcode == "71cQWhQJ5X8fxHSmVy"
        assert "Passcode: 71cQWhQJ5X8fxHSmVy" in TEAMS_PERSONAL_INVITE

    @pytest.mark.parametrize(
        "url",
        [
            "https://teams.live.com/meet/9339756425487",
            "https://teams.microsoft.com/meet/9339756425487",
            "http://teams.live.com/meet/9339756425487",
        ],
    )
    def test_both_hosts_and_a_passcodeless_link_resolve(self, url: str) -> None:
        """Which host an invite carries is decided by the organiser's account type, not by
        anything this service can see, so both have to work."""
        link = find_meeting_link(url)

        assert link is not None
        assert link.platform == PLATFORM_TEAMS
        assert link.meeting_number == "9339756425487"
        assert link.passcode is None

    @pytest.mark.parametrize(
        "text",
        [
            "Get the app at https://teams.microsoft.com/downloads",
            "Need help? https://teams.live.com/support",
            "Options: https://teams.microsoft.com/meetingOptions/?organizerId=abc",
            "https://teams.live.com/",
            "Read https://www.microsoft.com/en/microsoft-teams/whats-new",
        ],
    )
    def test_a_footer_link_is_not_a_meeting(self, text: str) -> None:
        """The same failure the Zoom pattern guards against: dialling a download page."""
        assert find_meeting_link(text) is None

    def test_the_url_is_rebuilt_rather_than_carried_out_of_html(self) -> None:
        """**The bridge navigates this string, so junk in it is a failed join.**

        A link lifted out of an HTML part arrives wrapped in an attribute and carrying entities
        and tracking parameters. The scheme, host, id and passcode are the whole of what a join
        needs, so the URL is reassembled from them and everything else is dropped — which is
        also what makes ``&amp;p=`` readable at all.
        """
        html = (
            '<a href="https://teams.live.com/meet/9339756425487'
            '?anon=true&amp;p=71cQWhQJ5X8fxHSmVy">Join the meeting now</a>'
        )

        link = find_meeting_link(html)

        assert link is not None
        assert link.url == (
            "https://teams.live.com/meet/9339756425487?p=71cQWhQJ5X8fxHSmVy"
        )
        assert '"' not in link.url and "&amp;" not in link.url

    def test_a_tenant_invite_takes_its_number_from_the_printed_block(self) -> None:
        """A ``meetup-join`` link carries a thread id and a tenant, never a meeting number —
        the number is printed in the body, and it is what the join is de-duplicated on."""
        link = find_meeting_link(TEAMS_TENANT_INVITE)

        assert link is not None
        assert link.platform == PLATFORM_TEAMS
        assert link.meeting_number == "281442953617"
        assert link.passcode == "aB3dE9"
        assert link.url.startswith("https://teams.microsoft.com/l/meetup-join/")

    def test_a_tenant_link_alone_falls_back_to_the_url_as_the_identifier(self) -> None:
        """The bridge requires a non-empty ``meeting_number`` and accepts a Teams URL there, so
        a link with no printed id is still joinable rather than dropped."""
        link = find_meeting_link(
            "https://teams.microsoft.com/l/meetup-join/19%3ameeting_x%40thread.v2/0?context=%7b%22Tid%22%3a%22a%22%7d"
        )

        assert link is not None
        assert link.meeting_number == link.url
        assert link.meeting_number != ""

    def test_a_trailing_full_stop_is_not_part_of_the_url(self) -> None:
        """A link at the end of a sentence, which is how a human pastes one."""
        link = find_meeting_link(
            "We're on https://teams.microsoft.com/l/meetup-join/19%3ameeting_x"
            "%40thread.v2/0?context=%7b%22Tid%22%3a%22a%22%7d. See you there."
        )

        assert link is not None
        assert not link.url.endswith(".")

    def test_the_dial_in_block_never_supplies_a_passcode(self) -> None:
        """**A confident wrong answer is worse than none here.**

        Teams' phone block carries a *conference id* and a dial-in PIN, neither of which is the
        meeting passcode. Reading a number out of it — which the Zoom one-tap fallback would —
        yields a passcode Teams rejects, turning "no passcode, ask the host" into "wrong
        passcode, refused".
        """
        assert find_teams_passcode("+1 323-555-0199,,123456789# US") is None
        assert find_teams_passcode("Phone conference ID: 123 456 789#") is None

    def test_a_flattened_invitation_does_not_run_the_passcode_into_the_next_block(
        self,
    ) -> None:
        """Gmail's HTML view drops the newlines between blocks, and the block after
        ``Passcode:`` is "Dial in by phone" — so without the break the passcode reads
        ``aB3dE9Dial``, which is passcode-shaped, plausible, and rejected."""
        flattened = (
            "Microsoft TeamsNeed help?Join the meeting now"
            "https://teams.microsoft.com/l/meetup-join/19%3ameeting_x%40thread.v2/0?context=%7bx%7d"
            "Meeting ID: 281 442 953 617Passcode: aB3dE9Dial in by phone"
            "+1 323-555-0199,,123456789# United StatesFind a local number"
        )

        link = find_meeting_link(flattened)

        assert link is not None
        assert link.passcode == "aB3dE9", "ran into the next block"
        assert link.meeting_number == "281442953617"
        assert not link.url.endswith("Meeting"), "swallowed the following label"

    def test_the_underscore_rule_restores_one_break_not_one_per_character(self) -> None:
        """Teams separates its sections with a run of underscores where Zoom uses ``---``.

        **One break, at the start of the run.** A lookbehind of ``\\S`` also matches every
        position *inside* it, which turned Teams' forty-underscore rule into forty lines —
        harmless for the patterns that follow, but it made the repaired text unreadable in a
        log and it is the kind of thing that stops being harmless later. Zoom's ``---`` hid the
        flaw by being exactly three long.
        """
        repaired = restore_line_breaks("Passcode: aB3dE9" + "_" * 12)

        assert repaired == "Passcode: aB3dE9\n" + "_" * 12
        assert restore_line_breaks(repaired) == repaired, "not idempotent"

    def test_a_long_zoom_rule_gets_one_break_too(self) -> None:
        """The same fix, on the pattern that was already there — Zoom's rule is only ever
        three dashes in practice, so this was latent rather than observed."""
        assert restore_line_breaks("Passcode: 278999-----One") == (
            "Passcode: 278999\n-----One"
        )


class TestTeamsInviteBlock:
    """What counts as a Teams *invitation* rather than a message mentioning Teams.

    The gate for the body route, which accepts arbitrary senders — so the negative cases are
    the ones that matter.
    """

    def test_a_real_invitation_qualifies(self) -> None:
        assert has_teams_invite_block(TEAMS_PERSONAL_INVITE)
        assert has_teams_invite_block(TEAMS_TENANT_INVITE)

    def test_a_passcode_bearing_short_link_is_an_invitation_on_its_own(self) -> None:
        """No Zoom equivalent: ``?p=`` is what the *Copy link* button produces and it is the
        whole of what a join needs, so it does not also need a labelled block underneath it."""
        assert has_teams_invite_block(
            "https://teams.live.com/meet/9339756425487?p=71cQWhQJ5X8fxHSmVy"
        )

    def test_a_bare_link_with_no_passcode_and_no_block_is_not_an_invitation(self) -> None:
        """A colleague reminiscing about a meeting must not move the bot."""
        assert not has_teams_invite_block(
            "we used to meet at https://teams.live.com/meet/9339756425487"
        )

    def test_a_tenant_link_needs_a_labelled_line(self) -> None:
        bare = (
            "old thread: https://teams.microsoft.com/l/meetup-join/"
            "19%3ameeting_x%40thread.v2/0?context=%7bx%7d"
        )

        assert not has_teams_invite_block(bare)
        assert has_teams_invite_block(bare + "\nMeeting ID: 281 442 953 617")

    def test_talking_about_teams_without_a_link_is_not_an_invitation(self) -> None:
        assert not has_teams_invite_block(
            "Microsoft Teams is down again. Meeting ID: 281 442 953 617"
        )

    def test_a_zoom_invitation_is_not_a_teams_one(self) -> None:
        assert not has_teams_invite_block(ZOOM_INVITE)


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

    def test_a_teams_invitation_is_not_read_as_a_zoom_meeting_number(self) -> None:
        """**The reason Teams is tried before Zoom, and the bug it prevents.**

        Both platforms print ``Meeting ID: 281 442 953 617`` in the same words and the same
        digit grouping, and Zoom's last-resort pattern reads a bare id out of prose. Offered to
        Zoom first, a Teams invitation resolves to a ``zoom_web`` join of a number Zoom has
        never heard of — which fails while pointing at the wrong platform entirely.
        """
        link = find_meeting_link(TEAMS_TENANT_INVITE)

        assert link is not None
        assert link.platform == PLATFORM_TEAMS

    def test_a_teams_invitation_whose_link_was_stripped_is_not_claimed_by_zoom(self) -> None:
        """The same collision with nothing left to disambiguate but the word "Teams".

        Refusing is the answer rather than guessing: ``None`` logs a warning that the invite
        carried no joinable link, where a wrong-platform join dials a meeting that does not
        exist.
        """
        assert (
            find_meeting_link(
                "Microsoft Teams\nMeeting ID: 281 442 953 617\nPasscode: aB3dE9"
            )
            is None
        )

    def test_zooms_prose_fallback_still_works_where_it_always_did(self) -> None:
        """The guard above must cost a genuine Zoom invite nothing — those say "Zoom"."""
        link = find_meeting_link(
            "Join Zoom Meeting\nMeeting ID: 838 4321 2151\nPasscode: abc123"
        )

        assert link is not None
        assert link.platform == PLATFORM_ZOOM
        assert link.meeting_number == "83843212151"

    def test_meet_wins_over_teams_inside_one_text(self) -> None:
        """Meet's pattern is the strictest of the three, so an unambiguous Meet code should not
        lose to a Teams link mentioned in the same prose."""
        link = find_meeting_link(
            "Join https://meet.google.com/veg-fkxv-rhg "
            "(we used to use https://teams.live.com/meet/9339756425487?p=abc)"
        )

        assert link is not None
        assert link.platform == PLATFORM_GOOGLE_MEET

    def test_an_earlier_source_wins_for_teams_too(self) -> None:
        """A Teams event whose description still carries last week's Meet link resolves to
        Teams, because the structured field is passed first."""
        link = find_meeting_link(
            "https://teams.live.com/meet/9339756425487?p=abc",  # location
            "Agenda copied from last week: https://meet.google.com/veg-fkxv-rhg",
        )

        assert link is not None
        assert link.platform == PLATFORM_TEAMS
        assert link.meeting_number == "9339756425487"
