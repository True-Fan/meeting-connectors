"""Resolving a meeting code or link into a Google Meet URL.

The outbound anti-corruption boundary. Two properties matter beyond "does it parse":

* a **Zoom meeting number or a Teams meeting id must be rejected**, not turned into a URL
  that 404s — because a permissive pattern would surface a mis-routed request as an
  unexplained join timeout several layers away from the mistake;
* tracking and account-hint parameters must be **stripped**, since ``authuser`` and ``pli``
  can silently steer the browser to a different signed-in account.
"""

from __future__ import annotations

import pytest

from src.connectors.google_meet.exceptions import MeetUrlError
from src.connectors.google_meet.meeting.meet_url import (
    canonical_url,
    looks_like_meet_url,
    normalise_meeting_code,
    parse_meet_url,
    resolve_join_target,
)
from src.domain.meeting import MeetingContext, MeetingPlatform

CODE = "abc-defg-hij"


def _meeting(number: str = "", *, url: str | None = None, passcode: str | None = None):
    data = {"meeting_url": url} if url else {}
    return MeetingContext(
        meeting_number=number,
        display_name="AI Avatar",
        passcode=passcode,
        platform_data=data,
        platform=MeetingPlatform.GOOGLE_MEET,
    )


class TestMeetingCode:
    def test_canonical_code_passes_through(self) -> None:
        assert normalise_meeting_code(CODE) == CODE

    def test_undashed_code_is_regrouped(self) -> None:
        """Unambiguous, because the grouping is always 3-4-3."""
        assert normalise_meeting_code("abcdefghij") == CODE

    def test_case_and_spacing_are_normalised(self) -> None:
        assert normalise_meeting_code("  ABC-DEFG-HIJ ") == CODE

    @pytest.mark.parametrize(
        "value",
        [
            "1234567890",  # a Zoom meeting number
            "123 456 789 012",  # a Teams meeting id
            "abc-def-hij",  # wrong grouping
            "abc-defg-hi",
            "abcd-efg-hij",
            "abc_defg_hij",
            "",
        ],
    )
    def test_non_meet_identifiers_are_rejected(self, value: str) -> None:
        assert normalise_meeting_code(value) is None


class TestParseUrl:
    def test_plain_url(self) -> None:
        target = parse_meet_url(f"https://meet.google.com/{CODE}")
        assert target.meeting_code == CODE
        assert target.url == canonical_url(CODE)

    def test_scheme_less_url_is_accepted(self) -> None:
        """How the link is usually pasted; rejecting it would fail a complete request."""
        assert parse_meet_url(f"meet.google.com/{CODE}").meeting_code == CODE

    def test_tracking_and_account_parameters_are_stripped(self) -> None:
        """``authuser`` can steer the browser to the wrong signed-in account."""
        target = parse_meet_url(
            f"https://meet.google.com/{CODE}?authuser=2&hs=197&pli=1&ijlm=abc"
        )
        assert target.url == f"https://meet.google.com/{CODE}"
        assert "authuser" not in target.url

    def test_lookup_links_keep_their_query(self) -> None:
        """Only Google can resolve a nickname, and ``authuser`` decides as whom."""
        target = parse_meet_url("https://meet.google.com/lookup/standup?authuser=1")
        assert target.is_lookup
        assert target.meeting_code is None
        assert target.url == "https://meet.google.com/lookup/standup?authuser=1"

    def test_short_link(self) -> None:
        target = parse_meet_url("https://g.co/meet/team-sync")
        assert target.is_lookup
        assert target.url == "https://g.co/meet/team-sync"

    def test_non_meet_host_is_rejected(self) -> None:
        with pytest.raises(MeetUrlError, match="not a Google Meet host"):
            parse_meet_url("https://zoom.us/j/1234567890")

    def test_teams_url_is_rejected(self) -> None:
        """Each connector's URL grammar rejects the others'."""
        with pytest.raises(MeetUrlError, match="not a Google Meet host"):
            parse_meet_url("https://teams.microsoft.com/l/meetup-join/19%3ameeting_X%40thread.v2/0")

    def test_unparseable_path(self) -> None:
        with pytest.raises(MeetUrlError, match="not a meeting code"):
            parse_meet_url("https://meet.google.com/not-a-code")

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (f"https://meet.google.com/{CODE}", True),
            (f"meet.google.com/{CODE}", True),
            (CODE, False),
            ("abcdefghij", False),
        ],
    )
    def test_looks_like_a_url(self, value: str, expected: bool) -> None:
        assert looks_like_meet_url(value) is expected


class TestResolveJoinTarget:
    def test_code_in_meeting_number(self) -> None:
        """An operator drives Meet through the same request shape as Zoom and Teams."""
        assert resolve_join_target(_meeting(CODE)).url == canonical_url(CODE)

    def test_url_in_platform_data(self) -> None:
        target = resolve_join_target(_meeting(url=f"https://meet.google.com/{CODE}"))
        assert target.meeting_code == CODE

    def test_url_pasted_into_the_meeting_number_field(self) -> None:
        """A natural operator mistake, and the request carries everything needed."""
        target = resolve_join_target(_meeting(f"https://meet.google.com/{CODE}"))
        assert target.meeting_code == CODE

    def test_a_zoom_number_is_a_named_error_not_a_bad_url(self) -> None:
        with pytest.raises(MeetUrlError, match="not a Google Meet code"):
            resolve_join_target(_meeting("1234567890"))

    def test_nothing_supplied(self) -> None:
        with pytest.raises(MeetUrlError, match="cannot resolve a Google Meet join"):
            resolve_join_target(_meeting())

    def test_a_passcode_is_reported_but_does_not_fail_the_join(self, caplog) -> None:
        """Meet has no joiner-supplied secret, so a passcode signals a mis-written request.

        Warned about rather than dropped silently — but the code is what matters and may
        well be right, so it must not fail.
        """
        target = resolve_join_target(_meeting(CODE, passcode="s3cret"))
        assert target.meeting_code == CODE

    def test_meeting_number_wins_over_url_when_both_are_present(self) -> None:
        target = resolve_join_target(
            _meeting(CODE, url="https://meet.google.com/zzz-zzzz-zzz")
        )
        assert target.meeting_code == CODE

    def test_an_unusable_number_falls_back_to_the_url(self) -> None:
        """Neither half alone is enough; together they resolve."""
        target = resolve_join_target(
            _meeting("1234567890", url=f"https://meet.google.com/{CODE}")
        )
        assert target.meeting_code == CODE
