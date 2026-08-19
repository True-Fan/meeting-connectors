"""The one place RTMS observations are fanned out to the things that remember them.

**Why this exists rather than five listeners registered on the service.** The Google Meet
connector registers roster listeners individually, which is right there: its roster is one
stream feeding several consumers, and each subscribes to what it wants. Here the streams
are four and the consumers are five, and the interesting part is not the routing — it is
that a single observation usually has to reach more than one of them, in a fixed order,
with something derived in between. A participant joining updates the ledger *and* then
re-supplies the candidate list to everything that eliminates against it. A chat message
goes to the transcript before the mention filter, so the ledger holds the conversation and
not only the half addressed to the avatar. Spreading that across five independent
subscriptions would leave the order implicit and the derivation duplicated.

So this is the wiring, written down once, in the order it has to happen.

**Every method here is synchronous and total**, because ``RtmsService`` calls them from the
media and signaling pumps — the loops that also carry the meeting's audio. The service
guards against an exception anyway (``RtmsService._notify``), and this does not rely on
that: each consumer already swallows its own, so the belt and the braces are both present
on the path where the cost of being wrong is the meeting going silent.
"""

from __future__ import annotations

from src.connectors.zoom.rtms.observations import (
    ParticipantEvent,
    SpeakerEvent,
    TranscriptLine,
)
from src.connectors.zoom_web.meeting.active_speaker import ZoomSpeakerTracker
from src.connectors.zoom_web.meeting.attendance import ZoomAttendanceLedger
from src.connectors.zoom_web.meeting.chat import ZoomChatSource
from src.connectors.zoom_web.meeting.hand_raise import ZoomInterruptSource
from src.connectors.zoom_web.meeting.transcript import ZoomTranscript
from src.domain.meeting import ChatMessage
from src.infrastructure.logging import get_logger

logger = get_logger(__name__)


def _safe(name: str, call: object, *args: object) -> None:
    """Invoke one consumer, absorbing whatever it does.

    **Guarded per call rather than per method, and that is the difference that matters.**
    Wrapping each method's whole body would mean one unhappy consumer silently costing
    every consumer *after* it — and the order here is fixed, so "after it" is a specific
    list rather than an accident. This way a bug in the transcript costs the transcript
    and the chat source still sees the message.

    Redundant with ``RtmsService._notify``, deliberately. That guard protects the
    connection; this one protects the other consumers, and neither substitutes for the
    other. Each consumer also guards itself, which is why nothing here is expected to fire
    — a log line from this function means something is wrong that nobody predicted.
    """
    try:
        call(*args)  # type: ignore[operator]
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("zoom_web.observer_consumer_failed", consumer=name, error=str(exc))


class ZoomMeetingObserver:
    """Routes RTMS observations to the ledger, tracker, transcript, chat and interrupts.

    Every collaborator is optional, and an absent one means that feature is switched off —
    never a fault. A session with attendance disabled and chat enabled builds this with a
    chat source and no ledger, and nothing here branches on more than "is it there".
    """

    __slots__ = (
        "_attendance",
        "_chat",
        "_interrupts",
        "_speakers",
        "_transcript",
    )

    def __init__(
        self,
        *,
        attendance: ZoomAttendanceLedger | None = None,
        speakers: ZoomSpeakerTracker | None = None,
        transcript: ZoomTranscript | None = None,
        chat: ZoomChatSource | None = None,
        interrupts: ZoomInterruptSource | None = None,
    ) -> None:
        self._attendance = attendance
        self._speakers = speakers
        self._transcript = transcript
        self._chat = chat
        self._interrupts = interrupts

    # -- MeetingObserver ---------------------------------------------------

    def on_participant(self, event: ParticipantEvent) -> None:
        """Somebody joined or left.

        The ledger first, because everything after it reads the roster the ledger keeps —
        publishing the candidate list before folding in the event that changed it would
        hand every consumer a view one event out of date, which in a two-person meeting is
        the difference between "exactly one other person" and "nobody".
        """
        if self._attendance is not None:
            _safe("attendance.observe", self._attendance.observe, event)
        # Learned regardless of the ledger: a user id becoming a name is useful to the
        # speaker tracker whether or not anybody is keeping attendance.
        if self._speakers is not None:
            _safe(
                "speakers.observe_name",
                self._speakers.observe_name,
                event.user_id,
                event.display_name,
            )
        self._publish_roster()

    def on_speaker(self, event: SpeakerEvent) -> None:
        """The floor changed hands.

        Two consumers with genuinely different jobs, and both get it. The tracker records
        *who is talking* for the whole meeting, which is what the agent is briefed with and
        what the API serves. The interrupt source asks a much narrower question — is this
        somebody talking over the avatar right now — and answers it by stopping the avatar.

        The tracker first, so that if the interruption reaches the router in the same tick,
        ``current_speaker`` already names the person who caused it.
        """
        if self._speakers is not None:
            _safe("speakers.observe", self._speakers.observe, event)
            _safe(
                "speakers.observe_name",
                self._speakers.observe_name,
                event.user_id,
                event.display_name,
            )
        if self._interrupts is not None:
            _safe("interrupts.offer_voice", self._interrupts.offer_voice, event)

    def on_transcript(self, line: TranscriptLine) -> None:
        """Zoom transcribed a line of speech."""
        if self._transcript is not None:
            _safe("transcript.offer", self._transcript.offer, line)
        # The transcript is the other place a user id and a name arrive together, and it
        # arrives *while somebody is talking* — which is exactly when a speaker event that
        # carried only an id needs naming.
        if self._speakers is not None:
            _safe(
                "speakers.observe_name",
                self._speakers.observe_name,
                line.user_id,
                line.display_name,
            )

    def on_chat(self, message: ChatMessage) -> None:
        """Somebody typed in the meeting chat.

        **The transcript before the chat source, and that ordering is the fix for a real
        failure rather than a preference.** The chat source drops everything not addressed
        to the avatar, which is most of a meeting's chat and is the correct policy for
        deciding what to *answer*. It is the wrong policy for deciding what to *remember*:
        a meeting held largely in chat would leave the avatar, asked what had been
        discussed, describing only the messages aimed at it. So the ledger sees every
        message, and the filter runs after.
        """
        if self._transcript is not None:
            _safe("transcript.offer_chat", self._transcript.offer_chat, message)
        if self._chat is not None:
            _safe("chat.offer", self._chat.offer, message)

    # -- page events -------------------------------------------------------

    def on_page_event(self, event: dict[str, object]) -> None:
        """One JSON event from the injected page script. Never raises.

        The only thing that arrives this way is a raised hand and the diagnostics that say
        whether the observer looking for one is running — everything else about this
        meeting comes from RTMS. See ``connectors/zoom_web/js/inject.js`` for why that one
        signal has no API behind it.
        """
        try:
            kind = str(event.get("type") or "")
            if kind == "handRaise":
                if self._interrupts is not None:
                    self._interrupts.offer_hand(self._named(dict(event)))
                return
            if kind == "pageEvent":
                logger.info(
                    "zoom_web.page_event",
                    name=event.get("name"),
                    detail=event.get("detail"),
                )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("zoom_web.page_event_failed", error=str(exc))

    def _named(self, event: dict[str, object]) -> dict[str, object]:
        """Put a name on a hand the page could not attribute, when there is only one it
        can be.

        **The page usually cannot read the name off the tile that has the hand on it**, for
        a structural reason: Zoom renders the indicator on the participant's *video* tile,
        and a tile showing video carries an image where a camera-off tile carries the name.
        So the person whose hand is up is exactly the person whose name is not written down.

        Elimination closes that, and here it is far stronger than the equivalent on Google
        Meet: the roster comes from Zoom's own participant events rather than from reading a
        DOM, so "exactly one other person" is a fact rather than an inference. Fails closed
        at two or more — naming one of them would be a guess, and a confidently wrong
        "Priya raised their hand" is worse than "Someone".
        """
        if event.get("name") or self._attendance is None:
            return event
        others = self._attendance.present_names
        if len(others) == 1:
            event["name"] = others[0]
            logger.info(
                "zoom_web.hand_named_by_elimination",
                participant=others[0],
                note="the page saw a raised hand it could not attribute, and exactly one "
                "other person is in the meeting",
            )
        return event

    # -- internals ---------------------------------------------------------

    def _publish_roster(self) -> None:
        """Re-supply "who else is in the room" to everything that eliminates against it.

        Derived from the ledger rather than tracked separately, so there is one account of
        who is present and the three consumers of it cannot disagree. Cheap: a pass over a
        handful of entries, on an event that arrives when somebody joins or leaves.
        """
        if self._attendance is None:
            return
        try:
            present = self._attendance.present_names
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("zoom_web.roster_read_failed", error=str(exc))
            return
        if self._speakers is not None:
            _safe("speakers.observe_participants", self._speakers.observe_participants, present)
        if self._transcript is not None:
            _safe(
                "transcript.observe_participants",
                self._transcript.observe_participants,
                present,
            )
        if self._chat is not None:
            _safe("chat.observe_participants", self._chat.observe_participants, present)
