"""Browser ingest: hearing and understanding a Zoom meeting without RTMS.

**What these tests are really protecting is the claim that this is a new *source* rather
than a second connector.** Every observation the page produces has to arrive at the same
ledgers, in the same types, with the same ordering as the RTMS path — because that is what
kept the announcer, the interrupt source, the API and the media pipeline untouched. So the
assertions here are mostly about equivalence at that seam, not about the DOM.

The one place the two legs are genuinely allowed to differ is the echo gate, and there is a
test for that too: it is the difference that makes energy barge-in possible here and
impossible under RTMS, so getting it backwards would silently restore the "the avatar talks
over everybody until it finishes" behaviour doc 008 §4 was written about.

Selectors are deliberately *not* tested against fixture HTML. They are configuration for a
UI Zoom is free to change, and a fixture would only assert that a snapshot of Zoom's markup
still matches itself — which is true forever and says nothing about a live meeting. What is
tested is that a stale selector degrades to silence rather than to a wrong answer.
"""

from __future__ import annotations

import struct

import pytest

from src.config.settings import Settings
from src.connectors.zoom_web.config import ZoomWebConnectorConfig
from src.connectors.zoom_web.ingest.page_audio_source import PageAudioSource
from src.connectors.zoom_web.meeting.active_speaker import ZoomSpeakerTracker
from src.connectors.zoom_web.meeting.attendance import ZoomAttendanceLedger
from src.connectors.zoom_web.meeting.chat import ZoomChatSource
from src.connectors.zoom_web.meeting.hand_raise import ZoomInterruptSource
from src.connectors.zoom_web.meeting.observer import ZoomMeetingObserver
from src.connectors.zoom_web.meeting.transcript import ZoomTranscript
from src.connectors.zoom_web.page.protocol import (
    HEADER_SIZE,
    KIND_AUDIO_CAPTURE,
    KIND_AUDIO_PCM,
    MAGIC,
    VERSION,
    decode_audio,
    decode_event,
    encode_audio,
)
from src.connectors.zoom_web.page.server import PageAudioServer
from src.domain.avatar import AVATAR_INPUT_FORMAT
from src.domain.context import FrameContext
from src.domain.health import ComponentState
from src.domain.ids import CorrelationId, SessionId
from src.services.media.clock import MediaClock

FRAME_SAMPLES = 320
"""20 ms at 16 kHz — what ``capture_worklet.js`` posts."""


def _ctx() -> FrameContext:
    return FrameContext(
        session_id=SessionId("ses_zoomweb000000000000000000000"),
        correlation_id=CorrelationId("cor_zoomweb000000000000000000000"),
    )


def _capture_frame(pcm: bytes, *, pts_us: int = 0, kind: int = KIND_AUDIO_CAPTURE) -> bytes:
    """One page→bridge audio frame, framed exactly as ``inject.js`` frames it."""
    header = struct.Struct("!4sBBHQI").pack(MAGIC, VERSION, kind, 0, pts_us, len(pcm))
    return header + pcm


def _pcm(samples: int = FRAME_SAMPLES, value: int = 0) -> bytes:
    return struct.pack(f"<{samples}h", *([value] * samples))


# --------------------------------------------------------------------------- #
# The wire
# --------------------------------------------------------------------------- #


def test_a_tapped_frame_round_trips() -> None:
    pcm = _pcm(value=1234)
    assert decode_audio(_capture_frame(pcm, pts_us=99)) == pcm


def test_the_two_audio_directions_do_not_decode_as_each_other() -> None:
    """The whole point of a separate ``kind``.

    Both directions share a header, so without the discriminator the avatar's own outbound
    voice would decode as tapped meeting audio — and be fed straight back to the agent as
    something a participant said. That is the echo loop doc 008 describes, reintroduced
    through the front door.
    """
    outbound = encode_audio(_pcm(), pts_us=5)
    assert decode_audio(outbound) is None
    assert struct.unpack_from("!4sBBHQI", outbound)[2] == KIND_AUDIO_PCM


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("truncated payload", _capture_frame(_pcm())[:-4]),
        ("header only", _capture_frame(_pcm())[:HEADER_SIZE]),
        ("shorter than a header", b"ZWB1"),
        ("a text event", b'{"type":"chat","text":"hello"}'),
        ("empty", b""),
    ],
)
def test_unusable_binary_is_dropped_rather_than_raising(name: str, data: bytes) -> None:
    """Bytes a browser produced, against a page this service does not control.

    A malformed frame is a fact about the page, not an error condition here — the same
    contract ``decode_event`` holds, and for the same reason: this runs on the loop carrying
    the avatar's voice.
    """
    assert decode_audio(data) is None, name


def test_a_tapped_frame_is_not_mistaken_for_an_event() -> None:
    assert decode_event(_capture_frame(_pcm())) is None


# --------------------------------------------------------------------------- #
# The page channel
# --------------------------------------------------------------------------- #


async def test_the_page_channel_splits_audio_from_events() -> None:
    """Binary is audio, text is an event, and neither reaches the other's handler.

    Routing on the transport's frame type rather than on a parsed discriminator is what keeps
    a JSON decode off the audio path fifty times a second.
    """
    server = PageAudioServer()
    audio: list[bytes] = []
    events: list[dict[str, object]] = []
    server.set_audio_handler(audio.append)
    server.set_event_handler(events.append)

    server._dispatch(_capture_frame(_pcm(value=7)))
    server._dispatch('{"type":"handRaise","id":"name:dev","name":"Dev"}')

    assert audio == [_pcm(value=7)]
    assert [e["type"] for e in events] == ["handRaise"]
    assert (server.audio_received, server.events_received) == (1, 1)
    assert (server.audio_dropped, server.events_dropped) == (0, 0)


async def test_a_handler_that_raises_does_not_take_the_socket_with_it() -> None:
    """This loop also carries the avatar's voice into the page.

    A bookkeeping listener having a bad day must not be able to make the avatar go mute.
    """
    server = PageAudioServer()

    def explode(_: object) -> None:
        raise RuntimeError("boom")

    server.set_audio_handler(explode)
    server.set_event_handler(explode)

    server._dispatch(_capture_frame(_pcm()))
    server._dispatch('{"type":"pageEvent","name":"handsIdle"}')

    assert server.audio_received == 1
    assert server.events_received == 1


async def test_audio_arriving_with_no_handler_is_counted_not_faulted() -> None:
    """The normal state under RTMS ingest: nothing registered, nothing to route."""
    server = PageAudioServer()
    server._dispatch(_capture_frame(_pcm()))
    assert server.audio_received == 1
    assert server.audio_dropped == 0


# --------------------------------------------------------------------------- #
# The ingest source
# --------------------------------------------------------------------------- #


def _source(server: PageAudioServer, clock: MediaClock | None = None) -> PageAudioSource:
    return PageAudioSource(
        server=server, ctx=_ctx(), clock=clock or MediaClock(), queue_size=8
    )


async def test_tapped_audio_becomes_frames_at_the_avatar_format() -> None:
    server = PageAudioServer()
    source = _source(server)
    await source.start()

    server._dispatch(_capture_frame(_pcm(value=100)))
    server._dispatch(_capture_frame(_pcm(value=200)))
    await source.stop()

    frames = [frame async for frame in source.frames()]
    assert len(frames) == 2
    assert all(frame.format == AVATAR_INPUT_FORMAT for frame in frames)
    assert all(frame.sample_count == FRAME_SAMPLES for frame in frames)
    # A mixed tap cannot attribute. Who is talking comes from the DOM, separately.
    assert all(frame.participant is None for frame in frames)


async def test_frames_are_stamped_from_our_clock_not_the_page_s() -> None:
    """The page's ``AudioContext.currentTime`` runs on the audio device's timeline.

    It has an arbitrary origin and drifts against the monotonic clock the pipeline is paced
    on, so mixing the two would corrupt the single-clock invariant A/V sync depends on. The
    header's value is carried for latency attribution and never for presentation.
    """
    server = PageAudioServer()
    source = _source(server)
    await source.start()

    absurd = 10**15
    server._dispatch(_capture_frame(_pcm(), pts_us=absurd))
    await source.stop()

    frame = await anext(source.frames())
    assert frame.pts_us < absurd


async def test_a_frame_that_is_not_whole_samples_is_dropped() -> None:
    """Script skew, not an expected path — the worklet only ever posts whole frames.

    Dropped rather than raised: ``AudioFrame`` would reject it, and this runs on the page
    channel's read loop where raising drops the socket.
    """
    server = PageAudioServer()
    source = _source(server)
    await source.start()

    server._dispatch(_capture_frame(b"\x00" * 641))
    server._dispatch(_capture_frame(_pcm()))
    await source.stop()

    assert len([f async for f in source.frames()]) == 1


async def test_silence_and_a_broken_tap_are_reported_differently_from_healthy() -> None:
    """The one distinction this health report exists to make.

    A meeting where nobody has spoken and a tap that never found Zoom's audio graph both
    produce zero frames. They are indistinguishable from here, so the report states the fact
    and refuses to editorialise — degraded, with the count in the detail.
    """
    server = PageAudioServer()
    source = _source(server)
    assert source.health().state is ComponentState.UNKNOWN

    await source.start()
    assert source.health().state is ComponentState.DEGRADED
    assert "tapped=0" in (source.health().detail or "")

    server._dispatch(_capture_frame(_pcm()))
    assert source.health().state is ComponentState.HEALTHY


async def test_stopping_ingest_does_not_stop_the_avatar_speaking() -> None:
    """The page channel is shared with the publisher.

    Stopping the server from the ingest leg would close the avatar's voice as a side effect
    of ingest shutting up. ``ZoomWebSession`` stops it once, for both legs.
    """
    server = PageAudioServer()
    await server.start()
    source = _source(server)
    await source.start()
    await source.stop()

    assert server._server is not None
    await server.stop()


async def test_overflow_drops_the_oldest_rather_than_blocking_the_page() -> None:
    """``put`` must never block: the producer is the socket carrying the avatar's voice."""
    server = PageAudioServer()
    source = PageAudioSource(server=server, ctx=_ctx(), clock=MediaClock(), queue_size=2)
    await source.start()

    for value in (1, 2, 3, 4):
        server._dispatch(_capture_frame(_pcm(value=value)))
    await source.stop()

    frames = [f async for f in source.frames()]
    assert len(frames) == 2
    assert struct.unpack_from("<h", frames[0].pcm)[0] == 3


# --------------------------------------------------------------------------- #
# The observer: page events as RTMS observations
# --------------------------------------------------------------------------- #


def _ledger() -> ZoomAttendanceLedger:
    return ZoomAttendanceLedger(self_names=("AI Avatar",))


def test_a_roster_level_becomes_joins_and_leaves() -> None:
    """The page reports a list; the ledger wants edges. The diff happens in Python.

    Doing it in the page would put the authoritative roster inside something that does not
    survive a reload — and a reload would then re-announce the whole meeting as having just
    joined, which the announcer pushes to the agent as news.
    """
    ledger = _ledger()
    # No grace, so this test is about the diff alone. The debounce has its own test below.
    observer = ZoomMeetingObserver(
        attendance=ledger, clock=MediaClock(), leave_grace_s=0.0
    )

    observer.on_page_event({"type": "roster", "names": ["AI Avatar", "Dev", "Priya"]})
    assert ledger.present_names == ("Dev", "Priya")

    # Twice: a departure is only acted on once it has been *seen* to persist, so with a zero
    # window the first pass records the absence and the second acts on it.
    observer.on_page_event({"type": "roster", "names": ["AI Avatar", "Dev"]})
    observer.on_page_event({"type": "roster", "names": ["AI Avatar", "Dev"]})
    assert ledger.present_names == ("Dev",)

    before = ledger.events
    observer.on_page_event({"type": "roster", "names": ["AI Avatar", "Dev"]})
    assert ledger.events == before, "an unchanged roster must produce no events"


def test_an_empty_roster_never_empties_the_meeting() -> None:
    """``add_init_script`` runs in every frame, and most of Zoom's cannot see the panel.

    The avatar is always in its own participants list, so a genuinely empty roster is not a
    state this can observe — which makes an empty report evidence of a blind frame or a
    closed panel. Believing it would let a re-render wipe the roster.
    """
    ledger = _ledger()
    observer = ZoomMeetingObserver(attendance=ledger, clock=MediaClock())
    observer.on_page_event({"type": "roster", "names": ["AI Avatar", "Dev"]})

    observer.on_page_event({"type": "roster", "names": []})
    observer.on_page_event({"type": "roster", "names": "not a list"})

    assert ledger.present_names == ("Dev",)


def test_a_rejoin_is_a_join_again_and_then_a_leave_again() -> None:
    """The observer diffs against its own last view, not against the ledger.

    The ledger keeps departed records, so diffing against it would see a rejoin as somebody
    already known and never emit the join — and then never see them leave.
    """
    ledger = _ledger()
    observer = ZoomMeetingObserver(
        attendance=ledger, clock=MediaClock(), leave_grace_s=0.0
    )

    observer.on_page_event({"type": "roster", "names": ["AI Avatar", "Dev"]})
    observer.on_page_event({"type": "roster", "names": ["AI Avatar"]})
    observer.on_page_event({"type": "roster", "names": ["AI Avatar"]})
    assert ledger.present_names == ()

    observer.on_page_event({"type": "roster", "names": ["AI Avatar", "Dev"]})
    assert ledger.present_names == ("Dev",)


def test_a_tile_vanishing_for_a_moment_is_not_somebody_leaving() -> None:
    """**The flap a live run produced**, and the reason departures are debounced.

    The roster is read off the tile grid, and Zoom re-lays that grid out constantly —
    switching between speaker and gallery view, opening a panel, somebody sharing a screen.
    The run logged ``zoom_attendance.left dev Choudhary stayed_s=142.8`` and then ``rejoined``
    forty-four seconds later, with the person never having moved: the tile count went from
    two to one and back.

    It is expensive rather than cosmetic — each flap re-pushes the meeting brief, so the agent
    is told the room emptied and refilled, and elimination briefly has nobody to name.
    """
    ledger = _ledger()
    observer = ZoomMeetingObserver(
        attendance=ledger, clock=MediaClock(), leave_grace_s=60.0
    )

    observer.on_page_event({"type": "roster", "names": ["AI Avatar", "Dev"]})
    for _ in range(5):
        observer.on_page_event({"type": "roster", "names": ["AI Avatar"]})

    assert ledger.present_names == ("Dev",), "a re-layout is not a departure"

    observer.on_page_event({"type": "roster", "names": ["AI Avatar", "Dev"]})
    assert ledger.present_names == ("Dev",)
    assert ledger.events == 1, "and nothing was reported to the agent in between"


def test_an_arrival_is_believed_immediately() -> None:
    """Deliberately asymmetric with the departure grace above.

    There is no layout in which Zoom invents a participant, so an appearance is evidence of
    its own kind. Holding arrivals as well would delay the avatar greeting somebody for no
    reason at all.
    """
    ledger = _ledger()
    observer = ZoomMeetingObserver(
        attendance=ledger, clock=MediaClock(), leave_grace_s=60.0
    )

    observer.on_page_event({"type": "roster", "names": ["AI Avatar", "Priya"]})

    assert ledger.present_names == ("Priya",)


def test_a_speaker_event_reaches_the_tracker_and_the_interrupt_source() -> None:
    """Two consumers with genuinely different jobs, and the tracker goes first.

    So that an interruption reaching the router in the same tick finds ``current_speaker``
    already naming the person who caused it.
    """
    clock = MediaClock()
    speakers = ZoomSpeakerTracker(
        clock=clock, hold_ms=0, merge_gap_ms=0, self_names=("AI Avatar",)
    )
    interrupts = ZoomInterruptSource(
        clock=clock,
        prompt="{name} wants to speak",
        cooldown_s=0.0,
        self_names=("AI Avatar",),
        voice_enabled=True,
        is_avatar_speaking=lambda: True,
    )
    observer = ZoomMeetingObserver(
        speakers=speakers, interrupts=interrupts, clock=clock
    )

    observer.on_page_event({"type": "speaker", "name": "Dev", "isSelf": False})

    assert speakers.current_speaker() == "Dev"
    assert interrupts.voices == 1
    assert interrupts.received == 1


def test_the_avatar_does_not_interrupt_itself() -> None:
    """It is an active speaker precisely when the barge-in gate is open.

    The page's ``isSelf`` is deliberately not trusted for this: the page knows one configured
    name, and the Python side knows every name the avatar might have joined under.
    """
    clock = MediaClock()
    speakers = ZoomSpeakerTracker(
        clock=clock, hold_ms=0, merge_gap_ms=0, self_names=("AI Avatar",)
    )
    observer = ZoomMeetingObserver(speakers=speakers, clock=clock)

    observer.on_page_event({"type": "speaker", "name": "AI Avatar", "isSelf": False})

    assert speakers.current_speaker() != "AI Avatar"


def test_only_settled_captions_reach_the_transcript() -> None:
    """An agent handed half a sentence answers half a question."""
    transcript = ZoomTranscript(self_names=("AI Avatar",))
    observer = ZoomMeetingObserver(transcript=transcript, clock=MediaClock())

    observer.on_page_event(
        {"type": "caption", "name": "Dev", "text": "what is the", "final": False}
    )
    assert transcript.count == 0

    observer.on_page_event(
        {"type": "caption", "name": "Dev", "text": "what is the status", "final": True}
    )
    assert transcript.count == 1


def test_chat_reaches_the_transcript_even_when_it_is_not_addressed_to_the_avatar() -> None:
    """The ordering fix from the RTMS path, holding for the page path too.

    The chat source drops everything not addressed to the avatar, which is the right policy
    for deciding what to *answer* and the wrong one for deciding what to *remember*: a
    meeting held largely in chat would leave the avatar describing only the half aimed at it.
    """
    transcript = ZoomTranscript(self_names=("AI Avatar",))
    chat = ZoomChatSource(require_mention=True, mention_names=("AI Avatar",))
    observer = ZoomMeetingObserver(transcript=transcript, chat=chat, clock=MediaClock())

    observer.on_page_event(
        {"type": "chat", "id": "1", "name": "Dev", "text": "morning everyone"}
    )

    assert transcript.snapshot().chat_lines == 1
    assert chat.ignored == 1


def _interrupts(clock: MediaClock) -> ZoomInterruptSource:
    return ZoomInterruptSource(
        clock=clock,
        prompt="{name} wants to speak",
        cooldown_s=0.0,
        self_names=("AI Avatar",),
        voice_enabled=True,
        is_avatar_speaking=lambda: True,
    )


def test_a_hand_that_stays_up_interrupts_once() -> None:
    """**The behaviour the live run got wrong, and the reason the state lives in Python.**

    The page keys a raised hand on a name read out of a tile Zoom re-renders constantly, and
    several frames run the observer independently. A hand that stays up while its row is
    re-rendered past the grace window is retired in the page and re-detected as a fresh raise
    — the person has not moved, and the avatar stops itself to say "ok, go ahead" again.

    Note the cooldown is zero here on purpose: a rate limit would still let the repeat
    through, only later, which is the same bug slowed down.
    """
    clock = MediaClock()
    interrupts = _interrupts(clock)
    observer = ZoomMeetingObserver(interrupts=interrupts, clock=clock)

    raise_event = {"type": "handRaise", "id": "name:dev", "name": "Dev", "isSelf": False}
    observer.on_page_event(dict(raise_event))
    observer.on_page_event(dict(raise_event))
    observer.on_page_event(dict(raise_event))

    assert interrupts.hands == 1


async def test_lowering_a_hand_lets_the_next_raise_through() -> None:
    """Otherwise the fix above would silence somebody for the rest of the meeting.

    The queue is drained between raises because ``ZoomInterruptSource`` holds a single
    undelivered request and drops anything offered on top of it — its own guard, unrelated to
    the one under test, and the router drains it continuously in production.
    """
    clock = MediaClock()
    interrupts = _interrupts(clock)
    observer = ZoomMeetingObserver(interrupts=interrupts, clock=clock)
    floor = interrupts.events()

    observer.on_page_event({"type": "handRaise", "id": "name:dev", "name": "Dev"})
    assert (await anext(floor)).participant == "Dev"

    observer.on_page_event({"type": "handLower", "id": "name:dev"})
    observer.on_page_event({"type": "handRaise", "id": "name:dev", "name": "Dev"})

    assert interrupts.hands == 2


async def test_two_people_raising_hands_are_two_interruptions() -> None:
    """The suppression is per participant, not a global latch."""
    clock = MediaClock()
    interrupts = _interrupts(clock)
    observer = ZoomMeetingObserver(interrupts=interrupts, clock=clock)
    floor = interrupts.events()

    observer.on_page_event({"type": "handRaise", "id": "name:dev", "name": "Dev"})
    assert (await anext(floor)).participant == "Dev"

    observer.on_page_event({"type": "handRaise", "id": "name:priya", "name": "Priya"})
    assert (await anext(floor)).participant == "Priya"

    assert interrupts.hands == 2


def test_the_brief_names_the_only_other_person_as_the_one_speaking() -> None:
    """**The fix for "what is my name", asked by voice.**

    A live run gave the agent "Currently in the meeting (1): Dev Choudhary" and it still
    answered "I'm sorry, but I don't know your name". The same question *typed* was answered
    correctly, because a chat message arrives with its sender attached and a spoken turn does
    not — the avatar hears one mixed stream carrying no attribution at all.

    With exactly one other participant the inference is not a guess, so the brief makes it.
    """
    ledger = _ledger()
    observer = ZoomMeetingObserver(attendance=ledger, clock=MediaClock())
    observer.on_page_event({"type": "roster", "names": ["AI Avatar", "Dev Choudhary"]})

    brief = ledger.snapshot().agent_context()
    assert (
        '"Dev Choudhary" is the only other person here, so anyone speaking to the avatar '
        'right now is "Dev Choudhary"'
    ) in brief
    # ``present`` holds records, not names. Asserting only that the name appears *somewhere*
    # passes against a dataclass repr — which is what the first version of this did, while
    # the brief carried timestamps and user ids into the agent's context window.
    assert "AttendanceRecord" not in brief
    assert "first_seen_us" not in brief


def test_the_brief_names_nobody_when_two_people_could_be_speaking() -> None:
    """Fails closed, like every other elimination here.

    Greeting the wrong person by name is worse than not knowing.
    """
    ledger = _ledger()
    observer = ZoomMeetingObserver(attendance=ledger, clock=MediaClock())
    observer.on_page_event({"type": "roster", "names": ["AI Avatar", "Dev", "Priya"]})

    brief = ledger.snapshot().agent_context()
    assert "only other person" not in brief
    assert "Dev" in brief and "Priya" in brief


def test_an_unknown_page_event_is_ignored_rather_than_fatal() -> None:
    """A page and this build can drift apart; that is not a reason to fail a session."""
    observer = ZoomMeetingObserver(attendance=_ledger(), clock=MediaClock())
    observer.on_page_event({"type": "somethingNew", "payload": {"a": 1}})
    observer.on_page_event({"type": "roster"})
    observer.on_page_event({})


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


def _config(**env: object) -> ZoomWebConnectorConfig:
    settings = Settings(
        zoom_web={"enabled": True, **env},  # type: ignore[arg-type]
        _env_file=None,  # type: ignore[call-arg]
    )
    return ZoomWebConnectorConfig.from_settings(settings)


def test_browser_ingest_is_the_default() -> None:
    """Because it is the mode that works on an account the operator does not own."""
    assert _config().browser_ingest is True


def test_rtms_subscription_switches_do_not_disable_browser_observers() -> None:
    """The failure this fold exists to prevent is invisible.

    An ``RTMS_EVENTS_ENABLED=false`` left over from an earlier configuration would switch off
    attendance, speaker tracking and barge-in in a mode where RTMS is not involved at all —
    and the only symptom would be an avatar that never knows who is in the meeting.
    """
    config = _config(
        rtms_events_enabled=False,
        rtms_chat_enabled=False,
        rtms_transcript_enabled=False,
    )
    assert config.attendance_enabled
    assert config.speaker_tracking_enabled
    assert config.voice_interrupt_enabled
    assert config.chat_enabled
    assert config.transcript_enabled


def test_rtms_mode_still_folds_consumers_into_subscriptions() -> None:
    """The property doc 008 §7 describes, unchanged: a stream nobody reads is not requested."""
    config = _config(ingest_mode="rtms", rtms_events_enabled=False)
    assert not config.attendance_enabled
    assert not config.speaker_tracking_enabled
    assert not config.voice_interrupt_enabled


def test_captions_are_read_but_not_switched_on_by_default() -> None:
    """Reading a panel somebody opened and opening it are different acts.

    The second is visible to everybody in the meeting, so it is opt-in even though it is the
    only thing that makes "what did they say" answerable.
    """
    config = _config()
    assert config.captions_enabled
    assert not config.captions_auto_enable


def test_captions_are_not_enabled_for_a_transcript_nobody_keeps() -> None:
    """Opening a panel in somebody else's meeting for no purpose at all."""
    config = _config(captions_auto_enable=True, transcript_enabled=False)
    assert not config.captions_auto_enable
    assert not config.captions_enabled


def test_rtms_mode_reads_none_of_the_page_observer_settings() -> None:
    config = _config(ingest_mode="rtms", captions_auto_enable=True)
    assert not config.browser_ingest
    assert not config.captions_enabled
    assert not config.captions_auto_enable


# --------------------------------------------------------------------------- #
# The seam between the two legs
# --------------------------------------------------------------------------- #


def _session(config: ZoomWebConnectorConfig):  # type: ignore[no-untyped-def]
    from src.connectors.zoom_web.session.zoom_web_session import ZoomWebSessionFactory
    from src.domain.meeting import MeetingContext, MeetingPlatform
    from src.domain.session import SessionContext
    from tests.fakes.meet_page import FakeBrowserDriver

    session = SessionContext(
        session_id=SessionId("ses_zoomweb000000000000000000000"),
        correlation_id=CorrelationId("cor_zoomweb000000000000000000000"),
        meeting=MeetingContext(
            platform=MeetingPlatform.ZOOM_WEB,
            meeting_number="94241716923",
            passcode="139601",
            display_name="AI Avatar",
        ),
    )
    factory = ZoomWebSessionFactory(config=config, driver_override=FakeBrowserDriver())
    return factory.build(session)


def test_the_only_other_person_in_the_meeting_is_named_by_elimination() -> None:
    """**"Someone wants to say something" in a two-person meeting.**

    That is what every interruption in the first live run reported: the tapped mix carries no
    attribution and Zoom's speaking indicator had not been found in the DOM, so the tracker
    had nothing to offer. Elimination is the same repair ``ZoomMeetingObserver._named``
    already applies to an unattributable raised hand.
    """
    from src.connectors.zoom_web.session.zoom_web_session import _speaker_provider

    ledger = _ledger()
    observer = ZoomMeetingObserver(attendance=ledger, clock=MediaClock())
    observer.on_page_event({"type": "roster", "names": ["AI Avatar", "Dev Choudhary"]})

    provider = _speaker_provider(None, ledger)
    assert provider is not None
    assert provider() == "Dev Choudhary"


def test_elimination_fails_closed_with_two_candidates() -> None:
    """A confidently wrong "Priya wants to say something" is worse than "Someone".

    The agent would greet the wrong person by name. ``None`` costs the name and never the
    barge-in — the router falls back to its anonymous wording.
    """
    from src.connectors.zoom_web.session.zoom_web_session import _speaker_provider

    ledger = _ledger()
    observer = ZoomMeetingObserver(attendance=ledger, clock=MediaClock())
    observer.on_page_event({"type": "roster", "names": ["AI Avatar", "Dev", "Priya"]})

    provider = _speaker_provider(None, ledger)
    assert provider is not None
    assert provider() is None


def test_the_tracker_wins_over_elimination_when_it_knows() -> None:
    """Elimination is a repair, not the primary answer — it is guesswork that happens to be
    sound at exactly one roster size."""
    from src.connectors.zoom_web.session.zoom_web_session import _speaker_provider

    clock = MediaClock()
    ledger = _ledger()
    speakers = ZoomSpeakerTracker(
        clock=clock, hold_ms=0, merge_gap_ms=0, self_names=("AI Avatar",)
    )
    observer = ZoomMeetingObserver(attendance=ledger, speakers=speakers, clock=clock)
    observer.on_page_event({"type": "roster", "names": ["AI Avatar", "Dev", "Priya"]})
    observer.on_page_event({"type": "speaker", "name": "Priya"})

    provider = _speaker_provider(speakers, ledger)
    assert provider is not None
    assert provider() == "Priya"


def test_browser_ingest_builds_a_page_source_and_no_rtms_trigger() -> None:
    """Nothing to trigger: there is no RTMS connection waiting for a stream to start.

    Asking Zoom to start one would provoke a webhook nothing is listening for — on an account
    that, in the case this mode exists for, cannot serve the request anyway.
    """
    built = _session(_config())
    assert isinstance(built._source, PageAudioSource)
    assert built._trigger is None


def test_the_echo_gate_is_open_under_browser_ingest_and_shut_under_rtms() -> None:
    """The single difference that makes energy barge-in possible in one mode and not the other.

    RTMS delivers the meeting's mix *with the avatar in it*, so the gate is the only defence
    and has to withhold every inbound frame while the avatar talks — which is exactly the
    window a barge-in exists in. The page tap has no such loop, so the gate is a backstop and
    the detector can hear the interruption.

    Getting this backwards restores the behaviour doc 008 §4 was written about, silently.
    """
    browser = _session(_config())
    rtms = _session(_config(ingest_mode="rtms"))

    assert browser._router._echo_guard._gate_enabled is False
    assert rtms._router._echo_guard._gate_enabled is True
    assert rtms._router._echo_guard._strict is True


def test_the_page_is_only_asked_to_observe_what_python_will_read() -> None:
    """An observer whose ledger is off would scan a DOM to produce events nothing consumes —
    on the renderer thread that also encodes the avatar's audio."""
    quiet = _session(
        _config(attendance_enabled=False, chat_enabled=False, transcript_enabled=False)
    )
    bootstrap = quiet._page_bootstrap()
    assert '"rosterEnabled": false' in bootstrap
    assert '"chatEnabled": false' in bootstrap
    assert '"captionsEnabled": false' in bootstrap


def test_rtms_mode_ships_no_capture_worklet_to_the_page() -> None:
    """The script the page runs under RTMS ingest is the one it ran before this existed."""
    bootstrap = _session(_config(ingest_mode="rtms"))._page_bootstrap()
    assert '"captureWorkletSource": null' in bootstrap
    assert '"rosterEnabled": false' in bootstrap
    assert '"ingestMode": "rtms"' in bootstrap


def test_browser_mode_ships_the_capture_worklet() -> None:
    bootstrap = _session(_config())._page_bootstrap()
    assert "mc-zoom-capture" in bootstrap
    assert '"ingestMode": "browser"' in bootstrap
