"""``TeamsMeetingObserver`` — where the page's *levels* become *edges*.

This is the file that guards the properties the page cannot guarantee for itself, because it
does not survive a reload and several frames run every observer independently:

* a roster is a list, and the ledger wants arrivals and departures;
* an empty list means "this frame cannot see the meeting", never "the meeting emptied";
* departures are debounced and arrivals are not;
* "whose hand is up" lives here, so an unmoved hand is one interruption rather than one per
  page re-render;
* a chat message reaches the transcript before the mention filter that drops most of them.

Every one of those was a live failure on a sibling connector before it was a rule.
"""

from __future__ import annotations

import asyncio

import pytest

from src.connectors.teams_web.meeting.active_speaker import TeamsSpeakerTracker
from src.connectors.teams_web.meeting.attendance import TeamsAttendanceLedger
from src.connectors.teams_web.meeting.chat import TeamsChatSource
from src.connectors.teams_web.meeting.hand_raise import TeamsInterruptSource
from src.connectors.teams_web.meeting.observer import TeamsMeetingObserver
from src.connectors.teams_web.meeting.transcript import TeamsTranscript
from src.services.media.clock import MediaClock


class _StepClock:
    """A media clock a test can advance, so grace windows are exercised without sleeping."""

    def __init__(self) -> None:
        self._now_us = 0

    def now_us(self) -> int:
        return self._now_us

    def advance_s(self, seconds: float) -> None:
        self._now_us += int(seconds * 1_000_000)


def _observer(
    *,
    clock: object | None = None,
    leave_grace_s: float = 8.0,
    self_names: tuple[str, ...] = ("AI Avatar",),
    require_mention: bool = True,
) -> tuple[TeamsMeetingObserver, dict[str, object]]:
    media_clock = clock or MediaClock()
    parts: dict[str, object] = {
        "attendance": TeamsAttendanceLedger(self_names=self_names),
        "speakers": TeamsSpeakerTracker(clock=media_clock, self_names=self_names),  # type: ignore[arg-type]
        "transcript": TeamsTranscript(self_names=self_names),
        "chat": TeamsChatSource(mention_names=self_names, require_mention=require_mention),
        "interrupts": TeamsInterruptSource(
            clock=media_clock,  # type: ignore[arg-type]
            self_names=self_names,
            cooldown_s=0.0,
        ),
    }
    observer = TeamsMeetingObserver(
        attendance=parts["attendance"],  # type: ignore[arg-type]
        speakers=parts["speakers"],  # type: ignore[arg-type]
        transcript=parts["transcript"],  # type: ignore[arg-type]
        chat=parts["chat"],  # type: ignore[arg-type]
        interrupts=parts["interrupts"],  # type: ignore[arg-type]
        clock=media_clock,  # type: ignore[arg-type]
        leave_grace_s=leave_grace_s,
    )
    return observer, parts


class TestRoster:
    def test_a_new_name_is_an_arrival(self) -> None:
        observer, parts = _observer()
        observer.on_page_event({"type": "roster", "names": ["Dev Choudhary"]})
        ledger: TeamsAttendanceLedger = parts["attendance"]  # type: ignore[assignment]
        assert ledger.present_names == ("Dev Choudhary",)

    def test_the_avatar_is_never_counted_as_an_attendee(self) -> None:
        """Every headcount would otherwise be wrong by one."""
        observer, parts = _observer()
        observer.on_page_event({"type": "roster", "names": ["AI Avatar", "Dev Choudhary"]})
        ledger: TeamsAttendanceLedger = parts["attendance"]  # type: ignore[assignment]
        assert ledger.present_names == ("Dev Choudhary",)

    def test_an_unchanged_roster_produces_no_further_events(self) -> None:
        observer, parts = _observer()
        for _ in range(5):
            observer.on_page_event({"type": "roster", "names": ["Dev Choudhary"]})
        ledger: TeamsAttendanceLedger = parts["attendance"]  # type: ignore[assignment]
        assert ledger.events == 1

    def test_an_empty_roster_is_ignored_rather_than_emptying_the_meeting(self) -> None:
        """``add_init_script`` runs in every frame Chromium creates and most of Teams' frames
        have no roster. The avatar is always in its own roster, so a genuinely empty one is not
        a state this can observe — and letting a re-render wipe the ledger is worse than not
        noticing that everybody left."""
        observer, parts = _observer()
        observer.on_page_event({"type": "roster", "names": ["Dev Choudhary"]})
        observer.on_page_event({"type": "roster", "names": []})
        ledger: TeamsAttendanceLedger = parts["attendance"]  # type: ignore[assignment]
        assert ledger.present_names == ("Dev Choudhary",)

    def test_a_departure_is_held_for_the_grace_window(self) -> None:
        """**The asymmetry is the fix.** The roster is read off a virtualised list and a tile
        grid that Teams re-lays out constantly, so believing every disappearance produces a
        ledger that flaps — and every flap re-pushes the meeting brief to the agent, telling
        the avatar the room emptied and refilled."""
        clock = _StepClock()
        observer, parts = _observer(clock=clock, leave_grace_s=8.0)
        ledger: TeamsAttendanceLedger = parts["attendance"]  # type: ignore[assignment]

        observer.on_page_event({"type": "roster", "names": ["Dev Choudhary", "Priya Menon"]})
        assert set(ledger.present_names) == {"Dev Choudhary", "Priya Menon"}

        # First disappearance: recorded as missing, not believed.
        observer.on_page_event({"type": "roster", "names": ["Dev Choudhary"]})
        assert "Priya Menon" in ledger.present_names

        # Reappears inside the window — a re-render, not a departure.
        observer.on_page_event({"type": "roster", "names": ["Dev Choudhary", "Priya Menon"]})
        clock.advance_s(20)
        observer.on_page_event({"type": "roster", "names": ["Dev Choudhary", "Priya Menon"]})
        assert "Priya Menon" in ledger.present_names

    def test_a_persistent_departure_is_eventually_believed(self) -> None:
        clock = _StepClock()
        observer, parts = _observer(clock=clock, leave_grace_s=8.0)
        ledger: TeamsAttendanceLedger = parts["attendance"]  # type: ignore[assignment]

        observer.on_page_event({"type": "roster", "names": ["Dev Choudhary", "Priya Menon"]})
        observer.on_page_event({"type": "roster", "names": ["Dev Choudhary"]})
        clock.advance_s(10)
        observer.on_page_event({"type": "roster", "names": ["Dev Choudhary"]})

        assert ledger.present_names == ("Dev Choudhary",)
        snapshot = ledger.snapshot()
        assert [r.label for r in snapshot.departed] == ["Priya Menon"]

    def test_an_arrival_is_believed_at_once(self) -> None:
        """There is no layout in which Teams invents a participant."""
        clock = _StepClock()
        observer, parts = _observer(clock=clock)
        observer.on_page_event({"type": "roster", "names": ["Dev Choudhary"]})
        observer.on_page_event({"type": "roster", "names": ["Dev Choudhary", "Priya Menon"]})
        ledger: TeamsAttendanceLedger = parts["attendance"]  # type: ignore[assignment]
        assert set(ledger.present_names) == {"Dev Choudhary", "Priya Menon"}

    def test_the_candidate_list_is_republished_to_everything_that_eliminates(self) -> None:
        """One account of who is present, so the three consumers of it cannot disagree."""
        observer, parts = _observer()
        observer.on_page_event({"type": "roster", "names": ["Dev Choudhary"]})
        speakers: TeamsSpeakerTracker = parts["speakers"]  # type: ignore[assignment]
        assert speakers.snapshot().candidates == ("Dev Choudhary",)


class TestRosterNames:
    """One person spelled several ways is several people, and that broke every answer.

    The ledger identifies participants by name, so a roster carrying "Dev Choudhary" and "Dev
    Choudhary muted Context menu is available" holds two attendees. Elimination — which is what
    names an unattributed voice, chat message or caption on this connector — needs *exactly*
    one other person present and fails closed at two, so the avatar could answer neither "what
    is my name" nor "who is in the meeting" in a two-person call.
    """

    def test_status_decorations_do_not_split_one_person_into_several(self) -> None:
        observer, parts = _observer()
        observer.on_page_event(
            {
                "type": "roster",
                "names": [
                    "Dev Choudhary muted Context menu is available",
                    "Dev Choudhary Context menu is available",
                    "Dev Choudhary",
                ],
            }
        )
        ledger: TeamsAttendanceLedger = parts["attendance"]  # type: ignore[assignment]
        assert ledger.present_names == ("Dev Choudhary",)

    def test_the_agent_brief_names_the_only_other_person(self) -> None:
        """The line that answers "what is my name": with one other participant there is nobody
        else the voice could belong to, so the brief makes the inference rather than leaving it
        to the agent."""
        observer, parts = _observer()
        observer.on_page_event(
            {"type": "roster", "names": ["Dev Choudhary muted Context menu is available"]}
        )
        ledger: TeamsAttendanceLedger = parts["attendance"]  # type: ignore[assignment]
        brief = ledger.snapshot().agent_context()
        assert "Dev Choudhary" in brief
        assert "Context menu" not in brief
        assert "is the only other person here" in brief

    def test_a_label_that_is_only_a_status_word_is_not_a_participant(self) -> None:
        """A name element that failed to resolve leaves the row's status pill as the whole
        label. Admitting it puts somebody in the meeting who does not exist."""
        observer, parts = _observer()
        observer.on_page_event({"type": "roster", "names": ["Dev Choudhary", "muted"]})
        ledger: TeamsAttendanceLedger = parts["attendance"]  # type: ignore[assignment]
        assert ledger.present_names == ("Dev Choudhary",)

    def test_a_chat_message_the_panel_could_not_attribute_is_named_by_elimination(
        self,
    ) -> None:
        """Asked who typed that, the avatar can answer — but only because the roster resolved
        to exactly one person."""
        observer, parts = _observer()
        observer.on_page_event(
            {"type": "roster", "names": ["Dev Choudhary muted Context menu is available"]}
        )
        observer.on_page_event(
            {"type": "chat", "name": None, "text": "@AI Avatar what is my name?"}
        )
        transcript: TeamsTranscript = parts["transcript"]  # type: ignore[assignment]
        assert "Dev Choudhary" in transcript.snapshot().agent_context()

    def test_a_speaker_is_named_as_the_roster_spells_them(self) -> None:
        """Asked who was talking to it, the avatar needs the speaker and the roster to be the
        same string — a speaker called "Dev Choudhary muted" is nobody the ledger has heard
        of."""
        observer, parts = _observer()
        observer.on_page_event({"type": "roster", "names": ["Dev Choudhary"]})
        observer.on_page_event(
            {"type": "speaker", "name": "Dev Choudhary muted Context menu is available"}
        )
        speakers: TeamsSpeakerTracker = parts["speakers"]  # type: ignore[assignment]
        assert speakers.current_speaker() == "Dev Choudhary"


class TestHands:
    def test_a_raised_hand_becomes_an_interruption(self) -> None:
        observer, parts = _observer()
        observer.on_page_event(
            {"type": "handRaise", "id": "name:priya menon", "name": "Priya Menon"}
        )
        interrupts: TeamsInterruptSource = parts["interrupts"]  # type: ignore[assignment]
        assert interrupts.received == 1
        assert interrupts.hands == 1

    def test_an_unmoved_hand_is_reported_once_however_often_the_page_re_detects_it(
        self,
    ) -> None:
        """**The failure this exists to prevent.** The page's set is keyed on a name read out of
        a re-rendered row, so it retires a hand that has not moved and re-detects it as new. The
        avatar then stops itself to say "ok, go ahead" again, repeatedly, for as long as somebody
        leaves their hand up.

        Deliberately *not* the per-participant cooldown, which would still fire again every
        cooldown — the same behaviour, only slower."""
        observer, parts = _observer()
        for _ in range(5):
            observer.on_page_event(
                {"type": "handRaise", "id": "name:priya menon", "name": "Priya Menon"}
            )
        interrupts: TeamsInterruptSource = parts["interrupts"]  # type: ignore[assignment]
        assert interrupts.received == 1

    @pytest.mark.asyncio
    async def test_a_hand_lowered_and_raised_again_is_two_interruptions(self) -> None:
        """The lower event is what re-arms the edge.

        The first interruption is drained the way the router drains it, because the queue is
        deliberately one deep: an undelivered request for the floor is the one that still needs
        to happen, so a second arriving on top of it is dropped rather than replacing it.
        """
        observer, parts = _observer()
        interrupts: TeamsInterruptSource = parts["interrupts"]  # type: ignore[assignment]

        observer.on_page_event(
            {"type": "handRaise", "id": "name:priya menon", "name": "Priya Menon"}
        )
        await asyncio.wait_for(anext(interrupts.events()), timeout=1)

        observer.on_page_event({"type": "handLower", "id": "name:priya menon"})
        observer.on_page_event(
            {"type": "handRaise", "id": "name:priya menon", "name": "Priya Menon"}
        )
        assert interrupts.received == 2

    def test_an_unmoved_hand_whose_label_changed_is_still_one_interruption(self) -> None:
        """**The live failure, reproduced exactly.** Teams writes a participant's *state* into
        the same accessible name as their name, so the page's key changes the moment they mute:

            name:dev choudhary muted context menu is available
            name:dev choudhary context menu is available

        The page then retires the first — its row stopped being seen — and reports the second as
        a fresh raise. Nobody moved, and the avatar stopped itself to say "ok, go ahead" a second
        time five seconds after the first.

        Two defences, and this asserts the outcome of both: ``meeting/names.py`` scrubs the
        decorations so the two keys describe one person, and the observer latches on that person
        rather than only on the page's key.
        """
        observer, parts = _observer()
        observer.on_page_event(
            {
                "type": "handRaise",
                "id": "name:dev choudhary muted context menu is available",
                "name": "Dev Choudhary muted Context menu is available",
            }
        )
        # The label lost "muted" — they unmuted to ask their question — so the page retires the
        # old key and offers the new one.
        observer.on_page_event(
            {"type": "handLower", "id": "name:dev choudhary muted context menu is available"}
        )
        observer.on_page_event(
            {
                "type": "handRaise",
                "id": "name:dev choudhary context menu is available",
                "name": "Dev Choudhary Context menu is available",
            }
        )
        interrupts: TeamsInterruptSource = parts["interrupts"]  # type: ignore[assignment]
        assert interrupts.received == 1

    def test_the_agent_is_told_the_person_s_name_and_not_the_row_s_label(self) -> None:
        """The name is *spoken*: it reaches the agent as "{name} wants to say something", and a
        live meeting had the avatar told that "Dev Choudhary muted Context menu is available"
        wanted the floor."""
        observer, parts = _observer()
        observer.on_page_event(
            {
                "type": "handRaise",
                "id": "name:dev choudhary muted context menu is available",
                "name": "Dev Choudhary muted Context menu is available",
            }
        )
        interrupts: TeamsInterruptSource = parts["interrupts"]  # type: ignore[assignment]
        event = interrupts._queue.get_nowait()
        assert event.participant == "Dev Choudhary"
        assert "Dev Choudhary wants to say something" in event.prompt

    def test_a_hand_lowered_under_every_label_re_arms_the_edge(self) -> None:
        """The person's latch is released once nothing still reports them, and not before: a
        hand genuinely lowered and raised again is two requests for the floor."""
        observer, parts = _observer()
        interrupts: TeamsInterruptSource = parts["interrupts"]  # type: ignore[assignment]
        observer.on_page_event(
            {
                "type": "handRaise",
                "id": "name:dev choudhary muted",
                "name": "Dev Choudhary muted",
            }
        )
        interrupts._queue.get_nowait()  # drained the way the router drains it
        observer.on_page_event({"type": "handLower", "id": "name:dev choudhary muted"})
        observer.on_page_event(
            {"type": "handRaise", "id": "name:dev choudhary", "name": "Dev Choudhary"}
        )
        assert interrupts.received == 2

    def test_an_unattributed_hand_is_named_by_elimination(self) -> None:
        """Teams draws the indicator on the participant's *tile*, and a tile showing video
        carries an image where a camera-off tile carries a name — so the person whose hand is up
        can be exactly the person whose name is not written down."""
        observer, parts = _observer()
        observer.on_page_event({"type": "roster", "names": ["Dev Choudhary"]})
        observer.on_page_event({"type": "handRaise", "id": "anonymous", "name": None})
        interrupts: TeamsInterruptSource = parts["interrupts"]  # type: ignore[assignment]
        assert interrupts.received == 1

    def test_elimination_fails_closed_with_two_others_present(self) -> None:
        """A confidently wrong "Priya raised their hand" is worse than "Someone": the agent
        would greet the wrong person by name."""
        observer, parts = _observer()
        observer.on_page_event({"type": "roster", "names": ["Dev Choudhary", "Priya Menon"]})
        event: dict[str, object] = {"type": "handRaise", "id": "anonymous", "name": None}
        observer.on_page_event(event)
        interrupts: TeamsInterruptSource = parts["interrupts"]  # type: ignore[assignment]
        # Still delivered — an anonymous interruption is better than a missed one — but not
        # attributed to a guess.
        assert interrupts.received == 1

    def test_the_avatar_s_own_hand_is_never_an_interruption(self) -> None:
        observer, parts = _observer()
        observer.on_page_event(
            {"type": "handRaise", "id": "name:ai avatar", "name": "AI Avatar"}
        )
        interrupts: TeamsInterruptSource = parts["interrupts"]  # type: ignore[assignment]
        assert interrupts.received == 0
        assert interrupts.ignored == 1


class TestSpeaker:
    def test_the_tracker_and_the_interrupt_source_both_see_the_floor_move(self) -> None:
        observer, parts = _observer()
        observer.on_page_event({"type": "speaker", "name": "Priya Menon"})
        speakers: TeamsSpeakerTracker = parts["speakers"]  # type: ignore[assignment]
        assert speakers.current_speaker() == "Priya Menon"

    def test_the_page_s_own_self_check_does_not_short_circuit_python_s(self) -> None:
        """The page knows one name; ``_self_name_candidates`` knows two. Letting the page's
        narrower answer decide is how the avatar ends up interrupting itself."""
        observer, parts = _observer(self_names=("AI Avatar", "TrueFan Avatar"))
        observer.on_page_event(
            {"type": "speaker", "name": "TrueFan Avatar", "isSelf": False}
        )
        speakers: TeamsSpeakerTracker = parts["speakers"]  # type: ignore[assignment]
        assert speakers.current_speaker() is None
        assert speakers.ignored == 1

    def test_a_voice_is_not_an_interruption_when_the_avatar_is_silent(self) -> None:
        """Somebody starting to talk into a silence is just the meeting happening, and their
        audio is already on its way to the agent."""
        clock = MediaClock()
        interrupts = TeamsInterruptSource(
            clock=clock, self_names=("AI Avatar",), is_avatar_speaking=lambda: False
        )
        observer = TeamsMeetingObserver(interrupts=interrupts, clock=clock)
        observer.on_page_event({"type": "speaker", "name": "Priya Menon"})
        assert interrupts.received == 0
        assert interrupts.ignored == 1


class TestChatAndCaptions:
    def test_the_transcript_records_every_message_and_the_filter_runs_after(self) -> None:
        """**The ordering is the fix.** The chat source drops everything not addressed to the
        avatar, which is the right policy for deciding what to *answer* and the wrong one for
        deciding what to *remember*: a meeting held largely in chat would leave the avatar
        describing only the half aimed at it."""
        observer, parts = _observer()
        observer.on_page_event(
            {"type": "chat", "name": "Dev Choudhary", "text": "morning everyone"}
        )
        transcript: TeamsTranscript = parts["transcript"]  # type: ignore[assignment]
        chat: TeamsChatSource = parts["chat"]  # type: ignore[assignment]
        assert transcript.count == 1
        assert chat.received == 0

    def test_a_tagged_message_reaches_the_chat_source_with_the_mention_stripped(self) -> None:
        observer, parts = _observer()
        observer.on_page_event(
            {
                "type": "chat",
                "name": "Dev Choudhary",
                "text": "@AI Avatar what is the notice period?",
            }
        )
        chat: TeamsChatSource = parts["chat"]  # type: ignore[assignment]
        assert chat.received == 1

    def test_a_typed_line_is_marked_as_typed_rather_than_spoken(self) -> None:
        """An agent must not report that it heard somebody who never unmuted."""
        observer, parts = _observer()
        observer.on_page_event(
            {"type": "chat", "name": "Dev Choudhary", "text": "morning everyone"}
        )
        transcript: TeamsTranscript = parts["transcript"]  # type: ignore[assignment]
        assert transcript.snapshot().lines[0].in_chat is True
        assert transcript.snapshot().chat_lines == 1

    def test_only_settled_captions_reach_the_transcript(self) -> None:
        """Interim lines are refused here as well as in the page: an agent handed half a
        sentence answers half a question."""
        observer, parts = _observer()
        observer.on_page_event(
            {"type": "caption", "name": "Dev", "text": "I want to know", "final": False}
        )
        transcript: TeamsTranscript = parts["transcript"]  # type: ignore[assignment]
        assert transcript.count == 0

        observer.on_page_event(
            {
                "type": "caption",
                "name": "Dev",
                "text": "I want to know about Delhi",
                "final": True,
            }
        )
        assert transcript.count == 1
        assert transcript.snapshot().lines[0].in_chat is False


class TestRobustness:
    def test_a_malformed_event_is_dropped_rather_than_raised(self) -> None:
        """This runs on the loop that carries the avatar's voice into the page: a bad payload
        must cost the observation and nothing else."""
        observer, _ = _observer()
        for event in (
            {},
            {"type": "roster", "names": "not-a-list"},
            {"type": "speaker"},
            {"type": "caption", "final": True},
            {"type": "chat", "text": ""},
            {"type": "unknown-kind"},
        ):
            observer.on_page_event(event)  # type: ignore[arg-type]

    def test_every_consumer_is_optional(self) -> None:
        """An absent collaborator means the feature is switched off, never a fault."""
        observer = TeamsMeetingObserver()
        observer.on_page_event({"type": "roster", "names": ["Dev"]})
        observer.on_page_event({"type": "speaker", "name": "Dev"})
        observer.on_page_event({"type": "chat", "name": "Dev", "text": "hi"})
        observer.on_page_event({"type": "caption", "name": "Dev", "text": "hi", "final": True})
        observer.on_page_event({"type": "handRaise", "id": "x", "name": "Dev"})
