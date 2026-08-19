"""The Zoom-web connector's meeting awareness: who, what, and who wants the floor.

**These tests exist because the failures they encode are all invisible.** Every one of
them produces a session that joins, publishes, reports healthy and answers questions
wrongly — an avatar that talks over people, credits a question to the wrong person, or
says it has no idea who is in a meeting whose roster it holds. Nothing crashes, so
nothing else would catch them.

Four properties carry the weight:

1. **A voice interrupts only while the avatar is speaking.** Both halves matter: not
   interrupting is the complaint the feature exists to answer, and interrupting when the
   avatar is silent would send the agent a "stop talking" message on every sentence
   anybody utters.
2. **The avatar is never counted as a participant, a speaker, or an interrupter.** It is
   in its own roster and it is an active speaker whenever it talks, so each of those is a
   real path to the avatar talking about — or interrupting — itself.
3. **Chat reaches the transcript before the mention filter.** A meeting held largely in
   chat must not leave the avatar describing only the messages aimed at it.
4. **A refused text subscription degrades to audio rather than ending the connection.**
   The alternative is an avatar that goes deaf in a meeting it has already joined.
"""

from __future__ import annotations

import asyncio

import pytest

from src.connectors.zoom.exceptions import RtmsHandshakeError
from src.connectors.zoom.rtms.enums import MediaDataType, RtmsEventType, RtmsMessageType
from src.connectors.zoom.rtms.mapping import to_participant_events, to_speaker_event
from src.connectors.zoom.rtms.models import EventUpdate
from src.connectors.zoom.rtms.observations import (
    ParticipantEvent,
    SpeakerEvent,
    TranscriptLine,
)
from src.connectors.zoom.rtms.service import RtmsService
from src.connectors.zoom_web.meeting.active_speaker import ANONYMOUS, ZoomSpeakerTracker
from src.connectors.zoom_web.meeting.attendance import ZoomAttendanceLedger
from src.connectors.zoom_web.meeting.chat import ZoomChatSource, strip_mention
from src.connectors.zoom_web.meeting.hand_raise import ZoomInterruptSource
from src.connectors.zoom_web.meeting.observer import ZoomMeetingObserver
from src.connectors.zoom_web.meeting.transcript import ZoomTranscript
from src.connectors.zoom_web.page.protocol import decode_event
from src.domain.context import FrameContext
from src.domain.ids import CorrelationId, SessionId
from src.domain.media import AudioFrame
from src.domain.meeting import ChatMessage
from src.services.media.clock import MediaClock
from src.services.media.queues import BoundedFrameQueue
from tests.fakes.rtms import FakeTransportFactory

SELF = "AI Avatar"
HUMAN = "Priya Menon"


def _ctx() -> FrameContext:
    return FrameContext(
        session_id=SessionId("ses_zoomaware0000000000000000000"),
        correlation_id=CorrelationId("cor_zoomaware0000000000000000000"),
    )


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #


def test_ledger_records_a_join_and_a_departure() -> None:
    ledger = ZoomAttendanceLedger(self_names=(SELF,))
    ledger.observe(ParticipantEvent(user_id=7, display_name=HUMAN, joined=True, at_us=1_000))

    snapshot = ledger.snapshot()
    assert [r.label for r in snapshot.present] == [HUMAN]
    assert HUMAN in snapshot.agent_context()

    ledger.observe(
        ParticipantEvent(user_id=7, display_name=HUMAN, joined=False, at_us=3_000_000)
    )
    snapshot = ledger.snapshot()
    assert not snapshot.present
    assert [r.label for r in snapshot.departed] == [HUMAN]
    # Exact at both ends, unlike a scan-based ledger's lower bound: Zoom told us both
    # moments, so the figure is the real one.
    assert snapshot.records[0].duration_us() == 2_999_000


def test_the_avatar_is_never_counted_as_an_attendee() -> None:
    """Zoom reports the avatar joining like anybody else.

    Counting it makes every answer wrong by one — "there are 2 people here" in a
    one-to-one interview — which reads as a bug in the model rather than in the bridge.
    """
    ledger = ZoomAttendanceLedger(self_names=(SELF,))
    ledger.observe(ParticipantEvent(user_id=1, display_name=SELF, joined=True))
    ledger.observe(ParticipantEvent(user_id=7, display_name=HUMAN, joined=True))

    snapshot = ledger.snapshot()
    assert [r.label for r in snapshot.present] == [HUMAN]


def test_a_rejoin_is_one_person_rather_than_two() -> None:
    """Zoom mints a fresh ``user_id`` when somebody reconnects.

    Keying on it would report one person whose wifi dropped as two attendees — which is
    why the ledger keys on the folded name instead.
    """
    ledger = ZoomAttendanceLedger(self_names=(SELF,))
    ledger.observe(ParticipantEvent(user_id=7, display_name=HUMAN, joined=True))
    ledger.observe(ParticipantEvent(user_id=7, display_name=HUMAN, joined=False))
    ledger.observe(ParticipantEvent(user_id=99, display_name=HUMAN, joined=True))

    snapshot = ledger.snapshot()
    assert len(snapshot.records) == 1
    assert snapshot.records[0].rejoins == 1
    assert snapshot.records[0].present


def test_a_departure_for_somebody_never_seen_joining_is_not_recorded() -> None:
    """RTMS attaching mid-meeting means joins that happened before we were listening.

    Recording a departure for a person with no arrival would put somebody in "was here
    and left" who was never observed at all.
    """
    ledger = ZoomAttendanceLedger(self_names=(SELF,))
    ledger.observe(ParticipantEvent(user_id=7, display_name="Ghost", joined=False))
    assert ledger.snapshot().records == ()


def test_invitees_who_never_joined_are_answerable() -> None:
    ledger = ZoomAttendanceLedger(self_names=(SELF,))
    assert ledger.seed_invitees((HUMAN, "Aarav Sharma")) == 2
    ledger.observe(ParticipantEvent(user_id=7, display_name=HUMAN, joined=True))

    snapshot = ledger.snapshot()
    assert [r.label for r in snapshot.never_joined] == ["Aarav Sharma"]
    assert "Aarav Sharma" in snapshot.agent_context()


def test_attendance_with_no_events_says_it_does_not_know() -> None:
    """The difference between "nobody is here" and "we have not been told" is the whole
    reason ``scans`` is on the snapshot."""
    snapshot = ZoomAttendanceLedger().snapshot()
    assert snapshot.scans == 0
    assert "not known yet" in snapshot.agent_context()


# --------------------------------------------------------------------------- #
# Who is speaking
# --------------------------------------------------------------------------- #


def test_the_floor_moving_closes_the_previous_turn() -> None:
    """Zoom sends no "stopped", so closing the open turn is this class's job.

    Without it every speaker in a meeting is reported as still talking, and
    ``current_speaker`` names whoever spoke first for the rest of the call.
    """
    clock = MediaClock()
    tracker = ZoomSpeakerTracker(clock=clock, self_names=(SELF,), merge_gap_ms=0)

    tracker.observe(SpeakerEvent(user_id=7, display_name=HUMAN))
    assert tracker.current_speaker() == HUMAN

    tracker.observe(SpeakerEvent(user_id=8, display_name="Aarav Sharma"))
    assert tracker.current_speaker() == "Aarav Sharma"

    snapshot = tracker.snapshot()
    assert [t.label for t in snapshot.turns] == [HUMAN, "Aarav Sharma"]
    assert not snapshot.turns[0].is_open
    assert snapshot.turns[1].is_open


def test_the_same_speaker_repeated_is_one_turn() -> None:
    """Zoom re-reports the active speaker through a conversation.

    Treating each as a new turn makes one person talking read as a dozen speakers, and
    "who has been speaking" answers with the same name over and over.
    """
    tracker = ZoomSpeakerTracker(clock=MediaClock(), self_names=(SELF,))
    for _ in range(5):
        tracker.observe(SpeakerEvent(user_id=7, display_name=HUMAN))
    assert len(tracker.snapshot().turns) == 1


def test_the_avatar_is_never_the_current_speaker() -> None:
    """The avatar is an active speaker whenever it talks.

    Counting it would have the avatar report *itself* as the person speaking for as long
    as it speaks — and then brief the agent to yield the floor to itself.
    """
    tracker = ZoomSpeakerTracker(clock=MediaClock(), self_names=(SELF,))
    tracker.observe(SpeakerEvent(user_id=1, display_name=SELF))

    assert tracker.current_speaker() is None
    assert tracker.snapshot().events == 0
    assert tracker.ignored == 1


def test_a_speaker_known_only_by_id_is_named_retroactively() -> None:
    """A name arriving later must repair the turn it belongs to.

    Otherwise whoever spoke in the first seconds of a call is "an unidentified
    participant" in the history for the rest of the meeting, when we now know exactly who
    they were.
    """
    tracker = ZoomSpeakerTracker(clock=MediaClock(), self_names=(SELF,))
    tracker.observe(SpeakerEvent(user_id=7, display_name=None))
    assert tracker.snapshot().turns[0].display_name is None

    tracker.observe_name(7, HUMAN)
    assert tracker.snapshot().turns[0].display_name == HUMAN


def test_the_hold_window_keeps_a_speaker_across_a_pause() -> None:
    """Speech has gaps at every clause boundary.

    Without the hold, a barge-in landing between two words is attributed to nobody.
    """
    clock = MediaClock()
    tracker = ZoomSpeakerTracker(clock=clock, hold_ms=60_000, self_names=(SELF,))
    tracker.observe(SpeakerEvent(user_id=7, display_name=HUMAN))
    tracker.release()

    assert tracker.current_speaker() == HUMAN


def test_the_brief_names_the_candidates_when_the_voice_is_unattributed() -> None:
    """Saying "unknown" invites the model to resolve it from whatever else is in frame,
    and what is in frame is a chat history with one name in it."""
    tracker = ZoomSpeakerTracker(clock=MediaClock(), self_names=(SELF,), merge_gap_ms=0)
    tracker.observe_participants((HUMAN, "Aarav Sharma"))
    tracker.observe(SpeakerEvent(user_id=None, display_name=None))

    snapshot = tracker.snapshot()
    assert snapshot.turns[0].label == ANONYMOUS
    brief = snapshot.agent_context()
    assert HUMAN in brief and "Aarav Sharma" in brief
    assert "typed in the chat" in brief


def test_elimination_names_the_only_other_participant() -> None:
    """The case that matters most is two people: the avatar and a candidate."""
    tracker = ZoomSpeakerTracker(clock=MediaClock(), self_names=(SELF,))
    tracker.observe_participants((HUMAN,))
    tracker.observe(SpeakerEvent(user_id=7, display_name=None))

    assert tracker.current_speaker() == HUMAN
    assert tracker.snapshot().turns[0].inferred is True


# --------------------------------------------------------------------------- #
# Interruption
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_voice_interrupts_only_while_the_avatar_is_speaking() -> None:
    """**The property the whole feature is about, in both directions.**

    Not interrupting is the complaint: an avatar that talks until it finishes whatever
    anybody says over it. Interrupting when it is silent is the other failure — the agent
    would be sent "stop talking and let them speak" on every sentence anybody utters, and
    an avatar that answers each of those with "go ahead" is worse than one that never
    yields.
    """
    speaking = False
    source = ZoomInterruptSource(
        clock=MediaClock(),
        cooldown_s=0,
        self_names=(SELF,),
        is_avatar_speaking=lambda: speaking,
    )

    assert source.offer_voice(SpeakerEvent(user_id=7, display_name=HUMAN)) is False

    speaking = True
    assert source.offer_voice(SpeakerEvent(user_id=7, display_name=HUMAN)) is True

    event = await asyncio.wait_for(anext(aiter(source.events())), timeout=1.0)
    assert event.participant == HUMAN
    assert HUMAN in event.prompt
    assert source.voices == 1


def test_the_avatar_never_interrupts_itself() -> None:
    """The avatar is an active speaker whenever it talks — which is precisely when the
    "is the avatar speaking" gate is open. Without the self check it would interrupt
    itself continuously, for as long as it spoke."""
    source = ZoomInterruptSource(
        clock=MediaClock(), cooldown_s=0, self_names=(SELF,), is_avatar_speaking=lambda: True
    )
    assert source.offer_voice(SpeakerEvent(user_id=1, display_name=SELF)) is False
    assert source.received == 0


def test_a_raised_hand_interrupts_a_silent_avatar() -> None:
    """Unlike a voice, and deliberately: a hand means "notice me" whether or not anybody
    is talking, and a silent avatar still needs to be told to say "go ahead"."""
    source = ZoomInterruptSource(
        clock=MediaClock(), cooldown_s=0, self_names=(SELF,), is_avatar_speaking=lambda: False
    )
    assert source.offer_hand({"id": "name:priya menon", "name": HUMAN}) is True
    assert source.hands == 1


def test_the_same_participant_is_rate_limited() -> None:
    """Both inputs repeat — the page re-reads an unmoved hand, and Zoom re-reports the
    same active speaker. An avatar interrupted continuously never gets as far as saying
    "go ahead"."""
    source = ZoomInterruptSource(
        clock=MediaClock(), cooldown_s=60, self_names=(SELF,), is_avatar_speaking=lambda: True
    )
    assert source.offer_hand({"id": "h", "name": HUMAN}) is True
    assert source.offer_hand({"id": "h", "name": HUMAN}) is False
    assert source.ignored == 1


def test_voice_interruption_can_be_switched_off_without_disabling_hands() -> None:
    source = ZoomInterruptSource(
        clock=MediaClock(),
        cooldown_s=0,
        self_names=(SELF,),
        voice_enabled=False,
        is_avatar_speaking=lambda: True,
    )
    assert source.offer_voice(SpeakerEvent(user_id=7, display_name=HUMAN)) is False
    assert source.offer_hand({"id": "h", "name": HUMAN}) is True


# --------------------------------------------------------------------------- #
# Chat
# --------------------------------------------------------------------------- #


def test_only_tagged_messages_are_forwarded() -> None:
    chat = ZoomChatSource(mention_names=(SELF,))
    assert chat.offer(ChatMessage(text="sounds good, thanks!", sender=HUMAN)) is False
    assert chat.offer(ChatMessage(text=f"@{SELF} what is the notice period?", sender=HUMAN))
    assert chat.ignored == 1


def test_the_mention_is_stripped_before_the_agent_sees_it() -> None:
    """What survives becomes an LLM prompt, and leaving the vocative in invites the agent
    to answer a question about its own name."""
    assert strip_mention("@AI Avatar, what is the CTC?", (SELF,)) == "what is the CTC?"
    assert strip_mention("@ai_avatar are you there?", (SELF,)) == "are you there?"
    # A message that is only a mention keeps its text: forwarding an empty string would
    # have the agent drop somebody getting its attention.
    assert strip_mention("@AI Avatar", (SELF,)) == "@AI Avatar"
    # Whole words only, so a name that starts with the avatar's does not trigger it.
    assert strip_mention("@Aisha can you help?", ("AI",)) is None


@pytest.mark.asyncio
async def test_the_avatar_recognises_its_own_chat_message_by_name() -> None:
    """Zoom labels a message with the sender's display name and nothing that says "this
    is you", so ``is_self`` arrives False on the avatar's own line — and an avatar that
    answers its own message is the text-channel version of the echo loop.

    Queued rather than dropped here, because the transcript wants it: the final filter is
    ``AvatarClient.send_chat``, which is the one place the decision belongs.
    """
    chat = ZoomChatSource(mention_names=(SELF,), require_mention=False)
    assert chat.offer(ChatMessage(text="hello everyone", sender=SELF)) is True

    message = await asyncio.wait_for(anext(aiter(chat.messages())), timeout=1.0)
    assert message.is_self is True


def test_a_nameless_message_is_named_by_elimination() -> None:
    chat = ZoomChatSource(mention_names=(SELF,), require_mention=False)
    chat.observe_participants((HUMAN,))
    assert chat.offer(ChatMessage(text="what is the CTC?", sender=None)) is True


# --------------------------------------------------------------------------- #
# Transcript
# --------------------------------------------------------------------------- #


def test_the_transcript_attributes_spoken_and_typed_lines_separately() -> None:
    """Typing is not speaking. Flattening the two would have the agent claim it heard
    somebody who never opened their microphone."""
    transcript = ZoomTranscript(self_names=(SELF,))
    transcript.offer(TranscriptLine(user_id=7, display_name=HUMAN, text="tell me about Delhi"))
    transcript.offer_chat(ChatMessage(text="and about Mumbai", sender=HUMAN))

    snapshot = transcript.snapshot()
    assert [line.in_chat for line in snapshot.lines] == [False, True]
    brief = snapshot.agent_context()
    assert f"{HUMAN}: tell me about Delhi" in brief
    assert f"{HUMAN} (in chat): and about Mumbai" in brief
    assert "Zoom's own live transcription" in brief


def test_the_avatars_own_turn_is_marked_rather_than_credited_to_a_participant() -> None:
    """The brief's reader *is* the avatar: "AI Avatar asked about Delhi" reads as a third
    party whose question is owed an answer, when it was the avatar's own sentence."""
    transcript = ZoomTranscript(self_names=(SELF,))
    transcript.offer(TranscriptLine(user_id=1, display_name=SELF, text="hello, how are you?"))

    line = transcript.snapshot().lines[0]
    assert line.is_self is True
    assert line.label == "The avatar (you)"


def test_a_repeated_line_is_recorded_once() -> None:
    transcript = ZoomTranscript(self_names=(SELF,))
    line = TranscriptLine(user_id=7, display_name=HUMAN, text="tell me about Delhi")
    assert transcript.offer(line) is True
    assert transcript.offer(line) is False
    assert transcript.count == 1


# --------------------------------------------------------------------------- #
# The observer's wiring
# --------------------------------------------------------------------------- #


def test_chat_reaches_the_transcript_even_when_it_is_not_addressed_to_the_avatar() -> None:
    """**The ordering this observer exists to make explicit.**

    The chat source drops everything not tagged, which is the right policy for deciding
    what to *answer* and the wrong one for deciding what to *remember*: a meeting held
    largely in chat would otherwise leave the avatar, asked what was discussed, describing
    only the messages aimed at it.
    """
    transcript = ZoomTranscript(self_names=(SELF,))
    chat = ZoomChatSource(mention_names=(SELF,))
    observer = ZoomMeetingObserver(transcript=transcript, chat=chat)

    observer.on_chat(ChatMessage(text="shall we start at 4?", sender=HUMAN))

    assert transcript.count == 1
    assert chat.received == 0


def test_a_join_updates_the_candidate_set_everything_eliminates_against() -> None:
    """The ledger is the single account of who is present; the tracker, transcript and
    chat source all read it rather than keeping their own."""
    ledger = ZoomAttendanceLedger(self_names=(SELF,))
    tracker = ZoomSpeakerTracker(clock=MediaClock(), self_names=(SELF,))
    observer = ZoomMeetingObserver(attendance=ledger, speakers=tracker)

    observer.on_participant(ParticipantEvent(user_id=7, display_name=HUMAN, joined=True))
    observer.on_speaker(SpeakerEvent(user_id=None, display_name=None))

    assert tracker.current_speaker() == HUMAN


@pytest.mark.asyncio
async def test_an_unattributed_hand_is_named_from_the_rtms_roster() -> None:
    """**The page usually cannot read the name off the tile holding the hand.**

    Zoom renders the indicator on the participant's *video* tile, and a tile showing video
    carries an image where a camera-off tile carries the name — so the person whose hand is
    up is exactly the person whose name is not written down. Observed in a live run: the
    row that gained ``lazy-icon-nvf/270b`` was the only row with no name element.

    Elimination against Zoom's own roster closes it, which is stronger than the Meet
    equivalent because the roster is an API fact rather than a DOM reading.
    """
    ledger = ZoomAttendanceLedger(self_names=(SELF,))
    ledger.observe(ParticipantEvent(user_id=7, display_name=HUMAN, joined=True))
    source = ZoomInterruptSource(
        clock=MediaClock(), cooldown_s=0, self_names=(SELF,), is_avatar_speaking=lambda: False
    )
    observer = ZoomMeetingObserver(attendance=ledger, interrupts=source)

    observer.on_page_event({"type": "handRaise", "id": "anonymous", "name": None})

    assert source.hands == 1
    event = await asyncio.wait_for(anext(aiter(source.events())), timeout=1.0)
    assert event.participant == HUMAN


def test_an_unattributed_hand_stays_anonymous_when_it_could_be_either() -> None:
    """Fails closed at two or more: a confidently wrong name is worse than "Someone"."""
    ledger = ZoomAttendanceLedger(self_names=(SELF,))
    ledger.observe(ParticipantEvent(user_id=7, display_name=HUMAN, joined=True))
    ledger.observe(ParticipantEvent(user_id=8, display_name="Aarav Sharma", joined=True))
    source = ZoomInterruptSource(
        clock=MediaClock(), cooldown_s=0, self_names=(SELF,), is_avatar_speaking=lambda: False
    )
    observer = ZoomMeetingObserver(attendance=ledger, interrupts=source)

    observer.on_page_event({"type": "handRaise", "id": "anonymous", "name": None})
    assert source.hands == 1  # still interrupts — it just cannot say who


def test_a_page_hand_raise_event_reaches_the_interrupt_source() -> None:
    source = ZoomInterruptSource(
        clock=MediaClock(), cooldown_s=0, self_names=(SELF,), is_avatar_speaking=lambda: False
    )
    observer = ZoomMeetingObserver(interrupts=source)

    observer.on_page_event({"type": "handRaise", "id": "h", "name": HUMAN, "isSelf": False})
    assert source.hands == 1


def test_an_observer_that_raises_cannot_reach_the_rtms_pump() -> None:
    """A bookkeeping listener must never be able to drop the meeting's audio."""

    class Exploding:
        def observe(self, event: object) -> None:
            raise RuntimeError("boom")

    observer = ZoomMeetingObserver(attendance=Exploding())  # type: ignore[arg-type]
    observer.on_participant(ParticipantEvent(user_id=7, display_name=HUMAN, joined=True))


# --------------------------------------------------------------------------- #
# The page event codec
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "payload",
    [b"", b"not json", b"[]", b'{"no":"type"}', b"x" * (64 * 1024 + 1)],
)
def test_an_unusable_page_frame_is_dropped_rather_than_raising(payload: bytes) -> None:
    """The peer is a browser running a script against a page we do not control, so a bad
    frame is a fact about the page rather than an error condition here."""
    assert decode_event(payload) is None


def test_a_hand_raise_frame_decodes() -> None:
    assert decode_event('{"type":"handRaise","name":"Priya Menon"}') == {
        "type": "handRaise",
        "name": HUMAN,
    }


# --------------------------------------------------------------------------- #
# The RTMS subscription
# --------------------------------------------------------------------------- #


def _service(
    factory: FakeTransportFactory, observer: object | None = None, **kwargs: object
) -> RtmsService:
    return RtmsService(
        meeting_uuid="uuid",
        rtms_stream_id="stream",
        signature="sig",
        ctx=_ctx(),
        clock=MediaClock(),
        queue=BoundedFrameQueue[AudioFrame](name="t", maxsize=8),
        transport_factory=factory,
        observer=observer,  # type: ignore[arg-type]
        **kwargs,  # type: ignore[arg-type]
    )


def _handshakes(transport) -> list[dict]:
    return [
        m for m in transport.sent if m["msg_type"] == RtmsMessageType.DATA_HAND_SHAKE_REQ
    ]


@pytest.mark.asyncio
async def test_the_audio_handshake_never_carries_an_optional_subscription() -> None:
    """**The regression this asserts took a live meeting down, so it is worth naming.**

    ``media_type`` is validated by Zoom as a *single enum member*, not as a bitmask.
    Asking one socket for ``AUDIO|TRANSCRIPT|CHAT`` (25) was rejected with status 14,
    "Media type invalid value" — and a rejected data handshake ends the connection carrying
    the meeting's audio. The avatar went deaf in a meeting it had already joined, and the
    session died.

    So the audio handshake is audio and nothing else, whatever else is switched on. The
    optional streams take connections of their own.
    """
    for kwargs in ({}, {"subscribe_transcript": True, "subscribe_chat": True}):
        factory = FakeTransportFactory()
        await _service(factory, **kwargs).attach("wss://signal")

        audio = _handshakes(factory.media)
        # Exactly one handshake on the audio socket, ever. A second would mean something
        # retried on a connection Zoom had already stopped serving — which is how the
        # first attempt at this hung for ninety seconds before failing.
        assert len(audio) == 1
        assert audio[0]["media_type"] == int(MediaDataType.AUDIO)
        assert "transcript" not in audio[0]["media_params"]
        assert "chat" not in audio[0]["media_params"]


@pytest.mark.asyncio
async def test_each_text_stream_takes_a_connection_of_its_own() -> None:
    """One media type per connection, which is what Zoom's per-type ``server_urls`` says."""
    factory = FakeTransportFactory()
    await _service(factory, subscribe_transcript=True, subscribe_chat=True).attach(
        "wss://signal"
    )

    media_transports = [t for t in factory.created if t.role == "media"]
    assert len(media_transports) == 3  # audio, transcript, chat

    requested = [_handshakes(t)[0]["media_type"] for t in media_transports]
    assert requested == [
        int(MediaDataType.AUDIO),
        int(MediaDataType.TRANSCRIPT),
        int(MediaDataType.CHAT),
    ]


@pytest.mark.asyncio
async def test_a_refused_text_subscription_leaves_the_audio_leg_untouched() -> None:
    """**The property the separate socket exists for.**

    An account without RTMS transcription enabled refuses that handshake. Because it is
    its own connection, the refusal cannot reach the one carrying audio — where previously
    it ended it. Chat still attaches, and the reason for the loss is recorded rather than
    raised.
    """

    class RefuseTranscript(FakeTransportFactory):
        async def __call__(self, url: str):  # type: ignore[override]
            transport = await super().__call__(url)
            original = transport.send_json

            async def send_json(payload):  # type: ignore[no-untyped-def]
                if payload.get("msg_type") == RtmsMessageType.DATA_HAND_SHAKE_REQ and payload[
                    "media_type"
                ] == int(MediaDataType.TRANSCRIPT):
                    transport.sent.append(payload)
                    transport.push(
                        {
                            "msg_type": int(RtmsMessageType.DATA_HAND_SHAKE_RESP),
                            "status_code": 14,
                            "reason": "Media type invalid value",
                        }
                    )
                    return
                await original(payload)

            transport.send_json = send_json  # type: ignore[method-assign]
            return transport

    factory = RefuseTranscript()
    service = _service(factory, subscribe_transcript=True, subscribe_chat=True)
    await service.attach("wss://signal")

    assert service.is_attached
    # The audio socket was never spoken to a second time — no retry, nothing to hang on.
    assert len(_handshakes(factory.media)) == 1
    assert service.text_degraded is not None
    assert "MC_ZOOM_WEB__RTMS_TRANSCRIPT_ENABLED" in service.text_degraded
    # Independent: one stream refusing says nothing about the other.
    assert "MC_ZOOM_WEB__RTMS_CHAT_ENABLED" not in service.text_degraded


@pytest.mark.asyncio
async def test_a_text_socket_that_dies_does_not_stop_the_meeting_audio() -> None:
    """These share a task group with the audio pump, so an exception escaping the text
    pump would cancel it — putting the optional streams back in a position to take the
    meeting down, by a different route than the shared handshake did."""
    factory = FakeTransportFactory()
    service = _service(factory, subscribe_transcript=True)
    await service.attach("wss://signal")

    transcript_socket = [t for t in factory.created if t.role == "media"][1]
    task = asyncio.create_task(service.run())
    await asyncio.sleep(0.02)
    transcript_socket.disconnect()
    await asyncio.sleep(0.02)

    assert not task.done()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_rejected_audio_handshake_still_fails_the_attach() -> None:
    """The fallback must not turn a genuinely broken subscription into a silent success."""
    factory = FakeTransportFactory(
        responses={
            int(RtmsMessageType.DATA_HAND_SHAKE_REQ): {
                "msg_type": int(RtmsMessageType.DATA_HAND_SHAKE_RESP),
                "status_code": 13,
                "reason": "bad signature",
            }
        }
    )
    with pytest.raises(RtmsHandshakeError):
        await _service(factory, subscribe_transcript=True).attach("wss://signal")


@pytest.mark.asyncio
async def test_transcript_chat_and_events_reach_the_observer() -> None:
    """The whole path, from a wire message to the thing that remembers it."""
    ledger = ZoomAttendanceLedger(self_names=(SELF,))
    tracker = ZoomSpeakerTracker(clock=MediaClock(), self_names=(SELF,))
    transcript = ZoomTranscript(self_names=(SELF,))
    chat = ZoomChatSource(mention_names=(SELF,), require_mention=False)
    observer = ZoomMeetingObserver(
        attendance=ledger, speakers=tracker, transcript=transcript, chat=chat
    )

    factory = FakeTransportFactory()
    service = _service(
        factory,
        observer=observer,
        subscribe_transcript=True,
        subscribe_chat=True,
        subscribe_events=True,
    )
    await service.attach("wss://signal")

    factory.signaling.push_event(
        int(RtmsEventType.PARTICIPANT_JOIN), user_id=7, user_name=HUMAN
    )
    factory.signaling.push_event(
        int(RtmsEventType.ACTIVE_SPEAKER_CHANGE), user_id=7, user_name=HUMAN
    )
    factory.media.push_transcript("tell me about Delhi", user_id=7, user_name=HUMAN)
    factory.media.push_chat("and about Mumbai", user_id=7, user_name=HUMAN)

    task = asyncio.create_task(service.run())
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert [r.label for r in ledger.snapshot().present] == [HUMAN]
    assert tracker.current_speaker() == HUMAN
    assert [line.text for line in transcript.snapshot().lines] == [
        "tell me about Delhi",
        "and about Mumbai",
    ]
    assert chat.received == 1


# --------------------------------------------------------------------------- #
# The session's wiring
# --------------------------------------------------------------------------- #


def test_self_detection_follows_the_name_the_browser_actually_joined_with() -> None:
    """**The name is the session's, not the connector setting's — and reading only the
    setting was a bug.**

    ``ZoomWebJoiner`` types ``session.meeting.display_name`` into Zoom's form, and
    ``MeetingService`` fills that from the ``POST /sessions`` request, falling back to
    ``MC_ZOOM__DISPLAY_NAME``. ``MC_ZOOM_WEB__DISPLAY_NAME`` is a *different* setting that
    agrees with it only while all three defaults are untouched.

    Getting it wrong is silent and expensive, because five things key on it: the avatar
    would count itself as an attendee, report itself as the current speaker, feed its own
    sentences back as a participant's, answer its own chat, and interrupt itself
    continuously — since it is an active speaker precisely when the barge-in gate is open.
    """
    built = _built_session(display_name="Configured Name", joined_as="Interview Avatar")

    assert built.attendance is not None
    assert built.speakers is not None
    # Both are recognised: they are usually the same string, and an extra name can only
    # make self-detection more likely to fire.
    assert set(built.speakers.self_names) == {"Interview Avatar", "Configured Name"}

    built.speakers.observe(SpeakerEvent(user_id=1, display_name="Interview Avatar"))
    assert built.speakers.current_speaker() is None

    built.attendance.observe(
        ParticipantEvent(user_id=1, display_name="Interview Avatar", joined=True)
    )
    assert built.attendance.snapshot().present == ()


def _built_session(*, joined_as: str = SELF, **overrides: object):
    """A fully wired session from the real factory, with the browser and legs faked.

    Built through ``ZoomWebSessionFactory`` rather than by hand precisely because the
    wiring *is* what these tests are about: the pieces below all work in isolation, and
    every failure this section guards against is one of them not being connected — a
    handler never registered, a source built before the observer that feeds it, an
    interrupt source the router was never given.
    """
    from src.config.settings import Settings
    from src.connectors.zoom_web.config import ZoomWebConnectorConfig
    from src.connectors.zoom_web.session.zoom_web_session import ZoomWebSessionFactory
    from src.domain.meeting import MeetingContext, MeetingPlatform
    from src.domain.session import SessionContext

    settings = Settings(_env_file=None)
    config = ZoomWebConnectorConfig.from_settings(settings)
    for key, value in overrides.items():
        object.__setattr__(config, key, value)

    session = SessionContext(
        session_id=SessionId("ses_zoomweb000000000000000000000"),
        correlation_id=CorrelationId("cor_zoomweb000000000000000000000"),
        meeting=MeetingContext(
            meeting_number="1", display_name=joined_as, platform=MeetingPlatform.ZOOM_WEB
        ),
    )
    return ZoomWebSessionFactory(config=config).build(session)


def test_the_factory_wires_every_feature_by_default() -> None:
    built = _built_session()
    assert built.attendance is not None
    assert built.speakers is not None
    assert built.transcript is not None


def test_a_disabled_feature_leaves_no_surface_at_all() -> None:
    """``None`` rather than an inert object, so "switched off" and "nothing observed yet"
    stay distinguishable — which is what lets the API answer 404 with a reason instead of
    an empty ledger that reads as "nobody attended"."""
    built = _built_session(
        attendance_enabled=False, speaker_tracking_enabled=False, transcript_enabled=False
    )
    assert built.attendance is None
    assert built.speakers is None
    assert built.transcript is None


def test_a_page_hand_raise_reaches_the_session_through_the_page_server() -> None:
    """**The end of the one path that does not come from RTMS.**

    A registration that never happened is invisible: the observer runs, the page reports
    hands, and nothing interrupts. This asserts the handler is actually installed on the
    server the injected script connects back to.
    """
    from src.connectors.zoom_web.session.zoom_web_session import ZoomWebSession

    built = _built_session()
    server = built._page_server
    server._dispatch(
        '{"type":"handRaise","id":"h","name":"Priya Menon","isSelf":false}'
    )

    assert isinstance(built, ZoomWebSession)
    assert server.events_received == 1


def test_the_injected_script_carries_the_hand_raise_configuration() -> None:
    """The selectors travel as data so a Zoom UI change is a settings edit.

    Asserted because the script treats a missing key as "feature off" — a bootstrap that
    forgot to send them produces an observer that runs and matches nothing, which is
    indistinguishable from a meeting where nobody raised a hand.
    """
    built = _built_session()
    bootstrap = built._page_bootstrap()

    assert '"handRaiseEnabled": true' in bootstrap
    assert "participants-item" in bootstrap
    assert f'"displayName": "{SELF}"' in bootstrap


# --------------------------------------------------------------------------- #
# The event envelope, captured from a live meeting
# --------------------------------------------------------------------------- #

LIVE_JOIN = {
    "msg_type": 6,
    "event": {
        "event_type": 3,
        "participants": [
            {"user_id": 16789504, "user_name": SELF},
            {"user_id": 16778240, "user_name": "Dev Choudhary"},
        ],
        "timestamp": 1787132264875,
    },
}
"""A real ``PARTICIPANT_JOIN``, copied verbatim out of a live run's logs.

Two properties, and both were broken: ``event_type`` is **inside** ``event`` where the
model read the top level, and the people arrive as a **list** where the model read one
``user_id``. Together they meant every event decoded as unknown — no attendance, no
speaker, and no voice barge-in — while the meeting looked entirely healthy."""

LIVE_SPEAKER = {
    "msg_type": 6,
    "event": {
        "event_type": 2,
        "timestamp": 1787132270398,
        "user_id": 16789504,
        "user_name": SELF,
    },
}

LIVE_FIRST_PACKET = {
    "msg_type": 6,
    "event": {"event_type": 1, "media_type": 1, "timestamp": 1787132264875},
}
"""``FIRST_PACKET_TIMESTAMP`` — a real event with nobody on it. Must decode to nothing
rather than to a phantom participant."""


def test_a_live_join_seeds_the_whole_roster() -> None:
    """The first join after attaching carries everybody already in the meeting.

    RTMS attaches after the meeting has begun, so the individual joins for people already
    present were never going to arrive. Reading one ``user_id`` left the ledger
    permanently empty, and the avatar answered "I don't have access to your current
    meeting" about a roster it had been sent.
    """
    update = EventUpdate.model_validate(LIVE_JOIN)
    assert update.resolved_event_type() == int(RtmsEventType.PARTICIPANT_JOIN)

    events = to_participant_events(update, clock=MediaClock())
    assert [(e.display_name, e.joined) for e in events] == [
        (SELF, True),
        ("Dev Choudhary", True),
    ]

    ledger = ZoomAttendanceLedger(self_names=(SELF,))
    for event in events:
        ledger.observe(event)
    # The avatar is in its own roster and must not be counted.
    assert [r.label for r in ledger.snapshot().present] == ["Dev Choudhary"]


def test_a_live_active_speaker_event_decodes() -> None:
    update = EventUpdate.model_validate(LIVE_SPEAKER)
    assert update.resolved_event_type() == int(RtmsEventType.ACTIVE_SPEAKER_CHANGE)

    speaker = to_speaker_event(update, clock=MediaClock())
    assert speaker is not None
    assert speaker.display_name == SELF
    assert speaker.user_id == 16789504


def test_an_event_with_nobody_on_it_produces_nothing() -> None:
    update = EventUpdate.model_validate(LIVE_FIRST_PACKET)
    assert to_participant_events(update, clock=MediaClock()) == ()
    assert to_speaker_event(update, clock=MediaClock()) is None


@pytest.mark.asyncio
async def test_the_live_envelope_drives_barge_in_end_to_end() -> None:
    """**The whole path the user actually cares about**, from Zoom's real bytes to the
    avatar being told to stop: roster in, then somebody who is not the avatar takes the
    floor while the avatar is speaking."""
    ledger = ZoomAttendanceLedger(self_names=(SELF,))
    tracker = ZoomSpeakerTracker(clock=MediaClock(), self_names=(SELF,))
    interrupts = ZoomInterruptSource(
        clock=MediaClock(), cooldown_s=0, self_names=(SELF,), is_avatar_speaking=lambda: True
    )
    observer = ZoomMeetingObserver(
        attendance=ledger, speakers=tracker, interrupts=interrupts
    )

    factory = FakeTransportFactory()
    service = _service(factory, observer=observer, subscribe_events=True)
    await service.attach("wss://signal")

    factory.signaling.push(LIVE_JOIN)
    factory.signaling.push(LIVE_SPEAKER)  # the avatar itself — must NOT interrupt
    factory.signaling.push(
        {
            "msg_type": 6,
            "event": {"event_type": 2, "user_id": 16778240, "user_name": "Dev Choudhary"},
        }
    )

    task = asyncio.create_task(service.run())
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert [r.label for r in ledger.snapshot().present] == ["Dev Choudhary"]
    assert tracker.current_speaker() == "Dev Choudhary"
    assert interrupts.voices == 1  # exactly one — the avatar's own turn was ignored

    event = await asyncio.wait_for(anext(aiter(interrupts.events())), timeout=1.0)
    assert event.participant == "Dev Choudhary"


@pytest.mark.asyncio
async def test_each_socket_answers_its_own_keepalive() -> None:
    """**Why the chat stream died 65 seconds in.**

    ``_handle_media`` answered every keep-alive on the audio socket whatever connection
    had asked, so the text sockets never answered theirs — and RTMS hangs up on a
    connection that goes unanswered for about a minute. Both streams attached, closed
    just after, and a chat message typed later was never delivered: the ``@mention``
    appeared to do nothing at all.
    """
    factory = FakeTransportFactory()
    service = _service(factory, subscribe_transcript=True, subscribe_chat=True)
    await service.attach("wss://signal")

    audio, transcript, chat = (t for t in factory.created if t.role == "media")
    for transport in (audio, transcript, chat):
        transport.push_keepalive(timestamp=4242)

    task = asyncio.create_task(service.run())
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    # Each connection answered on itself — not all three on the audio one.
    for transport in (audio, transcript, chat):
        assert len(transport.keepalive_responses()) == 1, transport.url


@pytest.mark.asyncio
async def test_the_event_subscription_is_sent_only_when_events_are_wanted() -> None:
    quiet = FakeTransportFactory()
    await _service(quiet).attach("wss://signal")
    assert not [
        m for m in quiet.signaling.sent if m["msg_type"] == RtmsMessageType.EVENT_SUBSCRIPTION
    ]

    loud = FakeTransportFactory()
    await _service(loud, subscribe_events=True).attach("wss://signal")
    subscription = next(
        m for m in loud.signaling.sent if m["msg_type"] == RtmsMessageType.EVENT_SUBSCRIPTION
    )
    assert {e["event_type"] for e in subscription["events"]} == {
        int(RtmsEventType.ACTIVE_SPEAKER_CHANGE),
        int(RtmsEventType.PARTICIPANT_JOIN),
        int(RtmsEventType.PARTICIPANT_LEAVE),
    }
