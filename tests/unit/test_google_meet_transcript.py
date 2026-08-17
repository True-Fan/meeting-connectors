"""What each person said, attributed to them.

**The gap this closes, stated precisely, because it is the reason the feature exists.** Asked
*"what did they ask you?"*, the avatar could only say it did not know — and no amount of work on
the speaker detector would have changed that. Attribution here is built from audio *levels*, so it
knows who is talking and never what was said; the agent's transcription receives **one mixed
stream**, so it knows what was said and can never attribute it. Neither half can become the other.

Meet's caption panel is where Meet has already joined them: it writes the speaker's name and the
words that person said, side by side, from its own per-participant transcription. So these tests
are about turning that panel into a ledger — and about the three ways a caption panel differs from
a chat panel, each of which produces a distinct wrong answer if ignored:

* a caption is **not final when it appears** — Meet extends it word by word, so forwarding on sight
  delivers a dozen fragments of one sentence;
* the panel **re-renders constantly**, so a settled line can be presented again;
* the avatar's own speech is captioned too, and reading it back as a participant's turn would have
  the agent answering itself.
"""

from __future__ import annotations

import re

import pytest

from src.connectors.google_meet.automation.selectors import DEFAULT_SELECTORS
from src.connectors.google_meet.js import BRIDGE_ASSET, read_asset
from src.connectors.google_meet.meeting.participants import MeetParticipant, MeetRoster
from src.connectors.google_meet.meeting.transcript import (
    _MAX_LINES,
    ANONYMOUS,
    SELF_LABEL,
    MeetTranscript,
)
from src.connectors.google_meet.websocket.protocol import MeetMessageType


@pytest.fixture(scope="module")
def bridge_code() -> str:
    source = read_asset(BRIDGE_ASSET)
    without_blocks = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    return re.sub(r"(?<!:)//[^\n]*", "", without_blocks)


@pytest.fixture
def transcript() -> MeetTranscript:
    return MeetTranscript(self_names=("AI Avatar",))


def _caption(speaker: str | None = "Dev Choudhary", text: str = "Tell me about Delhi") -> dict:
    return {"speaker": speaker, "text": text, "isSelf": False}


class TestTheLedger:
    def test_a_line_is_recorded_against_its_speaker(self, transcript) -> None:
        transcript.offer(_caption())

        (line,) = transcript.snapshot().lines
        assert line.speaker == "Dev Choudhary"
        assert line.text == "Tell me about Delhi"
        assert line.render() == "Dev Choudhary: Tell me about Delhi"

    def test_the_same_line_is_not_recorded_twice(self, transcript) -> None:
        """Meet re-renders the caption panel constantly; the page settles a line and the panel can
        present it again. Without identity the transcript reads as a stutter."""
        transcript.offer(_caption())
        transcript.offer(_caption())

        assert transcript.count == 1

    def test_the_same_words_from_two_people_are_two_lines(self, transcript) -> None:
        transcript.offer(_caption(speaker="Dev Choudhary", text="yes"))
        transcript.offer(_caption(speaker="Priya Menon", text="yes"))

        assert transcript.count == 2

    def test_an_unnamed_caption_is_still_kept(self, transcript) -> None:
        """Meet captions a continuation block without repeating the name. The words are still
        worth having — and "Someone" is honest where an empty gap is unreadable."""
        transcript.offer(_caption(speaker=None, text="and after that?"))

        assert transcript.snapshot().lines[0].label == ANONYMOUS

    def test_what_one_person_said_can_be_asked_for(self, transcript) -> None:
        transcript.offer(_caption(speaker="Dev Choudhary", text="Tell me about Delhi"))
        transcript.offer(_caption(speaker="Priya Menon", text="And Mumbai?"))
        transcript.offer(_caption(speaker="Dev Choudhary", text="What about India Gate?"))

        said = transcript.snapshot().by_speaker("dev choudhary")

        assert [line.text for line in said] == [
            "Tell me about Delhi",
            "What about India Gate?",
        ]

    def test_the_speakers_are_listed_in_the_order_they_first_spoke(self, transcript) -> None:
        transcript.offer(_caption(speaker="Priya Menon", text="hello"))
        transcript.offer(_caption(speaker="Dev Choudhary", text="hi"))
        transcript.offer(_caption(speaker="Priya Menon", text="how are you"))

        assert transcript.snapshot().speakers == ("Priya Menon", "Dev Choudhary")

    def test_the_avatars_own_turn_is_marked_rather_than_dropped(self, transcript) -> None:
        """Kept, because a transcript missing half a conversation is not one — and marked, so the
        brief presents it as the avatar's own turn instead of as something it was asked."""
        transcript.observe_roster(MeetRoster(self_name="Backend Services"))
        transcript.offer(_caption(speaker="Backend Services", text="India Gate is in Delhi"))

        (line,) = transcript.snapshot().lines
        assert line.is_self is True

    def test_the_account_name_is_learned_from_the_roster(self, transcript) -> None:
        transcript.observe_roster(
            MeetRoster(
                participants=(
                    MeetParticipant(page_id="s", display_name="Backend Services", is_self=True),
                ),
            )
        )

        transcript.offer(_caption(speaker="Backend Services", text="hello"))

        assert transcript.snapshot().lines[0].is_self is True

    def test_the_ledger_is_bounded(self) -> None:
        transcript = MeetTranscript()
        for index in range(520):
            transcript.offer(_caption(text=f"line {index}"))

        lines = transcript.snapshot().lines
        assert len(lines) <= 500
        # Oldest dropped: this answers "what was just said" far more often than "what was said an
        # hour ago".
        assert lines[-1].text == "line 519"
        assert transcript.dropped > 0

    @pytest.mark.parametrize(
        "body",
        [{}, {"text": ""}, {"speaker": "Dev"}, "not a dict", {"text": "   "}, {"text": None}],
    )
    def test_a_malformed_payload_is_dropped_rather_than_raised(self, transcript, body) -> None:
        """``offer`` runs in the bridge's read loop, which is the media channel."""
        transcript.offer(body)  # must not raise
        assert transcript.count == 0

    def test_a_container_label_is_not_a_speaker(self, transcript) -> None:
        """The roster's cleaner again: icon-font glyphs mean the label was a tile's whole text."""
        transcript.offer(_caption(speaker="frame_person Reframe more_vert", text="hello"))

        assert transcript.snapshot().lines[0].label == ANONYMOUS


class TestTheAgentBrief:
    def test_it_reads_as_dialogue(self, transcript) -> None:
        """Rendered as a conversation rather than as JSON because its destination is a context
        window: a list of ``{speaker, text}`` objects is a data structure, and "Dev Choudhary: Tell
        me about Delhi" is something an LLM can answer questions about."""
        transcript.offer(_caption(speaker="Dev Choudhary", text="Tell me about Delhi"))
        transcript.offer(_caption(speaker="Priya Menon", text="And India Gate?"))

        brief = transcript.snapshot().agent_context()

        assert "- Dev Choudhary: Tell me about Delhi" in brief
        assert "- Priya Menon: And India Gate?" in brief

    def test_it_says_the_wording_is_a_transcription(self, transcript) -> None:
        """Meet's captions mishear names and technical words. An agent that quotes them as verbatim
        fact will occasionally be confidently wrong about what somebody said."""
        transcript.offer(_caption())

        assert "captions" in transcript.snapshot().agent_context()

    def test_it_is_a_window_rather_than_the_whole_meeting(self, transcript) -> None:
        """The brief is standing context, re-sent whenever it changes. An unbounded transcript
        would grow the frame without limit and crowd out the conversation the agent is having."""
        for index in range(40):
            transcript.offer(_caption(text=f"line {index}"))

        brief = transcript.snapshot().agent_context()

        assert "line 39" in brief
        assert "line 0" not in brief
        assert "earlier lines not shown" in brief

    def test_an_empty_transcript_renders_nothing(self, transcript) -> None:
        """So a meeting where nobody has spoken adds no words to the brief at all."""
        assert transcript.snapshot().agent_context() == ""


class TestWhatWasTyped:
    """Chat is half the conversation, and it was recorded nowhere.

    **The live run this is written against.** A participant asked five questions, every one of
    them typed into the meeting chat, and the avatar answered every one out loud. Asked afterwards
    what conversation had taken place and with whom, it said it had been greeting somebody and
    asking how they were — which was an accurate summary of the *only* thing it had been told,
    because the ledger held four captions of the avatar's own voice and nothing else. Each chat
    message had crossed the avatar socket once, been answered, and left no trace anywhere.

    So chat lands in the same ledger captions do. What is under test is that it lands there
    *marked*: typing is not speaking, and an agent that reports hearing somebody who never opened
    their microphone has been handed a different wrong answer.
    """

    def test_a_typed_message_becomes_a_line_of_the_conversation(self, transcript) -> None:
        transcript.offer_chat({"text": "what is my name?", "sender": "Backend Services"})

        (line,) = transcript.snapshot().lines
        assert line.speaker == "Backend Services"
        assert line.text == "what is my name?"
        assert line.in_chat is True

    def test_it_is_rendered_as_typed_rather_than_spoken(self, transcript) -> None:
        """The distinction survives into the brief, so "who was speaking?" is not answered with
        somebody who only ever typed."""
        transcript.offer_chat({"text": "who is here?", "sender": "Backend Services"})

        assert transcript.snapshot().lines[0].render() == "Backend Services (in chat): who is here?"

    def test_the_brief_carries_both_halves_of_the_conversation(self, transcript) -> None:
        transcript.offer(_caption(speaker="Dev Choudhary", text="Tell me about Delhi"))
        transcript.offer_chat({"text": "and Agra?", "sender": "Dev Choudhary"})

        brief = transcript.snapshot().agent_context()

        assert "- Dev Choudhary: Tell me about Delhi" in brief
        assert "- Dev Choudhary (in chat): and Agra?" in brief

    def test_an_all_chat_meeting_is_not_described_as_a_transcription(self, transcript) -> None:
        """A caption is Meet's guess at what it heard; a chat message is the exact characters
        somebody typed. Telling the agent the wording may be imperfect makes it hedge on a line
        it should be quoting."""
        transcript.offer_chat({"text": "what is the notice period?", "sender": "Priya"})

        brief = transcript.snapshot().agent_context()

        assert "typed into the meeting chat" in brief
        assert "imperfect" not in brief

    def test_the_same_message_is_not_recorded_twice(self, transcript) -> None:
        """Meet re-renders the chat list on almost every DOM mutation — the hazard the page's
        message ids exist for, and the ledger honours the same id."""
        assert transcript.offer_chat({"text": "hello", "sender": "Dev"}, message_id="m1")
        assert not transcript.offer_chat({"text": "hello", "sender": "Dev"}, message_id="m1")

        assert transcript.count == 1

    def test_the_same_words_spoken_and_typed_are_two_lines(self, transcript) -> None:
        """Somebody reading their own question aloud is a real thing that happens, and it is two
        events in the meeting rather than a re-render of one."""
        transcript.offer_chat({"text": "where is the Taj Mahal?", "sender": "Dev"})
        transcript.offer(_caption(speaker="Dev", text="where is the Taj Mahal?"))

        assert transcript.count == 2

    def test_an_unnamed_message_is_credited_to_the_only_other_person(self, transcript) -> None:
        """**This is the fix that did not depend on Meet's markup, and it would have named the
        sender of every message in the run above.** The page could not read a name off the row;
        the roster had named the one other participant before the first message arrived."""
        transcript.observe_roster(
            MeetRoster(
                participants=(
                    MeetParticipant(page_id="p1", display_name="Backend Services"),
                    MeetParticipant(page_id="self", display_name="AI Avatar", is_self=True),
                ),
                self_name="AI Avatar",
            )
        )

        transcript.offer_chat({"text": "what is my name?", "sender": None})

        line = transcript.snapshot().lines[0]
        assert line.label == "Backend Services"
        assert line.inferred is True

    def test_with_two_others_it_stays_unattributed(self, transcript) -> None:
        transcript.observe_roster(
            MeetRoster(
                participants=(
                    MeetParticipant(page_id="p1", display_name="Dev"),
                    MeetParticipant(page_id="p2", display_name="Priya"),
                ),
                self_name="AI Avatar",
            )
        )

        transcript.offer_chat({"text": "hello", "sender": None})

        assert transcript.snapshot().lines[0].label == ANONYMOUS

    def test_the_avatars_own_message_is_marked_as_its_own(self, transcript) -> None:
        """Recorded rather than dropped — a transcript that omits half a conversation is not one
        — and labelled as the avatar, so it is not read back as something to answer."""
        transcript.offer_chat({"text": "I am Gunika", "sender": "AI Avatar"})

        line = transcript.snapshot().lines[0]
        assert line.is_self is True
        assert line.render().startswith(SELF_LABEL)

    def test_a_malformed_payload_costs_a_line_and_nothing_else(self, transcript) -> None:
        assert transcript.offer_chat("not a dict") is False  # type: ignore[arg-type]
        assert transcript.offer_chat({"text": ""}) is False
        assert transcript.count == 0

    def test_the_dedupe_set_is_bounded_by_the_ledger(self, transcript) -> None:
        """A message id is not derivable from the line it produced, so an evicted line's key has
        to be retired with it. Recomputing on eviction would leave every id in the dedupe set for
        the life of the session, which is a leak wearing a cap's clothing."""
        for index in range(_MAX_LINES + 10):
            transcript.offer_chat({"text": f"line {index}"}, message_id=f"m{index}")

        assert transcript.count == _MAX_LINES
        assert transcript.dropped == 10
        # The oldest id was released with its line: offering it again records it, where a leaked
        # key would silently swallow it.
        assert transcript.offer_chat({"text": "line 0"}, message_id="m0") is True


class TestThePageReader:
    """Properties of the caption reader in ``js/bridge.js``."""

    def test_a_caption_is_forwarded_only_once_it_settles(self, bridge_code: str) -> None:
        """Meet extends a caption word by word while somebody talks. Forwarding on sight would
        deliver one sentence as a dozen fragments."""
        from src.connectors.google_meet.bridge.chromium_bridge import CAPTION_SETTLE_MS

        assert CAPTION_SETTLE_MS > 0
        scan = bridge_code.split("function scanCaptions()", 1)[1].split("\n  function ", 1)[0]
        assert "captionSettleMs" in scan
        assert "changedAt" in scan

    def test_the_speaker_and_the_words_are_split_on_the_rendered_line(
        self, bridge_code: str
    ) -> None:
        """``textContent`` would run them together — "Dev ChoudharyTell me about Delhi" — with no
        way to tell where the name ended. Meet renders them as separate blocks, so the *rendered*
        line break is the split, and only ``innerText`` performs it."""
        lines = bridge_code.split("function captionLines(node)", 1)[1].split(
            "\n  function ", 1
        )[0]
        assert "innerText" in lines
        assert "split('\\n')" in lines

    def test_the_name_is_looked_for_beyond_the_matched_element(self, bridge_code: str) -> None:
        """**The failure this fixes captured a whole meeting and named nobody in it.** The block
        selectors match the element holding the *words*; Meet renders the name in a sibling of it.
        So the reader climbs to the caption row, and — better — reads the participant photo's
        ``alt``, which is a name by definition and is an accessibility obligation rather than a
        build artefact."""
        assert "function captionSpeakerFromImage(" in bridge_code
        parse = bridge_code.split("function parseCaptionBlock(node)", 1)[1].split(
            "\n  function ", 1
        )[0]
        assert "scope.parentElement" in parse, "it must look past the matched element"
        assert "fromImage" in parse

    def test_the_climb_is_bounded(self, bridge_code: str) -> None:
        """Climbing to the panel would swallow every speaker's line into one entry."""
        parse = bridge_code.split("function parseCaptionBlock(node)", 1)[1].split(
            "\n  function ", 1
        )[0]
        assert "depth < 2" in parse

    def test_the_caption_read_is_rate_limited(self, bridge_code: str) -> None:
        """It is the one ``innerText`` read this file adds, and it is affordable only because the
        panel is a few short lines rather than the document."""
        from src.connectors.google_meet.bridge.chromium_bridge import CAPTION_SCAN_MS

        assert CAPTION_SCAN_MS > 0
        assert "captionScanMs" in bridge_code

    def test_captions_are_switched_on_within_a_wall_clock_window(self, bridge_code: str) -> None:
        """The chat button's lesson, not repeated: a scan-count budget is spent in seconds because
        Meet mutates continuously, and Meet has not drawn its control bar yet."""
        assert "function ensureCaptions()" in bridge_code
        assert "captionOpenWindowMs" in bridge_code
        assert "captionOpenRetryMs" in bridge_code

    def test_the_button_is_also_found_by_its_label(self, bridge_code: str) -> None:
        assert "findCaptionButtonByLabel" in bridge_code

    def test_it_never_clicks_the_control_that_turns_captions_off(self, bridge_code: str) -> None:
        """The same asymmetry ``mute_toggle`` relies on — a label states the action, so "Turn off
        captions" is the one thing this must not match."""
        finder = bridge_code.split("function findCaptionButtonByLabel()", 1)[1].split(
            "\n  function ", 1
        )[0]
        assert "'turn off'" in finder
        for selector in DEFAULT_SELECTORS.captions_button:
            assert "turn off" not in selector.lower(), selector

    def test_a_caption_also_reports_who_is_speaking(self, bridge_code: str) -> None:
        """Meet naming somebody in words is a better speaking signal than any indicator this file
        could match on, and it arrives on the same channel so one person named by two routes is
        still one speaker."""
        assert "function raiseCaptionSpeaker(" in bridge_code
        assert "speakerDomActive" in bridge_code

    def test_finding_nothing_reports_what_the_panel_holds(self, bridge_code: str) -> None:
        """``on: False`` is a button problem and ``on: True`` with no blocks is a block-selector
        problem — two different fixes that would otherwise look identical."""
        from src.connectors.google_meet.bridge.chromium_bridge import CAPTION_DIAG_MS

        assert CAPTION_DIAG_MS > 0
        assert "captionsNothingSeen" in bridge_code
        assert "regionText" in bridge_code

    def test_captions_can_be_switched_off_from_python(self, bridge_code: str) -> None:
        assert "CONFIG.captionsEnabled" in bridge_code

    def test_every_caption_selector_reaches_the_page(self) -> None:
        config = DEFAULT_SELECTORS.to_page_config()
        for key in ("captionsButton", "captionsRegion", "captionBlock"):
            assert key in config, f"{key} never reaches bridge.js"
            assert config[key], f"{key} has no candidates"

    def test_the_caption_message_is_its_own_type(self) -> None:
        assert MeetMessageType.CAPTION == 0x0F
        assert MeetMessageType.CAPTION.is_json


class TestTheDiagnosticGate:
    """The gate that has now hidden two live runs, one level deeper each time.

    The speaker diagnostic first fired only while *no edges* had been sent — and the run that
    needed it sent edges continuously and attributed none of them, which is the exact state the
    report exists to explain. Gating on attribution fixed that and introduced the next one:
    **captions raise named edges too.** A later run had Meet's caption panel name four speakers a
    second and a half after each started talking, while the speaking-indicator selectors matched
    nothing whatsoever for the entire meeting — and the report that would have printed the
    indicator's real markup counted those four as success and stayed silent.

    So the gate is now the attributions that could name a voice *while it is still speaking*.
    """

    def test_the_speaker_diagnostic_gates_on_live_attribution(self, bridge_code: str) -> None:
        diag = bridge_code.split("function reportSpeakerDiagnostics(now)", 1)[1].split(
            "\n  function ", 1
        )[0]
        assert "state.speakerIndicatorAttributed > 0" in diag
        assert "state.speakerEventsSent > 0" not in diag, (
            "gating on edges sent is what made an unattributed run undiagnosable"
        )
        assert "state.speakerAttributed > 0" not in diag, (
            "gating on any attribution lets the caption panel switch off the report that "
            "explains why the indicator never matches"
        )

    def test_a_caption_does_not_count_as_live_attribution(self, bridge_code: str) -> None:
        """A caption naming a speaker is a fine attribution and a poor substitute: it lands after
        the words have settled, so the first seconds of every remark are still anonymous."""
        sender = bridge_code.split("function sendDomSpeaker(key, entry, speaking, now)", 1)[
            1
        ].split("\n  function ", 1)[0]
        assert "if (!entry.caption)" in sender

    def test_all_three_counters_are_reported(self, bridge_code: str) -> None:
        """Their differences are the diagnosis: edges without attribution means detection works
        and naming does not; attribution without *live* attribution means only captions name
        anybody, and they are always late."""
        assert "attributed: state.speakerAttributed" in bridge_code
        assert "attributedLive: state.speakerIndicatorAttributed" in bridge_code
        assert "edges: state.speakerEventsSent" in bridge_code


class TestAttributionByElimination:
    """Naming a caption Meet did not name, in the case that matters.

    A live run captured eleven caption lines and Meet's name was in none of them — the panel puts
    it in a sibling of the element the block selectors match. Reading that is fixed separately; this
    is the answer that does not depend on markup at all: in a two-person meeting there is exactly
    one person the words can belong to.
    """

    def _two_person_roster(self) -> MeetRoster:
        return MeetRoster(
            participants=(
                MeetParticipant(page_id="p1", display_name="dev Choudhary"),
                MeetParticipant(page_id="self", display_name="Backend Services", is_self=True),
            ),
            self_name="Backend Services",
        )

    def test_an_unnamed_caption_is_credited_to_the_only_other_person(self, transcript) -> None:
        transcript.observe_roster(self._two_person_roster())

        transcript.offer({"speaker": None, "text": "Tell me about India Gate"})

        (line,) = transcript.snapshot().lines
        assert line.label == "dev Choudhary"
        assert line.inferred is True

    def test_with_two_others_it_stays_unattributed(self, transcript) -> None:
        """A confident wrong name is the one output worse than "Someone"."""
        transcript.observe_roster(
            MeetRoster(
                participants=(
                    MeetParticipant(page_id="p1", display_name="dev Choudhary"),
                    MeetParticipant(page_id="p2", display_name="Priya Menon"),
                ),
                self_name="Backend Services",
            )
        )

        transcript.offer({"speaker": None, "text": "hello"})

        assert transcript.snapshot().lines[0].label == ANONYMOUS

    def test_it_never_credits_a_line_to_the_avatar_itself(self, clock_free_transcript) -> None:
        """Self-detection can fail, leaving the avatar as the only "other". Its own captioned
        speech must be marked as its own, never presented as a participant's question."""
        transcript = clock_free_transcript
        transcript.observe_self_name("Backend Services")
        transcript.observe_roster(
            MeetRoster(
                participants=(MeetParticipant(page_id="p1", display_name="Backend Services"),),
            )
        )

        transcript.offer({"speaker": None, "text": "India Gate is in Delhi"})

        assert transcript.snapshot().lines[0].label == ANONYMOUS

    def test_a_named_caption_is_never_overridden(self, transcript) -> None:
        transcript.observe_roster(self._two_person_roster())

        transcript.offer({"speaker": "Priya Menon", "text": "hello"})

        line = transcript.snapshot().lines[0]
        assert line.label == "Priya Menon"
        assert line.inferred is False


@pytest.fixture
def clock_free_transcript() -> MeetTranscript:
    """A transcript with no seeded self names, so a test can set them explicitly."""
    return MeetTranscript()


class TestMeetsFirstPerson:
    """"You" is the avatar, and treating it as a participant fed the agent its own words.

    **Observed live, and unambiguous once seen.** Captions are rendered in the avatar's browser, so
    Meet labels the *local* participant's captions "You" — and the local participant is the avatar.
    A run recorded ``speaker=You is_self=False`` for lines timed exactly to the avatar's own
    greeting, then pushed them to the agent as things it had been asked. The hand-raise observer has
    known this since it was written; captions and speaker attribution each had to learn it.
    """

    def test_a_you_caption_is_the_avatars_own_turn(self, transcript) -> None:
        transcript.offer({"speaker": "You", "text": "Hello! I am Gunika", "isSelf": False})

        (line,) = transcript.snapshot().lines
        assert line.is_self is True

    def test_it_is_kept_and_labelled_as_the_avatar(self, transcript) -> None:
        """Kept, because a transcript missing the avatar's half is not a conversation — and
        labelled as the avatar, because the brief's reader *is* the avatar: its account name would
        read as a third party whose question is owed an answer."""
        transcript.offer({"speaker": "You", "text": "Hello! I am Gunika"})

        brief = transcript.snapshot().agent_context()

        assert "The avatar (you): Hello! I am Gunika" in brief

    @pytest.mark.parametrize("label", ["You", "you", "YOU", "Dev Choudhary (You)"])
    def test_every_first_person_wording_counts(self, transcript, label) -> None:
        transcript.offer({"speaker": label, "text": "hello"})

        assert transcript.snapshot().lines[0].is_self is True

    def test_it_is_not_credited_to_the_only_other_participant(self, transcript) -> None:
        """The dangerous interaction: elimination would otherwise take the avatar's own sentence
        and put the candidate's name on it."""
        transcript.observe_roster(
            MeetRoster(
                participants=(
                    MeetParticipant(page_id="p1", display_name="dev Choudhary"),
                    MeetParticipant(page_id="self", display_name="Backend Services", is_self=True),
                ),
                self_name="Backend Services",
            )
        )

        transcript.offer({"speaker": "You", "text": "India Gate is in Delhi"})

        line = transcript.snapshot().lines[0]
        assert line.is_self is True
        assert line.label != "dev Choudhary"
        assert line.inferred is False


class TestNameProvenance:
    """Where a caption's name came from, recorded beside it.

    A live run produced a line credited to "Backend Services" while the same seconds also produced
    lines credited to the avatar — and the log could not say whether that name was read from the
    caption's own row, borrowed from a participant photo rendered nearby, or inferred. A name that
    turns out to be wrong is only diagnosable if its source is recorded.
    """

    def test_the_source_is_carried_through_to_the_log(self, transcript, capsys) -> None:
        """Asserted against what is actually printed, because that is the artefact a live run
        leaves behind and the only thing a diagnosis has to work from."""
        transcript.offer(
            {"speaker": "dev Choudhary", "text": "Tell me about Delhi", "nameFrom": "img"}
        )

        assert "name_from=img" in capsys.readouterr().out

    def test_an_inferred_name_says_so_rather_than_borrowing_a_source(
        self, transcript, capsys
    ) -> None:
        """Elimination is a different claim from "Meet said so", and the log must not blur them."""
        transcript.observe_roster(
            MeetRoster(
                participants=(
                    MeetParticipant(page_id="p1", display_name="dev Choudhary"),
                    MeetParticipant(page_id="self", display_name="Backend Services", is_self=True),
                ),
                self_name="Backend Services",
            )
        )
        capsys.readouterr()

        transcript.offer({"speaker": None, "text": "hello", "nameFrom": "none"})

        assert "name_from=inferred" in capsys.readouterr().out

    def test_the_image_search_cannot_reach_a_neighbouring_tile(self, bridge_code: str) -> None:
        """Two levels, not three: climbing further reaches the panel — and in a grid layout the
        participant tiles — where the first ``img[alt]`` is whoever is rendered nearby rather than
        whoever is talking. A missed name costs a "Someone"; a borrowed one puts the wrong person's
        name on somebody else's sentence."""
        image = bridge_code.split("function captionSpeakerFromImage(node)", 1)[1].split(
            "\n  function ", 1
        )[0]
        assert "depth < 2" in image

    def test_the_page_reports_which_source_named_the_speaker(self, bridge_code: str) -> None:
        assert "nameFrom: fromImage ? 'img' : 'line'" in bridge_code
        assert "nameFrom: block.nameFrom" in bridge_code


class TestTheSelfNameReading:
    """The one log line that settles which roster entry is the avatar."""

    def test_it_is_logged_on_the_first_roster_that_has_anybody_in_it(self) -> None:
        """**A bug in the diagnostic itself, and the reason two live runs could not answer the
        question it was added for.** The first roster of a session arrives empty — the page reports
        before Meet has drawn a tile — so gating on a change of ``self_name`` logged the reading
        against ``entries=[]`` and never again, because the fallback name never changed.
        """
        from pathlib import Path

        from src.connectors.google_meet.bridge import chromium_bridge

        text = Path(chromium_bridge.__file__).read_text()

        assert "first_populated = roster.count > 0 and self._roster.count == 0" in text
        assert "if first_populated or roster.self_name != self._roster.self_name:" in text


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


async def _wait_for(predicate, *, timeout_s: float = 2.0) -> None:
    import asyncio

    deadline = asyncio.get_running_loop().time() + timeout_s
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition was never met")
        await asyncio.sleep(0.005)


class TestThroughTheBridge:
    """The wire path, over a real socket and the real codec.

    **This is the seam the whole failure lived in, and it broke without anything looking
    broken.** ``CHAT_MESSAGE`` was dispatched to exactly one sink — the source that decides what
    to answer — so every message was answered and none was remembered. Every counter downstream
    read healthy, because each of them was doing its own job correctly. The only place the gap
    was visible was in what the avatar could say about its own meeting.
    """

    async def _bridge(self, meet_config, frame_ctx, driver):
        from src.connectors.google_meet.bridge.chromium_bridge import ChromiumBridge
        from src.connectors.google_meet.browser.profile import ProfileManager
        from src.services.media.clock import MediaClock

        return ChromiumBridge(
            config=meet_config,
            ctx=frame_ctx,
            clock=MediaClock(),
            driver_factory=lambda: driver,
            profiles=ProfileManager(template=meet_config.require_configured()),
        )

    async def test_a_typed_message_reaches_the_transcript(
        self, meet_config, meeting, frame_ctx
    ) -> None:
        from tests.fakes.meet_page import joined_driver

        driver = joined_driver(auto_page=True)
        transcript = MeetTranscript(self_names=("AI Avatar",))
        bridge = await self._bridge(meet_config, frame_ctx, driver)
        bridge.attach_transcript(transcript)
        try:
            await bridge.start(meeting)
            await driver.page.send_chat(text="what is my name?", sender="Backend Services")
            await _wait_for(lambda: transcript.count == 1)
        finally:
            await bridge.stop()

        (line,) = transcript.snapshot().lines
        assert line.render() == "Backend Services (in chat): what is my name?"

    async def test_the_transcript_keeps_what_the_answering_filter_discards(
        self, meet_config, meeting, frame_ctx
    ) -> None:
        """Two sinks, two questions. Whether to *answer* a message is policy — a message between
        two participants is not the avatar's to answer — and what was *said* is history. A ledger
        that inherits the answering filter cannot summarise a conversation it stayed out of.
        """
        from src.connectors.google_meet.meeting.chat import MeetChatSource
        from src.services.media.clock import MediaClock
        from tests.fakes.meet_page import joined_driver

        driver = joined_driver(auto_page=True)
        transcript = MeetTranscript(self_names=("AI Avatar",))
        chat = MeetChatSource(
            clock=MediaClock(), require_mention=True, mention_names=("AI Avatar",)
        )
        bridge = await self._bridge(meet_config, frame_ctx, driver)
        bridge.attach_transcript(transcript)
        bridge.attach_chat(chat)
        try:
            await bridge.start(meeting)
            await driver.page.send_chat(
                message_id="m1", text="shall we start?", sender="Backend Services"
            )
            await _wait_for(lambda: transcript.count == 1)
        finally:
            await bridge.stop()

        assert chat.received == 0, "nobody addressed the avatar, so it must not answer"
        assert transcript.snapshot().lines[0].text == "shall we start?"

    async def test_a_message_the_page_could_not_name_is_named_by_the_roster(
        self, meet_config, meeting, frame_ctx
    ) -> None:
        """The live failure end to end: the page reads no sender, and the ledger still records
        who typed it, because the roster arrived first and named the only other participant."""
        from tests.fakes.meet_page import joined_driver

        driver = joined_driver(auto_page=True)
        transcript = MeetTranscript(self_names=("AI Avatar",))
        bridge = await self._bridge(meet_config, frame_ctx, driver)
        bridge.attach_transcript(transcript)
        # The wiring the session factory does: elimination is only possible for a ledger that
        # has been told who is in the room.
        bridge.add_roster_listener(transcript.observe_roster)
        try:
            await bridge.start(meeting)
            await driver.page.send_participants(["Backend Services"], self_name="AI Avatar")
            await _wait_for(lambda: bridge.roster.count == 1)
            await driver.page.send_chat(text="who else is here?", sender=None)
            await _wait_for(lambda: transcript.count == 1)
        finally:
            await bridge.stop()

        line = transcript.snapshot().lines[0]
        assert line.label == "Backend Services"
        assert line.inferred is True


class TestMuteStateReachesThePage:
    """The one language-independent naming signal, read from a label already being parsed."""

    def test_the_roster_scan_reports_mute_state(self, bridge_code: str) -> None:
        """Read from the *whole* label rather than the first line: Meet renders audio state as part
        of the tile's status text, and the name is only the first line of it."""
        scan = bridge_code.split("function scanRoster", 1)[1].split("\n  function ", 1)[0]
        assert "muted" in scan
        assert "participants.push({ id, name: name.slice(0, 120), isSelf, muted })" in scan

    def test_an_unknown_mute_state_is_null_rather_than_false(self, bridge_code: str) -> None:
        """"The label said nothing" and "the label said they are unmuted" are different answers,
        and only one of them may remove somebody from the field of candidates."""
        scan = bridge_code.split("function scanRoster", 1)[1].split("\n  function ", 1)[0]
        assert "let muted = null;" in scan
