"""The one place the page's observations are fanned out to the things that remember them.

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

**Every method here is synchronous and total**, because the page server calls them from its
read loop — the loop that also carries the avatar's voice into the page. That loop guards
against an exception anyway, and this does not rely on it: each consumer already swallows its
own, so the belt and the braces are both present on the path where the cost of being wrong is
the meeting going silent.
"""

from __future__ import annotations

from src.connectors.zoom_web.meeting.active_speaker import ZoomSpeakerTracker
from src.connectors.zoom_web.meeting.attendance import ZoomAttendanceLedger
from src.connectors.zoom_web.meeting.chat import ZoomChatSource
from src.connectors.zoom_web.meeting.hand_raise import ZoomInterruptSource
from src.connectors.zoom_web.meeting.transcript import ZoomTranscript
from src.connectors.zoom_web.observations import (
    ParticipantEvent,
    SpeakerEvent,
    TranscriptLine,
)
from src.connectors.zoom_web.page.protocol import (
    EVENT_CAPTION,
    EVENT_CHAT,
    EVENT_HAND_LOWER,
    EVENT_HAND_RAISE,
    EVENT_PAGE,
    EVENT_ROSTER,
    EVENT_SPEAKER,
)
from src.domain.meeting import ChatMessage
from src.infrastructure.logging import get_logger
from src.services.media.clock import MediaClock

logger = get_logger(__name__)


def _safe(name: str, call: object, *args: object) -> None:
    """Invoke one consumer, absorbing whatever it does.

    **Guarded per call rather than per method, and that is the difference that matters.**
    Wrapping each method's whole body would mean one unhappy consumer silently costing
    every consumer *after* it — and the order here is fixed, so "after it" is a specific
    list rather than an accident. This way a bug in the transcript costs the transcript
    and the chat source still sees the message.

    Redundant with the page server's own guard, deliberately. That one protects the socket;
    this one protects the other consumers, and neither substitutes for the other. Each
    consumer also guards itself, which is why nothing here is expected to fire — a log line
    from this function means something is wrong that nobody predicted.
    """
    try:
        call(*args)  # type: ignore[operator]
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("zoom_web.observer_consumer_failed", consumer=name, error=str(exc))


class ZoomMeetingObserver:
    """Routes the page's observations to the ledger, tracker, transcript, chat and interrupts.

    Every collaborator is optional, and an absent one means that feature is switched off —
    never a fault. A session with attendance disabled and chat enabled builds this with a
    chat source and no ledger, and nothing here branches on more than "is it there".
    """

    __slots__ = (
        "_attendance",
        "_chat",
        "_clock",
        "_hands_up",
        "_interrupts",
        "_leave_grace_us",
        "_missing_since",
        "_roster",
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
        clock: MediaClock | None = None,
        leave_grace_s: float = 8.0,
    ) -> None:
        self._attendance = attendance
        self._speakers = speakers
        self._transcript = transcript
        self._chat = chat
        self._interrupts = interrupts
        # Optional only so a test can build this without one. The page's observations carry
        # no time of their own, so in the connector it is always supplied.
        self._clock = clock
        # Casefolded name -> the name as the page spelled it. **Held here rather than read
        # back off the ledger**, and the difference shows up in one specific case: the ledger
        # keeps people who have *left* as departed records, so diffing against it would
        # re-announce a rejoin as a join and then never see them leave again. This is the
        # page's last reported view and nothing else.
        self._roster: dict[str, str] = {}
        # Whose hand is up, keyed exactly as the page keys it. Held here because this
        # outlives every page re-render, reload and frame — see ``_on_hand``.
        self._hands_up: set[str] = set()
        # Casefolded name -> when the page stopped seeing them. A departure is not believed
        # until it has persisted; see ``_on_roster``.
        self._missing_since: dict[str, int] = {}
        self._leave_grace_us = max(int(leave_grace_s * 1_000_000), 0)

    # -- observation consumers ---------------------------------------------

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

        **Everything the avatar knows about the meeting arrives here**: the roster, the active
        speaker, the captions, the chat, a raised hand, and the diagnostics that say whether
        each observer is actually finding anything. This connector once read most of that from
        Zoom's RTMS stream instead, which required the meeting to be hosted on an
        RTMS-enabled account; the page is what removes that requirement.

        Each branch translates into one of this connector's observation types and hands it to
        the same consumers, so nothing below this method knows the difference between a DOM
        sweep and an event stream.
        """
        try:
            kind = str(event.get("type") or "")
            if kind == EVENT_HAND_RAISE:
                self._on_hand(dict(event))
                return
            if kind == EVENT_HAND_LOWER:
                self._hands_up.discard(str(event.get("id") or ""))
                return
            if kind == EVENT_ROSTER:
                self._on_roster(event)
                return
            if kind == EVENT_SPEAKER:
                self._on_speaker(event)
                return
            if kind == EVENT_CAPTION:
                self._on_caption(event)
                return
            if kind == EVENT_CHAT:
                self._on_chat(event)
                return
            if kind == EVENT_PAGE:
                logger.info(
                    "zoom_web.page_event",
                    name=event.get("name"),
                    detail=event.get("detail"),
                )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("zoom_web.page_event_failed", error=str(exc))

    def _on_hand(self, event: dict[str, object]) -> None:
        """One raised hand, reported at most once per raise.

        **The page cannot be the only thing holding "whose hand is up", and a live meeting is
        why.** Its set is keyed on a name read out of a tile Zoom re-renders constantly, and
        several frames run the observer independently; a hand that stays up while its row is
        re-rendered for longer than the grace window is retired in the page and re-detected
        as a fresh raise on the next scan. The person in the meeting has not moved, and the
        avatar stops itself to say "ok, go ahead" again — repeatedly, for as long as they
        leave their hand up.

        So the page reports edges *and* lowers, and the authority for "still up" lives here:
        this object outlives every page re-render, reload and frame, and it is the only place
        the several frames' reports converge.

        Deliberately **not** the per-participant cooldown in ``ZoomInterruptSource``. That is
        a rate limit — it would still fire again every cooldown for an unmoved hand, which is
        the exact behaviour being fixed, only slower.
        """
        key = str(event.get("id") or "")
        if key and key in self._hands_up:
            logger.debug(
                "zoom_web.hand_already_up",
                participant=event.get("name"),
                note="the page re-detected a hand that has not been lowered",
            )
            return
        if key:
            self._hands_up.add(key)
        if self._interrupts is not None:
            self._interrupts.offer_hand(self._named(dict(event)))

    # -- the page's observations, translated -------------------------------

    def _on_roster(self, event: dict[str, object]) -> None:
        """A list of who the page can see, turned into joins and leaves.

        **The page reports a level and the ledger wants edges**, so the diff happens here.
        Doing it in the page instead would put the authoritative roster inside something that
        does not survive a reload — and a reload would then re-announce the whole meeting as
        having just joined, which the announcer would push to the agent as news.

        **An empty roster is ignored rather than treated as an empty meeting**, which matters
        because ``add_init_script`` runs in every frame Chromium creates and most of Zoom's
        frames have no participants panel in them. A frame that cannot see the list reports
        nothing (``scanRoster`` returns early), but a frame that could see it and then had
        its panel closed would report zero — and the avatar itself is always in its own
        participants list, so a genuinely empty roster is not a state this can observe. The
        cost is that "everybody left" is invisible; the alternative is the roster being wiped
        by a re-render, which is both more likely and worse.
        """
        raw = event.get("names")
        if not isinstance(raw, list):
            return
        seen: list[str] = []
        for item in raw:
            name = " ".join(str(item or "").split())
            if name and not any(name.casefold() == known.casefold() for known in seen):
                seen.append(name)
        if not seen:
            return

        current = {name.casefold(): name for name in seen}
        previous = self._roster
        at_us = self._now_us()
        changed = False

        # Arrivals are believed immediately. A name that has appeared is evidence of its own
        # kind — there is no layout in which Zoom invents a participant.
        for key, name in current.items():
            self._missing_since.pop(key, None)
            if key not in previous:
                previous[key] = name
                self._participant(name, joined=True, at_us=at_us)
                changed = True

        # **Departures are held for a grace window, and a live run is why.** The roster is
        # read off the tile grid, and Zoom re-lays that grid out constantly — switching
        # between speaker and gallery view, opening a panel, somebody sharing a screen. A run
        # logged ``zoom_attendance.left dev Choudhary stayed_s=142.8`` followed by
        # ``rejoined`` forty-four seconds later, with the person never having moved: the tile
        # count simply went from two to one and back.
        #
        # That is expensive rather than cosmetic. Each flap re-pushes the meeting brief to
        # the agent, so the avatar is told the room emptied and refilled, and elimination
        # (``_speaker_provider``) briefly has nobody to name.
        #
        # An arrival needs no such window, so this is deliberately asymmetric: the cost of a
        # late leave is a departed name lingering for a few seconds, and the cost of an early
        # one is the loop above.
        for key in tuple(previous):
            if key in current:
                continue
            since = self._missing_since.get(key)
            if since is None:
                self._missing_since[key] = at_us
                continue
            if at_us - since < self._leave_grace_us:
                continue
            name = previous.pop(key)
            self._missing_since.pop(key, None)
            self._participant(name, joined=False, at_us=at_us)
            changed = True

        if changed:
            self._publish_roster()

    def _participant(self, name: str, *, joined: bool, at_us: int) -> None:
        """Feed one derived join/leave to the attendance ledger.

        ``user_id=None`` throughout, and that is a real loss rather than a placeholder. A
        participant id identifies a *presence*, which is what would let two people with the
        same display name be told apart; a DOM has no such thing. ``ZoomAttendanceLedger``
        already keys on the name — it has to, because ids are minted afresh on a rejoin — so
        nothing downstream breaks. What is genuinely lost is the ability to distinguish two
        participants who are both called "Dev".
        """
        event = ParticipantEvent(
            user_id=None, display_name=name, joined=joined, at_us=at_us
        )
        if self._attendance is not None:
            _safe("attendance.observe", self._attendance.observe, event)

    def _on_speaker(self, event: dict[str, object]) -> None:
        """The page believes somebody has the floor.

        Routed to exactly two consumers — the tracker, then the interrupt source — and
        for the same reasons — the tracker first, so that an interruption reaching the router
        in the same tick finds ``current_speaker`` already naming the person who caused it.

        **``isSelf`` is dropped here rather than acted on**, because the page only knows the
        one name it was configured with, and ``_self_name_candidates`` on the Python side
        knows two. The tracker and the interrupt source each do their own self-check against
        the full list; letting the page's narrower answer short-circuit that is how the
        avatar ends up interrupting itself.
        """
        name = " ".join(str(event.get("name") or "").split())
        if not name:
            return
        speaker = SpeakerEvent(user_id=None, display_name=name, at_us=self._now_us())
        if self._speakers is not None:
            _safe("speakers.observe", self._speakers.observe, speaker)
        if self._interrupts is not None:
            _safe("interrupts.offer_voice", self._interrupts.offer_voice, speaker)

    def _on_caption(self, event: dict[str, object]) -> None:
        """One settled line of Zoom's live transcription.

        Interim lines are refused here as well as in the page. The page already withholds
        them, so this is the belt to those braces — a caption that is still being revised is
        half a sentence, and an agent handed half a sentence answers half a question.
        """
        if not event.get("final"):
            return
        text = str(event.get("text") or "").strip()
        if not text or self._transcript is None:
            return
        name = " ".join(str(event.get("name") or "").split()) or None
        line = TranscriptLine(
            user_id=None, display_name=name, text=text, at_us=self._now_us()
        )
        _safe("transcript.offer", self._transcript.offer, line)

    def _on_chat(self, event: dict[str, object]) -> None:
        """One message from the chat panel.

        Delegates to ``on_chat`` rather than repeating its body, so the ordering that fix
        encodes — the transcript sees every message, the mention filter runs after — holds
        for both ingest modes without being written down twice.
        """
        text = str(event.get("text") or "").strip()
        if not text:
            return
        sender = " ".join(str(event.get("name") or "").split()) or None
        self.on_chat(
            ChatMessage(text=text, sender=sender, received_at_us=self._now_us())
        )

    def _now_us(self) -> int:
        """Media-clock time, or zero when this observer has no clock.

        Zero is what the observation types already default to, so an observer built without a
        clock — which only a test does — degrades to unstamped rather than failing.
        """
        if self._clock is None:
            return 0
        try:
            return self._clock.now_us()
        except Exception:  # pragma: no cover - defensive
            return 0

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
