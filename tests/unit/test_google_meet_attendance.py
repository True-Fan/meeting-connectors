"""AttendanceLedger — the historical view over the roster stream.

Every test here drives the ledger through ``observe_roster``, which is exactly how the bridge
feeds it, so nothing is exercised through a back door the production path does not use.
"""

from __future__ import annotations

from src.connectors.google_meet.meeting.attendance import AttendanceLedger
from src.connectors.google_meet.meeting.participants import (
    MeetParticipant,
    MeetRoster,
    parse_roster,
)


def _roster(*people: tuple[str, str], self_name: str | None = "AI Avatar") -> MeetRoster:
    """A roster of ``(page_id, display_name)`` pairs, plus our own entry."""
    participants = [MeetParticipant(page_id=page_id, display_name=name) for page_id, name in people]
    if self_name:
        participants.append(MeetParticipant(page_id="self-1", display_name=self_name, is_self=True))
    return MeetRoster(participants=tuple(participants), self_name=self_name)


def _names(records: tuple[object, ...]) -> set[str]:
    return {r.label for r in records}  # type: ignore[attr-defined]


class TestPresence:
    def test_records_who_is_in_the_meeting(self) -> None:
        ledger = AttendanceLedger()
        ledger.observe_roster(_roster(("p1", "Aarav Sharma"), ("p2", "Priya Menon")))

        snapshot = ledger.snapshot()
        assert _names(snapshot.present) == {"Aarav Sharma", "Priya Menon"}
        assert snapshot.departed == ()
        assert snapshot.scans == 1

    def test_the_avatar_is_not_an_attendee(self) -> None:
        """The bot is in its own roster; counting it makes every answer wrong by one."""
        ledger = AttendanceLedger()
        ledger.observe_roster(_roster(("p1", "Aarav Sharma")))

        snapshot = ledger.snapshot()
        assert _names(snapshot.present) == {"Aarav Sharma"}
        assert snapshot.self_name == "AI Avatar"

    def test_a_departure_is_remembered_rather_than_forgotten(self) -> None:
        """The whole reason this exists: the roster drops them, the ledger keeps them."""
        ledger = AttendanceLedger()
        ledger.observe_roster(_roster(("p1", "Aarav Sharma"), ("p2", "Priya Menon")))
        ledger.observe_roster(_roster(("p1", "Aarav Sharma")))

        snapshot = ledger.snapshot()
        assert _names(snapshot.present) == {"Aarav Sharma"}
        assert _names(snapshot.departed) == {"Priya Menon"}
        assert _names(snapshot.attended) == {"Aarav Sharma", "Priya Menon"}

    def test_no_roster_yet_is_distinguishable_from_an_empty_meeting(self) -> None:
        """``scans == 0`` is "we do not know"; an empty roster is "nobody is here"."""
        unknown = AttendanceLedger().snapshot()
        assert unknown.scans == 0
        assert "not known yet" in unknown.agent_context()

        ledger = AttendanceLedger()
        ledger.observe_roster(_roster())
        empty = ledger.snapshot()
        assert empty.scans == 1
        assert "alone" in empty.agent_context()


class TestIdentityAcrossTime:
    def test_a_rejoin_is_one_person_not_two(self) -> None:
        """Meet mints a fresh participant id on rejoin, so an id-keyed ledger would double-count."""
        ledger = AttendanceLedger()
        ledger.observe_roster(_roster(("p1", "Priya Menon")))
        ledger.observe_roster(_roster())
        ledger.observe_roster(_roster(("p2-new-id", "Priya Menon")))

        snapshot = ledger.snapshot()
        assert len(snapshot.attended) == 1
        assert snapshot.attended[0].rejoins == 1
        assert snapshot.attended[0].present is True

    def test_the_same_person_on_two_selectors_is_one_entry(self) -> None:
        """A tile carries an id, a roster row may not — both must fold together."""
        ledger = AttendanceLedger()
        ledger.observe_roster(_roster(("p1", "Priya Menon"), ("", "Priya Menon")))

        assert len(ledger.snapshot().present) == 1

    def test_an_unnamed_participant_is_still_counted(self) -> None:
        ledger = AttendanceLedger()
        ledger.observe_roster(_roster(("tile-9", "")))

        present = ledger.snapshot().present
        assert len(present) == 1
        assert present[0].display_name is None
        assert "unnamed" in present[0].label

    def test_a_late_self_name_evicts_the_entry_it_created(self) -> None:
        """``selfName`` can arrive after the first scans; the avatar must not linger as a guest."""
        ledger = AttendanceLedger()
        anonymous = MeetRoster(
            participants=(MeetParticipant(page_id="s", display_name="AI Avatar"),),
            self_name=None,
        )
        ledger.observe_roster(anonymous)
        assert len(ledger.snapshot().attended) == 1

        ledger.observe_roster(_roster(("p1", "Aarav Sharma")))
        assert _names(ledger.snapshot().attended) == {"Aarav Sharma"}


class TestInvitees:
    def test_never_joined_is_the_difference_between_invited_and_present(self) -> None:
        ledger = AttendanceLedger(invitees=("Aarav Sharma", "Priya Menon", "Rahul Verma"))
        ledger.observe_roster(_roster(("p1", "Aarav Sharma")))

        snapshot = ledger.snapshot()
        assert _names(snapshot.present) == {"Aarav Sharma"}
        assert _names(snapshot.never_joined) == {"Priya Menon", "Rahul Verma"}
        assert snapshot.has_invite_list is True

    def test_seeding_is_idempotent_and_may_arrive_late(self) -> None:
        """The invite list is posted after the join, possibly minutes in."""
        ledger = AttendanceLedger()
        ledger.observe_roster(_roster(("p1", "Aarav Sharma")))

        assert ledger.seed_invitees(("Aarav Sharma", "Priya Menon")) == 2
        assert ledger.seed_invitees(("Aarav Sharma", "Priya Menon")) == 0

        snapshot = ledger.snapshot()
        assert len(snapshot.attended) == 1, "seeding must mark, not duplicate, a known attendee"
        assert _names(snapshot.never_joined) == {"Priya Menon"}

    def test_a_roster_name_replaces_a_calendar_address(self) -> None:
        ledger = AttendanceLedger(invitees=("priya@example.com",))
        assert _names(ledger.snapshot().never_joined) == {"priya@example.com"}

        # Meet reports the same person under the name it renders. Keyed on the folded name,
        # these are different people as far as the ledger can tell — which is the honest
        # outcome, and why the address is a placeholder rather than an identity.
        ledger.observe_roster(_roster(("p1", "Priya Menon")))
        assert _names(ledger.snapshot().present) == {"Priya Menon"}

    def test_without_an_invite_list_nobody_is_reported_missing(self) -> None:
        """Absent a list, "who did not come" is unknown — not "nobody"."""
        ledger = AttendanceLedger()
        ledger.observe_roster(_roster(("p1", "Aarav Sharma")))

        snapshot = ledger.snapshot()
        assert snapshot.has_invite_list is False
        assert snapshot.never_joined == ()
        assert "unknown" in snapshot.agent_context()


class TestAgentContext:
    def test_names_present_and_departed_and_missing(self) -> None:
        ledger = AttendanceLedger(invitees=("Aarav Sharma", "Priya Menon", "Rahul Verma"))
        ledger.observe_roster(_roster(("p1", "Aarav Sharma"), ("p2", "Priya Menon")))
        ledger.observe_roster(_roster(("p1", "Aarav Sharma")))

        context = ledger.snapshot().agent_context()
        assert "Aarav Sharma" in context
        assert "has left" in context and "Priya Menon" in context
        assert "never joined" in context and "Rahul Verma" in context

    def test_everyone_turning_up_is_stated_positively(self) -> None:
        ledger = AttendanceLedger(invitees=("Aarav Sharma",))
        ledger.observe_roster(_roster(("p1", "Aarav Sharma")))

        assert "Everyone on the invite list joined" in ledger.snapshot().agent_context()


class TestRobustness:
    def test_a_malformed_roster_costs_an_update_and_nothing_else(self) -> None:
        """``observe_roster`` runs on the media read loop, so it must never raise."""
        ledger = AttendanceLedger()
        ledger.observe_roster(_roster(("p1", "Aarav Sharma")))

        class Hostile:
            self_name = "AI Avatar"

            @property
            def others(self):  # type: ignore[no-untyped-def]
                raise RuntimeError("the DOM did something unexpected")

        ledger.observe_roster(Hostile())  # type: ignore[arg-type]

        assert _names(ledger.snapshot().present) == {"Aarav Sharma"}

    def test_survives_a_real_parsed_payload(self) -> None:
        """End-to-end through ``parse_roster``, the way the bridge actually feeds it."""
        ledger = AttendanceLedger()
        ledger.observe_roster(
            parse_roster(
                {
                    "selfName": "AI Avatar",
                    "participants": [
                        {"id": "p1", "name": "Aarav Sharma, presenting"},
                        {"id": "p2", "name": "Priya Menon"},
                        {"id": "s1", "name": "AI Avatar"},
                        {"junk": True},
                    ],
                }
            )
        )

        snapshot = ledger.snapshot()
        assert _names(snapshot.present) == {"Aarav Sharma", "Priya Menon"}, (
            "the status suffix must be stripped and our own entry excluded"
        )


class TestLiveMeetingDefects:
    """Regressions from a real meeting on 2026-08-13, kept as the labels the page actually sent.

    Three separate faults showed up in one run and each is asserted here with the literal string
    from the log, because paraphrasing a DOM-derived label is how a regression test stops
    guarding the thing that broke.
    """

    SELF_TILE = (
        "frame_person Reframe visual_effects Backgrounds and effects more_vert "
        "More options for jadumeetboot jadumeetboot jadumee"
    )

    def test_a_tile_full_of_toolbar_text_is_not_a_participant(self) -> None:
        """``innerText`` on a tile is the name plus every control rendered over it."""
        roster = parse_roster(
            {"selfName": "AI Avatar", "participants": [{"id": "p1", "name": self.SELF_TILE}]}
        )

        assert roster.participants == (), (
            "an icon-font token means the label is a container, not a person"
        )

    def test_a_doubled_name_is_one_person(self) -> None:
        roster = parse_roster(
            {
                "selfName": "AI Avatar",
                "participants": [{"id": "p1", "name": "dev Choudhary dev Choudhary"}],
            }
        )

        assert [p.display_name for p in roster.participants] == ["dev Choudhary"]

    def test_the_avatars_own_account_is_not_an_attendee(self) -> None:
        """``selfName`` is the *configured* name; a signed-in profile renders the account's."""
        ledger = AttendanceLedger(self_names=("AI Avatar", "jadumeetboot"))
        ledger.observe_roster(
            parse_roster(
                {
                    "selfName": "AI Avatar",
                    "participants": [
                        {"id": "p1", "name": "jadumeetboot jadumeetboot"},
                        {"id": "p2", "name": "dev Choudhary dev Choudhary"},
                    ],
                }
            )
        )

        snapshot = ledger.snapshot()
        assert {r.label for r in snapshot.present} == {"dev Choudhary"}, (
            "Meet reported one other person in this call; the bot must not count itself"
        )

    def test_the_you_marker_identifies_us_without_any_configuration(self) -> None:
        ledger = AttendanceLedger()
        ledger.observe_roster(
            parse_roster(
                {
                    "selfName": None,
                    "participants": [
                        {"id": "p1", "name": "jadumeetboot (You)"},
                        {"id": "p2", "name": "dev Choudhary"},
                    ],
                }
            )
        )

        assert {r.label for r in ledger.snapshot().present} == {"dev Choudhary"}

    def test_the_learned_self_name_survives_a_scan_without_the_marker(self) -> None:
        """The marker depends on which selector matched, so it is not on every scan."""
        ledger = AttendanceLedger()
        ledger.observe_roster(
            parse_roster(
                {"selfName": None, "participants": [{"id": "p1", "name": "jadumeetboot (You)"}]}
            )
        )
        ledger.observe_roster(
            parse_roster(
                {
                    "selfName": None,
                    "participants": [
                        {"id": "p1", "name": "jadumeetboot"},
                        {"id": "p2", "name": "dev Choudhary"},
                    ],
                }
            )
        )

        snapshot = ledger.snapshot()
        assert {r.label for r in snapshot.present} == {"dev Choudhary"}
        assert snapshot.departed == (), "the avatar must not appear as somebody who left"

    def test_the_page_reported_is_self_flag_is_honoured(self) -> None:
        roster = parse_roster(
            {
                "selfName": "AI Avatar",
                "participants": [{"id": "p1", "name": "jadumeetboot", "isSelf": True}],
            }
        )

        assert roster.others == ()

    def test_more_options_for_a_name_is_the_button_not_the_person(self) -> None:
        roster = parse_roster(
            {
                "selfName": "AI Avatar",
                "participants": [{"id": "p1", "name": "More options for Priya Menon"}],
            }
        )

        assert [p.display_name for p in roster.participants] == ["Priya Menon"]

    def test_only_the_first_line_of_a_tile_is_the_name(self) -> None:
        roster = parse_roster(
            {
                "selfName": "AI Avatar",
                "participants": [{"id": "p1", "name": "Priya Menon\nPresenting\nMute"}],
            }
        )

        assert [p.display_name for p in roster.participants] == ["Priya Menon"]

    def test_a_genuinely_repeated_word_is_left_alone(self) -> None:
        """Only an exact doubling of the whole sequence collapses."""
        roster = parse_roster(
            {"selfName": "AI Avatar", "participants": [{"id": "p1", "name": "Ann Ann Smith"}]}
        )

        assert [p.display_name for p in roster.participants] == ["Ann Ann Smith"]
