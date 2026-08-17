"""Knowing who is speaking, and keeping that attribution for the whole meeting.

**The property that matters most is the one about what did *not* change.** Ingest on this
connector is a mix — every remote track is summed before the worklet samples it — and that is
what makes it cheap, resampler-free, and identical for every participant. Attribution is
assembled beside that path and must stay beside it, so this file asserts the negative as
carefully as the positive: no message type on the audio frame, no source id, no stage inserted
into the capture graph, and no DOM work added to the sampler that decides *when* somebody is
talking.

The rest is the behaviour the two features that came before this one already paid for:

* identity arrives late, so a turn must be **renamed** rather than split;
* two independent observers of one person must not become two speakers;
* a pause is not the end of a turn, or the history reads like a waveform instead of like a
  conversation.
"""

from __future__ import annotations

import asyncio
import re

import pytest

from src.connectors.google_meet.automation.selectors import DEFAULT_SELECTORS
from src.connectors.google_meet.js import BRIDGE_ASSET, read_asset
from src.connectors.google_meet.meeting.active_speaker import (
    ANONYMOUS,
    SOURCE_AUDIO,
    SOURCE_DOM,
    SpeakerTracker,
)
from src.connectors.google_meet.meeting.participants import MeetParticipant, MeetRoster
from src.connectors.google_meet.meeting.speaker_announcer import SpeakerAnnouncer, signature
from src.connectors.google_meet.websocket.protocol import MeetMessageType
from src.services.media.clock import MediaClock


class StepClock(MediaClock):
    """A media clock that only moves when a test says so.

    Turn boundaries, hold windows and merge gaps are all *durations*, and asserting on them
    against a real clock means either sleeping or accepting flakiness. Every property here is
    about elapsed time, so the time is made an input.
    """

    __slots__ = ("_now_us",)

    def __init__(self) -> None:
        super().__init__(origin_ns=0)
        self._now_us = 0

    def now_us(self) -> int:
        return self._now_us

    def advance(self, ms: float) -> None:
        self._now_us += int(ms * 1_000)


@pytest.fixture(scope="module")
def bridge_code() -> str:
    """``bridge.js`` with its comments stripped.

    The prose explains what the code must *not* do — "never a stage", "touches no DOM" — and a
    naive search over it reads those explanations as violations. Same fixture, same reason, as
    ``test_google_meet_js_assets.py``'s.
    """
    source = read_asset(BRIDGE_ASSET)
    without_blocks = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    return re.sub(r"(?<!:)//[^\n]*", "", without_blocks)


@pytest.fixture
def clock() -> StepClock:
    return StepClock()


@pytest.fixture
def meet_config(tmp_path):
    """A configured connector, with the tiny geometry the fake page expects."""
    from src.config.settings import GoogleMeetSettings, Settings
    from src.connectors.google_meet.config import GoogleMeetConnectorConfig

    template = tmp_path / "profile"
    (template / "Default").mkdir(parents=True)
    (template / "Default" / "Cookies").write_bytes(b"cookie")
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        google_meet=GoogleMeetSettings(
            profile_dir=template,
            video_width=320,
            video_height=180,
            publish_sample_rate_hz=48_000,
            bridge_ready_timeout_s=5.0,
            join_timeout_s=2.0,
            lobby_timeout_s=2.0,
        ),
    )
    return GoogleMeetConnectorConfig.from_settings(settings)


@pytest.fixture
def meeting():
    from src.domain.meeting import MeetingContext, MeetingPlatform

    return MeetingContext(
        meeting_number="abc-defg-hij",
        display_name="AI Avatar",
        platform=MeetingPlatform.GOOGLE_MEET,
    )


@pytest.fixture
def tracker(clock: StepClock) -> SpeakerTracker:
    return SpeakerTracker(clock=clock, hold_ms=1_500, merge_gap_ms=1_200, self_names=("AI Avatar",))


def _edge(
    *,
    track: str = "t1",
    name: str | None = "Priya Menon",
    page_id: str = "",
    speaking: bool = True,
    source: str = SOURCE_AUDIO,
) -> dict[str, object]:
    return {
        "trackId": track,
        "id": page_id,
        "name": name,
        "speaking": speaking,
        "source": source,
        "level": 0.08,
        "heldMs": 0,
    }


class TestCurrentSpeaker:
    """"Who is talking right now" — the first of the two questions."""

    def test_a_start_edge_makes_somebody_the_current_speaker(self, tracker) -> None:
        tracker.offer(_edge())

        assert tracker.current_speaker() == "Priya Menon"
        assert tracker.snapshot().is_anyone_speaking

    def test_nobody_is_speaking_before_anything_is_heard(self, tracker) -> None:
        snapshot = tracker.snapshot()

        assert snapshot.current_speaker is None
        # Zero events is "not known", not "nobody spoke" — the distinction the brief has to
        # state rather than paper over.
        assert snapshot.events == 0
        assert "has been heard" in snapshot.agent_context()

    def test_the_answer_survives_the_gap_between_two_sentences(self, tracker, clock) -> None:
        """**The hold window, and why it is not a fudge.**

        The page's release is short on purpose so a turn *ends* promptly, which means a stop
        edge arrives in every clause-length pause. Without a hold, "who is speaking" answers
        "nobody" for a fraction of a second several times per turn — and a barge-in landing in
        one of those gaps would be attributed to no one, which is exactly the case this feature
        exists to fix.
        """
        tracker.offer(_edge())
        clock.advance(2_000)
        tracker.offer(_edge(speaking=False))

        clock.advance(500)
        assert tracker.current_speaker() == "Priya Menon"

        clock.advance(2_000)  # past the 1.5 s hold
        assert tracker.current_speaker() is None

    def test_two_people_talking_at_once_are_both_reported(self, tracker) -> None:
        """Reporting one of them would be a guess presented as a fact."""
        tracker.offer(_edge(track="t1", name="Priya Menon"))
        tracker.offer(_edge(track="t2", name="Aarav Sharma"))

        assert set(tracker.snapshot().current) == {"Priya Menon", "Aarav Sharma"}

    def test_the_most_recent_speaker_is_the_single_answer(self, tracker, clock) -> None:
        tracker.offer(_edge(track="t1", name="Priya Menon"))
        clock.advance(1_000)
        tracker.offer(_edge(track="t2", name="Aarav Sharma"))

        assert tracker.current_speaker() == "Aarav Sharma"

    def test_a_lookup_is_cheap_enough_for_the_inbound_leg(self, tracker) -> None:
        """It is called on the frame that triggers a barge-in, so it may not do work.

        Not a benchmark — a guard against this becoming a snapshot builder later, which would
        allocate a tuple of every turn in the meeting on an audio frame.
        """
        for index in range(50):
            tracker.offer(_edge(track=f"t{index}", name=f"Person {index}"))

        assert tracker.current_speaker() == "Person 49"


class TestTurnHistory:
    """"Who has spoken, when, and for how long" — the second question."""

    def test_a_turn_records_its_length(self, tracker, clock) -> None:
        tracker.offer(_edge())
        clock.advance(4_000)
        tracker.offer(_edge(speaking=False))

        (turn,) = tracker.snapshot().turns
        assert turn.display_name == "Priya Menon"
        assert turn.duration_us() == 4_000_000
        assert not turn.is_open

    def test_an_open_turn_is_measured_to_now(self, tracker, clock) -> None:
        tracker.offer(_edge())
        clock.advance(3_000)

        (turn,) = tracker.snapshot().turns
        assert turn.is_open
        assert turn.duration_us(now_us=clock.now_us()) == 3_000_000

    def test_a_pause_punctuates_a_turn_rather_than_ending_it(self, tracker, clock) -> None:
        """Without this, one person talking for a minute is forty turns.

        The page's release window is 600 ms, so a stop edge arrives at every clause boundary.
        Recording each stretch separately would make "who has been speaking" answer with the
        same name forty times, and every talk-time figure a sum of fragments.
        """
        tracker.offer(_edge())
        clock.advance(2_000)
        tracker.offer(_edge(speaking=False))
        clock.advance(800)  # inside the 1.2 s merge gap
        tracker.offer(_edge())
        clock.advance(2_000)
        tracker.offer(_edge(speaking=False))

        turns = tracker.snapshot().turns
        assert len(turns) == 1
        assert turns[0].duration_us() == 4_800_000

    def test_a_real_gap_starts_a_new_turn(self, tracker, clock) -> None:
        """Even for the same person: they took the floor twice, and that is what happened."""
        tracker.offer(_edge())
        clock.advance(1_000)
        tracker.offer(_edge(speaking=False))
        clock.advance(5_000)  # well past the merge gap
        tracker.offer(_edge())

        assert len(tracker.snapshot().turns) == 2

    def test_talk_time_sums_across_turns(self, tracker, clock) -> None:
        tracker.offer(_edge(track="t1", name="Priya Menon"))
        clock.advance(3_000)
        tracker.offer(_edge(track="t1", name="Priya Menon", speaking=False))
        clock.advance(5_000)
        tracker.offer(_edge(track="t2", name="Aarav Sharma"))
        clock.advance(1_000)
        tracker.offer(_edge(track="t2", name="Aarav Sharma", speaking=False))
        clock.advance(5_000)
        tracker.offer(_edge(track="t1", name="Priya Menon"))
        clock.advance(2_000)
        tracker.offer(_edge(track="t1", name="Priya Menon", speaking=False))

        assert tracker.snapshot().talk_time() == (("Priya Menon", 5), ("Aarav Sharma", 1))

    def test_the_history_is_bounded(self, clock) -> None:
        """A pathological page must not grow this without limit for a long meeting."""
        tracker = SpeakerTracker(clock=clock, merge_gap_ms=0)
        for index in range(2_100):
            tracker.offer(_edge(track=f"t{index}", name=f"Person {index}"))
            tracker.offer(_edge(track=f"t{index}", name=f"Person {index}", speaking=False))
            clock.advance(10)

        turns = tracker.snapshot().turns
        assert len(turns) <= 2_000
        # Oldest dropped, newest kept — the opposite of the attendance ledger, and right for
        # the opposite reason: this answers "who is talking and who just talked".
        assert turns[-1].display_name == "Person 2099"


class TestLateIdentity:
    """Somebody can start talking before Meet has drawn their tile."""

    def test_an_unattributed_turn_is_renamed_rather_than_split(self, tracker, clock) -> None:
        """**The bug this prevents is the first sentence of every meeting.**

        The page keys an edge on the track, because that is what it knows first. If a later
        edge carrying the name were treated as a new turn, the opening sentence would be
        attributed to nobody and a second turn would appear for a person who never stopped
        talking — two wrong answers from one late label.
        """
        tracker.offer(_edge(name=None))
        assert tracker.current_speaker() == ANONYMOUS

        clock.advance(400)
        tracker.offer(_edge(name="Priya Menon", page_id="p1"))

        turns = tracker.snapshot().turns
        assert len(turns) == 1
        assert turns[0].display_name == "Priya Menon"
        assert turns[0].page_id == "p1"
        assert tracker.current_speaker() == "Priya Menon"

    def test_a_name_is_never_replaced_by_a_missing_one(self, tracker) -> None:
        tracker.offer(_edge(name="Priya Menon"))
        tracker.offer(_edge(name=None))

        assert tracker.snapshot().turns[0].display_name == "Priya Menon"

    def test_the_roster_names_a_turn_the_page_could_only_give_an_id_for(
        self, tracker, clock
    ) -> None:
        """A tile carries ``data-participant-id`` reliably and a label only sometimes.

        So the page reports the id and the name is resolved from the roster stream the
        connector already receives — no extra DOM work, and therefore no cost to the media
        path. Retroactive, because somebody who spoke in the first seconds would otherwise stay
        unidentified in the history for the whole meeting.
        """
        tracker.offer(_edge(name=None, page_id="p7"))
        clock.advance(1_000)
        tracker.offer(_edge(name=None, page_id="p7", speaking=False))

        tracker.observe_roster(
            MeetRoster(participants=(MeetParticipant(page_id="p7", display_name="Priya Menon"),))
        )

        assert tracker.snapshot().turns[0].display_name == "Priya Menon"


class TestNamesFromALossyDom:
    """Speaker names come off the same tiles roster names do, so they carry the same hazards."""

    def test_a_status_suffix_is_not_part_of_the_name(self, tracker) -> None:
        """Otherwise one person becomes two speakers the moment they start presenting."""
        tracker.offer(_edge(name="Priya Menon, presenting"))

        assert tracker.current_speaker() == "Priya Menon"

    def test_a_container_label_is_rejected_rather_than_salvaged(self, tracker) -> None:
        """Icon-font glyphs in a label mean it is a tile's whole text, not a person.

        A speaker called "frame_person Reframe visual_effects" is a wrong answer delivered
        confidently; an unattributed turn is a gap. The roster's cleaner is reused here rather
        than reimplemented, so the lesson it learned in a live meeting applies to speakers too.
        """
        tracker.offer(_edge(name="frame_person Reframe visual_effects", page_id="p3"))

        assert tracker.current_speaker() != "frame_person Reframe visual_effects"
        assert tracker.snapshot().turns[0].display_name is None
        # The id survives, so the roster can still name this turn retroactively.
        assert tracker.snapshot().turns[0].page_id == "p3"


class TestAttributionByElimination:
    """Naming a speaker in a two-person call without reading any markup at all.

    **This is the path the first live run proved was load-bearing.** Three audio probes measured
    speech correctly, one stream mapped to a tile, and every turn came out "Someone" — because
    Meet does not render remote *audio* on an element inside the participant tile, so an audio
    stream's id never appears there and never can. An interview is two people; if exactly one other
    person is in the room, whoever is speaking is that person. No DOM, nothing to break.
    """

    def test_the_only_other_participant_is_the_speaker(self, tracker) -> None:
        tracker.observe_roster(
            MeetRoster(
                participants=(
                    MeetParticipant(page_id="p1", display_name="dev Choudhary"),
                    MeetParticipant(page_id="self", display_name="Backend Services", is_self=True),
                ),
                self_name="Backend Services",
            )
        )

        tracker.offer(_edge(name=None))

        assert tracker.current_speaker() == "dev Choudhary"
        assert tracker.snapshot().turns[0].inferred is True

    def test_two_others_are_not_guessed_between(self, tracker) -> None:
        """A confident wrong name is worse than "Someone" — it is the one output that makes the
        agent state something false about a person."""
        tracker.observe_roster(
            MeetRoster(
                participants=(
                    MeetParticipant(page_id="p1", display_name="dev Choudhary"),
                    MeetParticipant(page_id="p2", display_name="Priya Menon"),
                ),
                self_name="Backend Services",
            )
        )

        tracker.offer(_edge(name=None))

        assert tracker.current_speaker() == ANONYMOUS

    def test_an_empty_meeting_infers_nothing(self, tracker) -> None:
        tracker.observe_roster(MeetRoster(self_name="Backend Services"))
        tracker.offer(_edge(name=None))

        assert tracker.current_speaker() == ANONYMOUS

    def test_a_turn_heard_before_the_roster_is_named_once_it_arrives(
        self, tracker, clock
    ) -> None:
        """The first sentence of a meeting is routinely heard before the roster is read."""
        tracker.offer(_edge(name=None))
        clock.advance(1_000)

        tracker.observe_roster(
            MeetRoster(
                participants=(
                    MeetParticipant(page_id="p1", display_name="dev Choudhary"),
                    MeetParticipant(page_id="self", display_name="Backend Services", is_self=True),
                ),
                self_name="Backend Services",
            )
        )

        assert tracker.snapshot().turns[0].display_name == "dev Choudhary"
        assert tracker.current_speaker() == "dev Choudhary"

    def test_meets_placeholder_audio_tracks_do_not_become_three_speakers(
        self, tracker, clock
    ) -> None:
        """Meet opens several audio receivers in a two-person call, and the live run showed three.

        Inferred to the same person, they must collapse into one turn — otherwise talk time is
        counted three times over and "who is speaking" lists one participant repeatedly.
        """
        tracker.observe_roster(
            MeetRoster(
                participants=(
                    MeetParticipant(page_id="p1", display_name="dev Choudhary"),
                    MeetParticipant(page_id="self", display_name="Backend Services", is_self=True),
                ),
                self_name="Backend Services",
            )
        )

        tracker.offer(_edge(track="t1", name=None))
        tracker.offer(_edge(track="t2", name=None))
        tracker.offer(_edge(track="t3", name=None))
        clock.advance(2_000)

        snapshot = tracker.snapshot()
        assert snapshot.current == ("dev Choudhary",)
        assert len(snapshot.turns) == 1
        assert snapshot.talk_time() == (("dev Choudhary", 2),)

    def test_it_never_credits_speech_to_the_avatars_own_name(self, clock) -> None:
        """**Observed live, and worse than the failure it replaced.** Self-detection missed the
        avatar's own tile, leaving the avatar as the only "other" in the room — and elimination
        then credited a participant's speech to "Backend Services", the avatar's own account.

        The avatar's audio cannot reach the tap at all (the WebRTC hook is inbound-only), so any
        speech arriving is by definition not ours. Failing closed here means the two faults cannot
        compound into a confident wrong name, which is the one output worse than "Someone".
        """
        tracker = SpeakerTracker(clock=clock, self_names=("Backend Services",))
        tracker.observe_roster(
            MeetRoster(
                # Self-detection has failed: our own tile is reported as an ordinary participant
                # and is the only one there.
                participants=(MeetParticipant(page_id="p1", display_name="Backend Services"),),
                self_name=None,
            )
        )

        tracker.offer(_edge(name=None))

        assert tracker.current_speaker() == ANONYMOUS

    def test_the_avatars_own_account_name_is_excluded(self, clock) -> None:
        """The roster's ``others`` is only trustworthy because ``parse_roster`` now reads the
        account's rendered name — see ``TestSelfNameFromTheAccount``."""
        tracker = SpeakerTracker(clock=clock, self_names=("AI Avatar",))
        tracker.observe_roster(
            MeetRoster(
                participants=(
                    MeetParticipant(page_id="p1", display_name="dev Choudhary"),
                    MeetParticipant(page_id="self", display_name="Backend Services"),
                ),
                self_name="Backend Services",
            )
        )

        tracker.offer(_edge(name=None))

        # "Backend Services" is us, learned from the roster's self name, so there is exactly one
        # other person and the inference is available.
        assert tracker.current_speaker() == "dev Choudhary"


class TestEliminationNarrowedByMuteState:
    """Two people in the room, one of them muted — which is one person it can be.

    **The case that spent four minutes answering with the wrong name.** Elimination is this
    connector's most reliable attribution route: it reads no markup and cannot be broken by a Meet
    release. It also gave up entirely at two other participants, because naming one of two would be
    a guess — and a live meeting had exactly two: one identity typing in the chat with its
    microphone off, another speaking. Captions were the only naming route left, and they name a
    voice only in the caption language, so two Urdu questions were answered "your name is <the
    person who was typing>" and the English one that followed was answered correctly.

    Somebody Meet says is muted is not the voice being heard. That is not an inference about who is
    talking; it is a fact about who is not.
    """

    def _roster(self, *entries: tuple[str, bool | None]) -> MeetRoster:
        return MeetRoster(
            participants=(
                *(
                    MeetParticipant(page_id=f"p{index}", display_name=name, muted=muted)
                    for index, (name, muted) in enumerate(entries)
                ),
                MeetParticipant(page_id="s", display_name="AI Avatar", is_self=True),
            ),
            self_name="AI Avatar",
        )

    def test_a_muted_participant_is_not_a_candidate(self, tracker) -> None:
        tracker.observe_roster(
            self._roster(("Backend Services", True), ("dev Choudhary", False))
        )

        tracker.offer(_edge(name=None))

        assert tracker.current_speaker() == "dev Choudhary"

    def test_an_unreadable_mute_state_keeps_everybody_in_the_running(self, tracker) -> None:
        """``None`` means Meet's label said nothing about audio, which must widen the field rather
        than narrow it — otherwise a Meet redesign that drops the suffix turns this from a
        narrowing into a confident wrong answer."""
        tracker.observe_roster(
            self._roster(("Backend Services", None), ("dev Choudhary", None))
        )

        tracker.offer(_edge(name=None))

        assert tracker.current_speaker() == ANONYMOUS

    def test_two_unmuted_people_are_still_two_candidates(self, tracker) -> None:
        tracker.observe_roster(
            self._roster(("Backend Services", False), ("dev Choudhary", False))
        )

        tracker.offer(_edge(name=None))

        assert tracker.current_speaker() == ANONYMOUS

    def test_everybody_muted_attributes_nobody(self, tracker) -> None:
        """A voice is being heard, so somebody's mute state is stale or wrong. Believing the label
        over the audio would credit speech to nobody at all — which is what ANONYMOUS says."""
        tracker.observe_roster(self._roster(("Backend Services", True), ("dev Choudhary", True)))

        tracker.offer(_edge(name=None))

        assert tracker.current_speaker() == ANONYMOUS

    def test_it_reads_mute_state_off_the_label_meet_already_sends(self) -> None:
        """No new page reading is required for the common case: ``", muted"`` has been in
        ``_STATUS_SUFFIXES`` since that list was written — it was stripped off the name and thrown
        away."""
        from src.connectors.google_meet.meeting.participants import parse_roster

        roster = parse_roster(
            {
                "participants": [
                    {"id": "p1", "name": "Backend Services, muted"},
                    {"id": "p2", "name": "dev Choudhary"},
                ],
                "selfName": "AI Avatar",
            }
        )

        by_name = {p.display_name: p for p in roster.participants}
        assert by_name["Backend Services"].muted is True
        assert by_name["dev Choudhary"].muted is None
        assert [p.display_name for p in roster.could_be_speaking] == ["dev Choudhary"]

    def test_the_page_may_report_it_directly_and_wins_when_it_does(self) -> None:
        """A tile attribute is stronger than prose, and a page that reports neither still works."""
        from src.connectors.google_meet.meeting.participants import parse_roster

        roster = parse_roster(
            {"participants": [{"id": "p1", "name": "dev Choudhary, muted", "muted": False}]}
        )

        assert roster.participants[0].muted is False


class TestSelfNameFromTheAccount:
    """Which roster entry is the avatar.

    **The bug this closes was making three features wrong at once.** ``display_name`` is what Meet
    is *asked* to call the avatar, and it is asked only when the profile has lost its Google
    session — a signed-in profile renders the account's own name. An avatar signed in as an account
    called "Backend Services" therefore matched nothing configured, and was counted as a
    participant: attendance reported two people present in a call with one other person in it, and
    speaker attribution could not answer "is exactly one other person here" because the avatar was
    one of them.
    """

    def test_the_account_name_wins_over_the_configured_one(self) -> None:
        from src.connectors.google_meet.meeting.participants import parse_roster

        roster = parse_roster(
            {
                "participants": [
                    {"id": "p1", "name": "dev Choudhary"},
                    {"id": "self", "name": "Backend Services"},
                ],
                "selfName": "AI Avatar",
                "accountName": "Backend Services",
            }
        )

        assert roster.self_name == "Backend Services"
        # And therefore the avatar stops being one of the "others" every consumer counts.
        assert [p.display_name for p in roster.others] == ["dev Choudhary"]

    def test_a_page_without_the_account_name_behaves_exactly_as_before(self) -> None:
        """A stale injected script must not change behaviour — it just does not improve it."""
        from src.connectors.google_meet.meeting.participants import parse_roster

        roster = parse_roster(
            {
                "participants": [{"id": "p1", "name": "dev Choudhary"}],
                "selfName": "AI Avatar",
            }
        )

        assert roster.self_name == "AI Avatar"

    def test_the_page_reads_it_from_the_account_button(self, bridge_code: str) -> None:
        """Google's accessible name for that control has kept the shape "Google Account: <name>
        (<email>)" for years — an accessibility obligation rather than a build artefact."""
        assert "function accountName()" in bridge_code
        assert "accountName: account" in bridge_code
        config = DEFAULT_SELECTORS.to_page_config()
        assert config.get("accountName"), "accountName never reaches bridge.js"


class TestTwoObservers:
    """Energy and Meet's own indicator report the same person independently."""

    def test_one_person_seen_twice_is_one_turn(self, tracker, clock) -> None:
        tracker.offer(_edge(track="t1", name="Priya Menon", source=SOURCE_AUDIO))
        clock.advance(200)
        tracker.offer(_edge(track="dom:p1", name="Priya Menon", source=SOURCE_DOM))

        assert len(tracker.snapshot().turns) == 1
        assert tracker.snapshot().current == ("Priya Menon",)

    def test_the_turn_ends_when_the_last_observer_says_so(self, tracker, clock) -> None:
        """Closing on the first stop would end a turn that is visibly still happening — Meet's
        indicator drops out on a re-render, and the level does not."""
        tracker.offer(_edge(track="t1", name="Priya Menon", source=SOURCE_AUDIO))
        tracker.offer(_edge(track="dom:p1", name="Priya Menon", source=SOURCE_DOM))

        clock.advance(1_000)
        tracker.offer(_edge(track="dom:p1", name="Priya Menon", speaking=False, source=SOURCE_DOM))
        assert tracker.current_speaker() == "Priya Menon"
        assert tracker.snapshot().turns[0].is_open

        tracker.offer(_edge(track="t1", name="Priya Menon", speaking=False, source=SOURCE_AUDIO))
        assert not tracker.snapshot().turns[0].is_open

    def test_an_unattributed_track_folds_into_the_named_turn_it_turns_out_to_be(
        self, tracker, clock
    ) -> None:
        """The energy path hears somebody before the mapping names them, and the indicator has
        already named them. One person, one turn — not a real one beside a phantom."""
        tracker.offer(_edge(track="dom:p1", name="Priya Menon", source=SOURCE_DOM))
        clock.advance(100)
        tracker.offer(_edge(track="t1", name=None, source=SOURCE_AUDIO))
        clock.advance(100)
        tracker.offer(_edge(track="t1", name="Priya Menon", source=SOURCE_AUDIO))

        turns = tracker.snapshot().turns
        assert len(turns) == 1
        assert turns[0].display_name == "Priya Menon"


class TestSelfAndMalformedInput:
    def test_the_avatar_is_never_its_own_speaker(self, clock) -> None:
        """Its audio cannot reach the tap at all — the WebRTC hook is inbound-only — so this
        can only be Meet's indicator on our own tile. Counting it would have the avatar report
        itself as the speaker for as long as it talks."""
        tracker = SpeakerTracker(clock=clock, self_names=("AI Avatar",))
        assert tracker.offer(_edge(name="AI Avatar", source=SOURCE_DOM)) is False
        assert tracker.current_speaker() is None
        assert tracker.ignored == 1

    def test_the_account_name_is_learned_from_the_roster(self, clock) -> None:
        """``display_name`` is what we asked to be called; a signed-in profile uses the
        account's own name, and only the roster reveals it."""
        tracker = SpeakerTracker(clock=clock)
        tracker.observe_roster(MeetRoster(self_name="jadumeetboot"))

        assert tracker.offer(_edge(name="jadumeetboot", source=SOURCE_DOM)) is False

    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"speaking": True},
            {"trackId": "", "id": "", "name": ""},
            "not a dict",
            {"trackId": "t1", "speaking": "yes please"},
            {"trackId": None, "name": None, "speaking": True},
        ],
    )
    def test_a_malformed_payload_is_dropped_rather_than_raised(self, tracker, body) -> None:
        """``offer`` runs inside the bridge's read loop, which is the media channel. An
        exception here would stop the meeting's audio in both directions — a catastrophic
        price for a payload from a DOM we do not control."""
        tracker.offer(body)  # must not raise

    def test_a_rejoin_closes_the_turns_the_dead_page_left_open(self, tracker, clock) -> None:
        """No stop edge for them will ever arrive, because the page that opened them is gone.
        Whoever was mid-sentence would otherwise read as speaking for the rest of the call."""
        tracker.offer(_edge())
        clock.advance(1_000)

        tracker.reset()

        assert tracker.current_speaker() is None
        assert tracker.snapshot().turns[0].duration_us() == 1_000_000


class TestAgentBrief:
    def test_it_names_the_speaker_and_the_recent_order(self, tracker, clock) -> None:
        tracker.offer(_edge(track="t1", name="Priya Menon"))
        clock.advance(2_000)
        tracker.offer(_edge(track="t1", name="Priya Menon", speaking=False))
        clock.advance(3_000)
        tracker.offer(_edge(track="t2", name="Aarav Sharma"))

        brief = tracker.snapshot().agent_context()

        assert "Aarav Sharma is speaking right now" in brief
        assert "Priya Menon" in brief

    def test_it_says_when_nobody_is_speaking(self, tracker, clock) -> None:
        tracker.offer(_edge())
        clock.advance(500)
        tracker.offer(_edge(speaking=False))
        clock.advance(9_000)

        assert "Nobody is speaking right now" in tracker.snapshot().agent_context()

    def test_it_admits_what_it_could_not_attribute(self, tracker, clock) -> None:
        """An agent told only that somebody is speaking will answer "who?" with an invention."""
        tracker.offer(_edge(name=None))
        clock.advance(1_000)
        tracker.offer(_edge(name=None, speaking=False))

        assert "could not be attributed" in tracker.snapshot().agent_context()

    def test_an_unattributed_voice_is_not_an_invitation_to_guess(self, tracker) -> None:
        """**The wrong answer this sentence exists to prevent, observed live.** Somebody spoke,
        the page could not name the voice, and the avatar — asked "what is my name?" — answered
        with the name of a *different* participant, who had been typing in the chat. "Someone is
        speaking right now" left a gap, and the agent filled it with the only name it had."""
        tracker.offer(_edge(name=None))

        brief = tracker.snapshot().agent_context().lower()

        assert "cannot tell which participant" in brief
        assert "do not assume it is whoever" in brief

    def test_an_unattributed_voice_is_narrowed_to_the_people_it_could_be(
        self, tracker, clock
    ) -> None:
        """Naming the field is real information, and withholding it is what left the model to
        resolve the question from the chat history instead."""
        tracker.observe_roster(
            MeetRoster(
                participants=(
                    MeetParticipant(page_id="p1", display_name="Backend Services"),
                    MeetParticipant(page_id="p2", display_name="dev Choudhary"),
                    MeetParticipant(page_id="s", display_name="AI Avatar", is_self=True),
                ),
                self_name="AI Avatar",
            )
        )

        tracker.offer(_edge(name=None))

        brief = tracker.snapshot().agent_context()
        assert "one of these people: Backend Services and dev Choudhary" in brief

    def test_the_guard_is_there_before_the_first_edge_arrives(self, tracker) -> None:
        """**The worst moment of a meeting, and it used to be the one moment left uncovered.**
        Somebody joins, speaks immediately, and the agent is asked a question by a voice it has
        been told nothing about — so the paragraph stopped at "nobody has been heard speaking
        yet" and the only name in the frame was the person who had been typing."""
        tracker.observe_roster(
            MeetRoster(
                participants=(
                    MeetParticipant(page_id="p1", display_name="Backend Services"),
                    MeetParticipant(page_id="p2", display_name="dev Choudhary"),
                ),
                self_name="AI Avatar",
            )
        )

        brief = tracker.snapshot().agent_context()

        assert "Nobody has been heard speaking yet" in brief
        assert "Do not assume it is whoever spoke or typed most recently" in brief

    def test_a_named_voice_answers_the_question_in_the_form_it_is_asked(self, tracker) -> None:
        """**The fact was in the brief and the answer still was not.** A live run had the agent
        answer "who is speaking?" with "dev Choudhary" and, from the same brief a minute earlier,
        answer "what is my name?" with "I do not know your name". The agent's own transcription
        hears the words with no name attached, so nothing connects the first-person question to
        the voice unless the brief writes the connection down."""
        tracker.offer(_edge(name="dev Choudhary"))

        brief = tracker.snapshot().agent_context()

        assert "That is the person talking to the avatar" in brief
        assert 'when they say "I", "me" or "my" they mean dev Choudhary' in brief
        assert 'asked "what is my name?" or "who am I?", the answer is dev Choudhary' in brief

    def test_it_names_who_spoke_last_once_the_room_is_quiet(self, tracker, clock) -> None:
        """"Who was talking before me?" is asked in a pause, which is exactly when `current` is
        empty."""
        tracker.offer(_edge(name="dev Choudhary"))
        clock.advance(1_000)
        tracker.offer(_edge(name="dev Choudhary", speaking=False))
        clock.advance(9_000)

        assert "The last person heard speaking was dev Choudhary" in (
            tracker.snapshot().agent_context()
        )

    def test_it_says_this_paragraph_is_about_speech_only(self, tracker) -> None:
        """**The conflation that produced the wrong name.** A participant who has only typed has
        never taken the floor, and an agent reading "recent speakers" as "people who have
        communicated" answers "who is talking to you?" with whoever typed last."""
        tracker.offer(_edge(name="Priya Menon"))

        brief = tracker.snapshot().agent_context()

        assert "counts speech only" in brief
        assert "typed in the chat but has not spoken is not the voice being heard" in brief


class TestSpeakerAnnouncer:
    """Pushing who is speaking as silent context, on change and only on change."""

    def test_the_signature_is_who_is_speaking_and_nothing_else(self, tracker, clock) -> None:
        """Talk times move on every poll; including them would make every tick look like news
        and hand the agent a reason to mention it."""
        tracker.offer(_edge())
        first = signature(tracker.snapshot())
        clock.advance(3_000)

        assert signature(tracker.snapshot()) == first

    async def test_it_pushes_the_brief_when_the_floor_changes_hands(
        self, tracker, clock
    ) -> None:
        avatar = _RecordingAvatar()
        announcer = SpeakerAnnouncer(
            tracker=tracker, avatar=avatar, interval_s=0.01, settle_s=0.0
        )
        tracker.offer(_edge(name="Priya Menon"))
        await announcer.start()
        try:
            await _wait_for(lambda: announcer.sent >= 1)
            first = len(avatar.sent)

            clock.advance(500)
            tracker.offer(_edge(name="Priya Menon", speaking=False))
            clock.advance(5_000)
            tracker.offer(_edge(track="t2", name="Aarav Sharma"))
            await _wait_for(lambda: len(avatar.sent) > first)
        finally:
            await announcer.stop()

        assert avatar.topics == ["speaker", "speaker"]
        assert "Priya Menon is speaking right now" in avatar.sent[0]
        assert "Aarav Sharma is speaking right now" in avatar.sent[1]

    async def test_an_unchanged_speaker_is_not_re_announced(self, tracker) -> None:
        """Standing context. Re-sending an identical brief is noise in a context window."""
        avatar = _RecordingAvatar()
        announcer = SpeakerAnnouncer(
            tracker=tracker, avatar=avatar, interval_s=0.01, settle_s=0.0
        )
        tracker.offer(_edge())
        await announcer.start()
        try:
            await _wait_for(lambda: announcer.sent >= 1)
            await asyncio.sleep(0.05)
        finally:
            await announcer.stop()

        assert len(avatar.sent) == 1

    async def test_a_send_failure_does_not_kill_the_loop(self, tracker) -> None:
        """A session carrying audio in both directions must not die for a context push."""
        avatar = _RecordingAvatar(fail_times=1)
        announcer = SpeakerAnnouncer(
            tracker=tracker, avatar=avatar, interval_s=0.01, settle_s=0.0
        )
        tracker.offer(_edge())
        await announcer.start()
        try:
            await _wait_for(lambda: announcer.sent >= 1)
        finally:
            await announcer.stop()

        assert announcer.sent == 1

    async def test_nothing_is_pushed_before_anybody_has_spoken(self, tracker) -> None:
        """"Nobody has spoken" is true, useless, and would need correcting a second later."""
        avatar = _RecordingAvatar()
        announcer = SpeakerAnnouncer(
            tracker=tracker, avatar=avatar, interval_s=0.01, settle_s=0.0
        )
        await announcer.start()
        try:
            await asyncio.sleep(0.05)
        finally:
            await announcer.stop()

        assert avatar.sent == []


class TestItIsNotOnTheMediaPath:
    """The claim the whole feature rests on, asserted rather than asserted-in-a-docstring."""

    def test_the_audio_wire_is_unchanged(self) -> None:
        """No source id, no new flag, no per-speaker audio message. The capture graph mixes
        before it samples, so a frame carries no attribution and this feature does not pretend
        otherwise — it carries attribution on a *separate* control message."""
        from src.connectors.google_meet.websocket.protocol import (
            AUDIO_HEADER_SIZE,
            MIXED_SOURCE,
            MeetFlags,
            encode_audio,
        )
        from src.domain.avatar import AVATAR_INPUT_FORMAT
        from src.domain.context import FrameContext
        from src.domain.media import AudioFrame

        assert AUDIO_HEADER_SIZE == 12
        assert MIXED_SOURCE == 0
        frame = AudioFrame(
            pcm=bytes(640),
            pts_us=0,
            format=AVATAR_INPUT_FORMAT,
            ctx=FrameContext(session_id="s", correlation_id="c"),
        )
        encoded = encode_audio(frame)
        # Same bytes as before this feature existed: the audio header still ends in the
        # mixed-source sentinel, because a mixed frame has nobody to attribute it to.
        header_size = 24
        audio_header = encoded[header_size : header_size + AUDIO_HEADER_SIZE]
        assert audio_header[-4:] == b"\x00\x00\x00\x00"
        assert MeetFlags.MIXED in MeetFlags

    def test_the_active_speaker_message_is_its_own_type(self) -> None:
        assert MeetMessageType.ACTIVE_SPEAKER == 0x0E
        assert MeetMessageType.ACTIVE_SPEAKER.is_json
        assert not MeetMessageType.ACTIVE_SPEAKER.is_media


class TestPageObserver:
    """Properties of ``js/bridge.js`` the Python side depends on.

    Same discipline as the chat and hand-raise asset tests: the browser half cannot be
    exercised here, so what it must and must not do is asserted against the source.
    """

    def test_the_analyser_is_a_branch_and_never_a_stage(self, bridge_code: str) -> None:
        """**The single most important assertion in this file.**

        The probe connects *from the source node that already feeds the mix*. Inserting it
        between the source and the mix — ``source.connect(analyser); analyser.connect(mix)`` —
        would put a node in the ingest path, which is the one thing this feature is not allowed
        to do. The mix keeps exactly one upstream per track, and it is the source.
        """
        probe = bridge_code.split("function attachSpeakerProbe(", 1)[1].split(
            "\n  function ", 1
        )[0]
        assert "source.connect(analyser)" in probe
        assert "connect(state.captureMix)" not in probe
        assert "analyser.connect(state.speakerSink)" in probe
        # The sink is what keeps the analyser pulled, at zero gain — a branch that reaches the
        # destination silently, exactly as the capture worklet's does.
        assert "sink.gain.value = 0" in probe

    def test_the_mix_is_wired_before_the_probe(self, bridge_code: str) -> None:
        """A probe that throws must cost the attribution and never the meeting's audio."""
        attach = bridge_code.split("async function attachRemoteTrack(", 1)[1].split(
            "\n  function ", 1
        )[0]
        assert attach.index("source.connect(state.captureMix)") < attach.index(
            "attachSpeakerProbe("
        )

    def test_the_level_sampler_touches_no_dom(self, bridge_code: str) -> None:
        """It runs five times a second on the renderer's main thread — the thread that also
        encodes the avatar's camera track and feeds the playout worklet. A DOM read there is
        paid for in media that arrives late, which is what "the avatar is slow" is."""
        sampler = bridge_code.split("function sampleSpeakers()", 1)[1].split(
            "\n  function ", 1
        )[0]
        for forbidden in ("document.", "innerText", "querySelector", "getAttribute"):
            assert forbidden not in sampler, f"the sampler must not touch the DOM: {forbidden}"

    def test_the_sampler_runs_on_its_own_clock(self, bridge_code: str) -> None:
        """Speech does not mutate the DOM. A turn sampled at the mutation rate would start late
        in a visually still meeting and end later."""
        assert "function installSpeakerSampler()" in bridge_code
        assert "setInterval(sampleSpeakers" in bridge_code
        assert "speakerSampleMs" in bridge_code

    def test_speaking_is_hysteretic_rather_than_a_single_threshold(
        self, bridge_code: str
    ) -> None:
        """One threshold makes a speaker flicker on and off across every consonant, and each
        flicker is a turn boundary Python then has to undo."""
        sampler = bridge_code.split("function sampleSpeakers()", 1)[1].split(
            "\n  function ", 1
        )[0]
        assert "speakerStartLevel" in sampler
        assert "speakerStopLevel" in sampler
        assert "speakerReleaseMs" in sampler

    def test_edges_are_reported_rather_than_presence(self, bridge_code: str) -> None:
        """A level signal would put several messages a second per participant on the socket
        that also carries the meeting's audio."""
        sampler = bridge_code.split("function sampleSpeakers()", 1)[1].split(
            "\n  function ", 1
        )[0]
        assert "probe.active" in sampler

    def test_the_stream_mapping_reads_properties_and_not_text(self, bridge_code: str) -> None:
        """``srcObject`` and ``closest`` force no layout; ``innerText`` forces a full-document
        one and is the most expensive thing in this file."""
        mapper = bridge_code.split("function mapSpeakerStreams(", 1)[1].split(
            "\n  function ", 1
        )[0]
        assert "innerText" not in mapper
        assert "srcObject" in mapper
        assert "closest(" in mapper
        assert "speakerMapMs" in mapper

    def test_the_mapping_skips_our_own_audio_elements(self, bridge_code: str) -> None:
        """The bridge appends a muted <audio> per remote track to keep Chromium pulling RTP.
        It holds a remote stream and belongs to no participant, so reading it would map every
        stream to nobody."""
        assert "data-mc-bridge" in bridge_code
        mapper = bridge_code.split("function mapSpeakerStreams(", 1)[1].split(
            "\n  function ", 1
        )[0]
        assert "data-mc-bridge" in mapper

    def test_an_unattributable_indicator_is_skipped_rather_than_keyed(
        self, bridge_code: str
    ) -> None:
        """The lesson from the hand indicator: a constant key for "somebody" reappears on every
        scan and, here, would hold a phantom speaker for the whole meeting."""
        scan = bridge_code.split("function scanSpeakingIndicators()", 1)[1].split(
            "\n  function ", 1
        )[0]
        assert "if (!key) {" in scan

    def test_the_indicator_only_counts_inside_a_participant(self, bridge_code: str) -> None:
        """A selector matching wording matches it anywhere — a menu item reading "Show who is
        speaking" is present for as long as its panel is open, and keyed as a speaker it would
        open a turn that never closes. The energy path carries the feature, so a missed indicator
        costs little and a phantom costs the answer."""
        scan = bridge_code.split("function scanSpeakingIndicators()", 1)[1].split(
            "\n  function ", 1
        )[0]
        assert "if (!holder) {" in scan
        assert "data-participant-id" in scan

    def test_the_matched_phrase_is_taken_out_of_the_name(self, bridge_code: str) -> None:
        """Otherwise the participant is reported as "Priya Menon is speaking" — the mistake
        ``handNameFrom`` already exists to avoid on the hand-raise path."""
        assert "function speakerNameFrom(" in bridge_code
        assert "SPEAKER_PHRASES" in bridge_code

    def test_a_track_that_ends_mid_sentence_closes_its_turn(self, bridge_code: str) -> None:
        """Somebody leaving while talking would otherwise be the current speaker forever."""
        detach = bridge_code.split("function detachSpeakerProbe(", 1)[1].split(
            "\n  function ", 1
        )[0]
        assert "probe.active" in detach
        assert "sendSpeaker(probe, false" in detach

    def test_it_can_be_switched_off_from_python(self, bridge_code: str) -> None:
        assert "CONFIG.speakerTrackingEnabled" in bridge_code

    def test_finding_nobody_is_reported_with_what_the_page_does_have(
        self, bridge_code: str
    ) -> None:
        """A DOM-reading feature that finds nothing is indistinguishable from a quiet meeting.
        The counters separate the two failures: no probes means the analysers never attached,
        probes with no mapping means the levels are measured and nothing says whose they are."""
        assert "speakerNothingSeen" in bridge_code
        assert "streamsOnTiles" in bridge_code
        assert "state.speakerDiagnostics >= 4" in bridge_code

    def test_the_speaking_selectors_reach_the_page(self) -> None:
        """A selector defined in Python but absent from ``to_page_config`` is dead code that
        looks live."""
        config = DEFAULT_SELECTORS.to_page_config()
        assert config.get("speaking"), "speaking never reaches bridge.js"

    def test_the_page_configuration_carries_every_speaker_knob(self) -> None:
        from src.connectors.google_meet.bridge.chromium_bridge import (
            SPEAKER_RELEASE_MS,
            SPEAKER_SAMPLE_MS,
            SPEAKER_START_LEVEL,
            SPEAKER_STOP_LEVEL,
        )

        # Timing belongs in Python, like every other cadence this connector uses.
        assert SPEAKER_SAMPLE_MS > 0
        assert SPEAKER_RELEASE_MS > 0
        assert SPEAKER_START_LEVEL > SPEAKER_STOP_LEVEL > 0


class TestThroughTheBridge:
    """The wire path, over a real socket and the real codec.

    The tracker's own tests prove the bookkeeping; this proves an ``ACTIVE_SPEAKER`` frame
    written by a page actually reaches it. That is the seam a new message type breaks silently:
    a type the Python side does not dispatch is logged as unexpected and dropped, and every
    other signal in the connector keeps reading healthy.
    """

    async def test_a_speaking_edge_from_the_page_reaches_the_tracker(
        self, meet_config, meeting, frame_ctx
    ) -> None:
        from src.connectors.google_meet.bridge.chromium_bridge import ChromiumBridge
        from src.connectors.google_meet.browser.profile import ProfileManager
        from tests.fakes.meet_page import joined_driver

        driver = joined_driver(auto_page=True)
        tracker = SpeakerTracker(clock=MediaClock())
        bridge = ChromiumBridge(
            config=meet_config,
            ctx=frame_ctx,
            clock=MediaClock(),
            driver_factory=lambda: driver,
            profiles=ProfileManager(template=meet_config.require_configured()),
        )
        bridge.attach_speakers(tracker)
        try:
            await bridge.start(meeting)
            await driver.page.send_speaker(name="Priya Menon")
            await _wait_for(lambda: tracker.current_speaker() == "Priya Menon")

            await driver.page.send_speaker(name="Priya Menon", speaking=False)
            await _wait_for(lambda: tracker.snapshot().turns[0].ended_us is not None)
        finally:
            await bridge.stop()

        assert bridge.stats["speaker_events"] == 2

    async def test_the_page_is_told_how_to_measure_a_speaker(
        self, meet_config, meeting, frame_ctx
    ) -> None:
        """Thresholds and cadences belong in Python, like every other knob this connector has —
        so tuning a room is a settings change rather than an edit to the asset that also
        contains the media path."""
        from src.connectors.google_meet.bridge.chromium_bridge import ChromiumBridge
        from src.connectors.google_meet.browser.profile import ProfileManager
        from tests.fakes.meet_page import joined_driver

        driver = joined_driver(auto_page=True)
        bridge = ChromiumBridge(
            config=meet_config,
            ctx=frame_ctx,
            clock=MediaClock(),
            driver_factory=lambda: driver,
            profiles=ProfileManager(template=meet_config.require_configured()),
        )
        try:
            await bridge.start(meeting)
            config = driver.page.config
        finally:
            await bridge.stop()

        assert config["speakerTrackingEnabled"] is True
        assert config["speakerSampleMs"] > 0
        assert config["speakerStartLevel"] > config["speakerStopLevel"]
        assert config["selectors"]["speaking"]


class _RecordingAvatar:
    """Just enough ``AvatarClient`` for the announcer: it only calls one method."""

    def __init__(self, *, fail_times: int = 0) -> None:
        self.sent: list[str] = []
        self.topics: list[str] = []
        self._fail_times = fail_times

    async def send_meeting_context(
        self, text: str, *, topic: str = "attendance", **_: object
    ) -> bool:
        if self._fail_times > 0:
            self._fail_times -= 1
            raise RuntimeError("the avatar socket is down")
        self.sent.append(text)
        self.topics.append(topic)
        return True


async def _wait_for(predicate, *, timeout_s: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition was never met")
        await asyncio.sleep(0.005)


class TestMeetsFirstPerson:
    """"You" is the avatar taking the floor from itself, which is not a thing."""

    def test_the_avatar_is_not_a_speaker_under_meets_first_person(self, tracker) -> None:
        """Observed live: ``participant=You source=dom`` timed exactly to the avatar's greeting,
        pushed to the agent as somebody it should hand the floor to."""
        assert tracker.offer(_edge(name="You", source=SOURCE_DOM)) is False
        assert tracker.current_speaker() is None
        assert tracker.ignored == 1

    def test_a_tile_marked_you_is_also_us(self, tracker) -> None:
        assert tracker.offer(_edge(name="dev Choudhary (You)", source=SOURCE_DOM)) is False


class TestTheTwoHalvesAgree:
    """Energy says *when*, captions and the DOM say *who* — for one voice, not two speakers."""

    def test_an_unnamed_voice_joins_the_one_named_turn(self, tracker, clock) -> None:
        """**The brief said "dev Choudhary and Someone" were both speaking, for one person saying
        one sentence.** The energy probe hears a track it cannot name at the moment Meet names the
        person talking; reporting both means telling the agent two people have the floor.
        """
        tracker.offer(_edge(track="dom:p1", name="dev Choudhary", source=SOURCE_DOM))
        clock.advance(200)

        tracker.offer(_edge(track="t1", name=None, source=SOURCE_AUDIO))

        snapshot = tracker.snapshot()
        assert snapshot.current == ("dev Choudhary",)
        assert len(snapshot.turns) == 1

    def test_it_does_not_guess_when_two_named_people_overlap(self, tracker) -> None:
        """With two named speakers the unnamed voice could be either, and a wrong name is worse
        than none."""
        tracker.offer(_edge(track="dom:p1", name="dev Choudhary", source=SOURCE_DOM))
        tracker.offer(_edge(track="dom:p2", name="Priya Menon", source=SOURCE_DOM))

        tracker.offer(_edge(track="t1", name=None, source=SOURCE_AUDIO))

        assert ANONYMOUS in tracker.snapshot().current

    def test_a_name_arriving_late_claims_the_voice_already_being_heard(
        self, tracker, clock
    ) -> None:
        """**The other arrival order, which is the common one, and it produced two turns for one
        person.** Energy hears a voice the instant it starts and cannot name it; Meet's caption
        names that voice a second or two later, once its transcription has settled. Observed live:

            started attributed=False Someone       source=audio
            started attributed=True  dev Choudhary source=dom
            stopped Someone       seconds=2.4
            stopped dev Choudhary seconds=2.1

        — so the agent was told "Someone is speaking" for the first half of every remark, and,
        asked who was talking, answered with the name of somebody who had been typing.
        """
        tracker.offer(_edge(track="t1", name=None, source=SOURCE_AUDIO))
        clock.advance(1_500)

        tracker.offer(
            _edge(track="dom:caption:dev choudhary", name="dev Choudhary", source=SOURCE_DOM)
        )

        snapshot = tracker.snapshot()
        assert snapshot.current == ("dev Choudhary",)
        assert len(snapshot.turns) == 1, "one voice is one turn, whichever half named it"
        assert snapshot.turns[0].inferred is True, (
            "the page named a voice; that it is *this* voice is our conclusion"
        )

    def test_the_whole_turn_is_credited_not_just_the_named_part(self, tracker, clock) -> None:
        """The talk-time figures are the reason: splitting one remark leaves half of it under
        "Someone", so nobody's share of the meeting adds up."""
        tracker.offer(_edge(track="t1", name=None, source=SOURCE_AUDIO))
        clock.advance(2_000)
        tracker.offer(_edge(track="dom:c", name="dev Choudhary", source=SOURCE_DOM))
        clock.advance(1_000)
        tracker.offer(_edge(track="t1", name=None, speaking=False, source=SOURCE_AUDIO))
        tracker.offer(_edge(track="dom:c", name="dev Choudhary", speaking=False, source=SOURCE_DOM))

        totals = dict(tracker.snapshot().talk_time())
        assert totals == {"dev Choudhary": 3}
        assert ANONYMOUS not in totals

    def test_a_caption_settling_just_after_the_voice_stopped_still_claims_it(
        self, tracker, clock
    ) -> None:
        """Meet's caption lands *after* the speaker has paused, which is the same gap this class
        already treats as punctuation rather than an ending."""
        tracker.offer(_edge(track="t1", name=None, source=SOURCE_AUDIO))
        clock.advance(2_000)
        tracker.offer(_edge(track="t1", name=None, speaking=False, source=SOURCE_AUDIO))
        clock.advance(600)  # inside the 1.2 s merge gap

        tracker.offer(_edge(track="dom:c", name="dev Choudhary", source=SOURCE_DOM))

        snapshot = tracker.snapshot()
        assert len(snapshot.turns) == 1
        assert snapshot.turns[0].label == "dev Choudhary"

    def test_a_name_long_after_the_silence_opens_its_own_turn(self, tracker, clock) -> None:
        """Past the merge gap it is a new remark, and claiming the old one would rewrite history
        on the strength of somebody else starting to talk."""
        tracker.offer(_edge(track="t1", name=None, source=SOURCE_AUDIO))
        tracker.offer(_edge(track="t1", name=None, speaking=False, source=SOURCE_AUDIO))
        clock.advance(30_000)

        tracker.offer(_edge(track="dom:c", name="dev Choudhary", source=SOURCE_DOM))

        labels = [turn.label for turn in tracker.snapshot().turns]
        assert labels == [ANONYMOUS, "dev Choudhary"]

    def test_it_does_not_choose_between_two_anonymous_voices(self, tracker, clock) -> None:
        """Which of them the name belongs to is a guess, so it opens a turn of its own."""
        tracker.offer(_edge(track="t1", name=None, source=SOURCE_AUDIO))
        tracker.offer(_edge(track="t2", name=None, source=SOURCE_AUDIO))
        clock.advance(300)

        tracker.offer(_edge(track="dom:c", name="dev Choudhary", source=SOURCE_DOM))

        assert len(tracker.snapshot().turns) == 3
        assert ANONYMOUS in tracker.snapshot().current

    def test_it_does_not_claim_a_voice_while_somebody_named_is_talking(
        self, tracker, clock
    ) -> None:
        """Two people are audible and one of them is anonymous: the new name could be either, and
        the anonymous voice could be a third person. Failing closed is the rule the rest of this
        class is built on."""
        tracker.offer(_edge(track="t1", name=None, source=SOURCE_AUDIO))
        tracker.offer(_edge(track="t2", name=None, source=SOURCE_AUDIO))
        tracker.offer(_edge(track="dom:p1", name="Priya Menon", source=SOURCE_DOM))
        clock.advance(300)

        tracker.offer(_edge(track="dom:p2", name="dev Choudhary", source=SOURCE_DOM))

        current = tracker.snapshot().current
        assert ANONYMOUS in current
        assert {"Priya Menon", "dev Choudhary"} <= set(current)

    def test_the_avatars_own_open_turn_is_never_adopted(self, clock) -> None:
        """The avatar is filtered before it can open a turn, so there is nothing to adopt — which
        is what stops its own name landing on a participant's voice."""
        tracker = SpeakerTracker(clock=clock, self_names=("Backend Services",))
        tracker.offer(_edge(track="dom:self", name="You", source=SOURCE_DOM))

        tracker.offer(_edge(track="t1", name=None, source=SOURCE_AUDIO))

        assert tracker.current_speaker() == ANONYMOUS
