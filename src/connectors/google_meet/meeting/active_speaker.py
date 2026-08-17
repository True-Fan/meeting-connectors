"""Who is speaking now, and who said what for the rest of the meeting.

**The constraint this module is written under.** The avatar's inbound audio is a *mix* — the
capture graph in ``js/bridge.js`` sums every remote track into one mono node before the worklet
samples it, which is what keeps ingest resampler-free and cheap. That is deliberate and it is
not changed here: no frame gains a source id, no frame is delayed, and the audio the agent
hears is byte-for-byte what it heard before this file existed. Attribution is assembled
*beside* the media path from two observations the page can make without touching it:

* **per-track energy**, measured on an ``AnalyserNode`` branched off the source node that
  already feeds the mix — this says precisely *when* somebody is talking;
* **the tile the stream is rendered on**, plus Meet's own speaking indicator — these say *who*.

So this module's job is to turn a stream of edges into two answers: *who is speaking right
now*, and *who has spoken, when, and for how long*. Neither is derivable from the audio, and
both are what "the agent knows who is talking" actually requires.

**Three properties earned from the features that came before it.**

*Identity may arrive late.* Somebody can start talking before Meet has drawn their tile, so a
turn can open under a track id and be named a second later. A repeated ``speaking: true`` for
the same track is therefore a **rename of an open turn**, never a new one — otherwise the first
sentence of every meeting is attributed to nobody and the second to a person who never stopped.

*Two observers must not become two speakers.* Energy and the DOM indicator report the same
person independently. Turns are keyed on **identity**, and a second observer of somebody
already speaking attaches to the open turn rather than starting a rival one.

*A pause is not the end of a turn.* Speech has gaps at every clause boundary, and the page's
release window is short on purpose so a turn *ends* promptly. Rejoining a turn interrupted by
less than ``merge_gap_ms`` is what makes the history read like a conversation instead of like a
waveform — 40 fragments of "Priya" become one turn of Priya talking.

Everything here is synchronous, total, and non-blocking, because ``offer`` is called from the
bridge's read loop — the media channel. The same rule ``MeetChatSource.offer``,
``MeetHandRaiseSource.offer`` and ``AttendanceLedger.observe_roster`` are written to, for the
same reason: a bug in a bookkeeping feature must never be able to stall or tear down audio.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from src.connectors.google_meet.meeting.participants import (
    MeetRoster,
    clean_label,
    looks_like_self,
)
from src.infrastructure.logging import get_logger
from src.services.media.clock import MediaClock

logger = get_logger(__name__)

COMPONENT_NAME = "google_meet_active_speaker"

SOURCE_AUDIO = "audio"
"""Per-track energy. Precise in time, silent about identity."""

SOURCE_DOM = "dom"
"""Meet's own speaking indicator. Names somebody, and only while Meet renders it."""

ANONYMOUS = "Someone"
"""What an unattributed speaker is called. The same stand-in ``meeting/hand_raise.py`` uses,
because the answer has to name somebody and "a participant we could not identify is speaking"
is worse than an honest placeholder."""

_MAX_NAME_LEN = 120
_MAX_TURNS = 2_000
"""Ceiling on remembered turns.

A guard against a pathological page rather than a product limit: a two-hour meeting of brisk
conversation is a few hundred turns, and each is a small frozen record. Past the ceiling the
*oldest* are dropped — the opposite of the attendance ledger's choice, and right for the
opposite reason. Attendance answers "who was ever here", where the first entries matter most;
this answers "who is talking and who just talked", where the newest do."""


@dataclass(frozen=True, slots=True)
class SpeakerTurn:
    """One stretch of one participant talking."""

    display_name: str | None
    page_id: str | None
    source: str
    started_us: int
    ended_us: int | None
    started_at: datetime
    ended_at: datetime | None
    inferred: bool = False
    """True when the name was reached by elimination — exactly one other person was in the
    meeting — rather than read from the page. Kept distinct because they are different claims,
    and a caller that cares about the difference should not have to guess."""

    @property
    def is_open(self) -> bool:
        """Whether they were still talking as of the last thing the page reported."""
        return self.ended_us is None

    @property
    def label(self) -> str:
        """What to call this speaker in an answer. Never empty."""
        if self.display_name:
            return self.display_name
        if self.page_id:
            return f"an unidentified participant ({self.page_id[:12]})"
        return ANONYMOUS

    def duration_us(self, *, now_us: int | None = None) -> int:
        """How long the turn lasted, extending an open one to ``now_us`` when given."""
        end = self.ended_us
        if end is None:
            end = now_us if now_us is not None else self.started_us
        return max(end - self.started_us, 0)


@dataclass(frozen=True, slots=True)
class SpeakerSnapshot:
    """An immutable answer to "who is speaking, and who has been".

    Returned rather than exposing the tracker, so a caller serialising this over HTTP cannot
    watch it change underneath them.
    """

    turns: tuple[SpeakerTurn, ...] = field(default_factory=tuple)
    current: tuple[str, ...] = field(default_factory=tuple)
    """Everyone speaking as of now, most recently started first. Plural because people talk
    over each other, and reporting one of them would be a guess dressed as a fact."""

    self_name: str | None = None
    events: int = 0
    """Edges observed. ``0`` means the page has reported nothing yet, which is the difference
    between "nobody has spoken" and "we do not know" — the same distinction
    ``AttendanceSnapshot.scans`` exists to preserve."""

    candidates: tuple[str, ...] = field(default_factory=tuple)
    """Everyone in the room who could be the voice: present, not the avatar, not known to be muted.

    Carried on the snapshot so the brief can name the field of candidates even before a single
    speaking edge has arrived — which is exactly when the agent was left to fill the gap and filled
    it with whoever had been typing."""

    now_us: int = 0
    observed_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def current_speaker(self) -> str | None:
        """The single best answer to "who is speaking", or ``None`` when nobody is."""
        return self.current[0] if self.current else None

    @property
    def is_anyone_speaking(self) -> bool:
        return bool(self.current)

    def talk_time(self) -> tuple[tuple[str, int], ...]:
        """Total seconds each participant has held the floor, longest first.

        Summed across turns rather than measured end-to-end, so somebody who spoke twice for a
        minute each reads as two minutes rather than as however long the meeting took.
        """
        totals: dict[str, int] = {}
        for turn in self.turns:
            seconds = turn.duration_us(now_us=self.now_us or None) // 1_000_000
            totals[turn.label] = totals.get(turn.label, 0) + seconds
        return tuple(sorted(totals.items(), key=lambda item: (-item[1], item[0])))

    def recent(self, limit: int = 10) -> tuple[SpeakerTurn, ...]:
        """The last ``limit`` turns, oldest first — the order the meeting happened in."""
        return self.turns[-limit:] if limit > 0 else ()

    def agent_context(self) -> str:
        """A plain-language brief for the avatar agent.

        Prose rather than JSON for the reason ``AttendanceSnapshot.agent_context`` is: its
        destination is a context window, where "Priya Menon is speaking now" is worth more than
        a field called ``current`` holding a list.

        States what is *not* known, deliberately. An agent told only that somebody is speaking
        will answer "who is talking?" with a name it invented.
        """
        if not self.events:
            heard = (
                "Nobody has been heard speaking yet — the avatar has not observed anyone "
                "take the floor."
            )
            # **And the guard, before the first edge rather than after it.** This paragraph used to
            # stop here, which left the worst moment of a meeting uncovered: somebody joins, speaks
            # immediately, and the agent is asked a question by a voice it has been told nothing
            # about. Observed live — the answer was the name of the participant who had been typing
            # in the chat, because that was the only name in the frame.
            return f"{heard} {self._unattributed_guard()}" if self.candidates else heard

        lines: list[str] = []
        if self.current:
            if len(self.current) == 1:
                speaker = self.current[0]
                if speaker == ANONYMOUS:
                    # **The sentence that stops the agent answering with the wrong person.**
                    # Observed live: somebody spoke, the page could not name the voice, and the
                    # avatar — asked "what is my name?" — answered with the name of a different
                    # participant who had been typing in the chat. The brief said "Someone is
                    # speaking right now" and left the agent to fill the gap, which it did.
                    lines.append(
                        "Somebody is speaking right now, but the avatar cannot tell which "
                        f"participant it is. {self._unattributed_guard()}"
                    )
                else:
                    # **Stated as the answer to the question that gets asked, because the
                    # inference does not happen on its own.** A live run had the agent answer
                    # "who is speaking?" with "dev Choudhary" and, a minute earlier and a minute
                    # later, answer "what is my name?" with "I do not know your name" — from the
                    # same brief. It had the fact and did not connect it to a first-person
                    # question, so the connection is now written down rather than left implied.
                    lines.append(
                        f"{speaker} is speaking right now. That is the person talking to the "
                        f"avatar, so when they say \"I\", \"me\" or \"my\" they mean "
                        f"{speaker} — asked \"what is my name?\" or \"who am I?\", the answer "
                        f"is {speaker}."
                    )
            else:
                lines.append(f"Speaking right now: {_join(self.current)}.")
        else:
            lines.append("Nobody is speaking right now.")

        spoken = self.recent(5)
        if spoken:
            order = _join(tuple(dict.fromkeys(turn.label for turn in spoken)))
            lines.append(f"Recent speakers, in order: {order}.")
            last = spoken[-1]
            if not self.current and last.display_name:
                lines.append(f"The last person heard speaking was {last.display_name}.")

        totals = self.talk_time()
        if totals:
            said = ", ".join(f"{label} {seconds}s" for label, seconds in totals[:5])
            lines.append(f"Time each person has held the floor so far: {said}.")

        if any(turn.display_name is None for turn in self.turns):
            lines.append(
                "Some speech could not be attributed to a named participant, so the figures "
                f"above under \"{ANONYMOUS}\" cover more than one person if several were "
                "unidentified."
            )

        # **Voice only, said out loud, because the two channels were being conflated.** This
        # brief is built from audio: somebody who has only typed in the chat has never taken the
        # floor and does not appear here at all. An agent that reads "recent speakers" as "people
        # who have communicated" answers "who was talking to you?" with whoever typed last — and
        # then, asked whose voice it is hearing now, offers the same name.
        lines.append(
            "This paragraph counts speech only: somebody who typed in the chat but has not "
            "spoken is not the voice being heard."
        )

        if self.self_name:
            lines.append(
                f'The avatar itself appears as "{self.self_name}" and is never counted as a '
                "speaker."
            )

        return " ".join(lines)

    def _unattributed_guard(self) -> str:
        """What to say about a voice nobody could name.

        Two halves, and the second is the one that was missing. Saying "unknown" invites the model
        to resolve it from whatever else is in the frame, and what was in the frame was a chat
        history with one name in it. So the candidates are named — the answer *is* one of them,
        which is real information — and the wrong inference is ruled out explicitly.
        """
        if not self.candidates:
            return (
                "Do not guess who it is, and do not assume it is whoever typed in the chat — "
                "ask if it matters."
            )
        who = (
            f"It is {self.candidates[0]}"
            if len(self.candidates) == 1
            else f"It is one of these people: {_join(self.candidates)}"
        )
        return (
            f"{who}. Do not assume it is whoever spoke or typed most recently — somebody typing "
            "in the chat is not the voice being heard — and ask who is speaking if the answer "
            "matters."
        )


class SpeakerTracker:
    """Turns the page's speaking edges into a current speaker and a turn history.

    Not a component in the health report and not on any media path: it holds no task, opens
    nothing, and every method returns rather than raising. ``current_speaker`` in particular is
    read from the router's inbound leg on the frame that triggers a barge-in, so it must be a
    dictionary lookup and nothing more.
    """

    __slots__ = (
        "_clock",
        "_events",
        "_hold_floor_us",
        "_hold_us",
        "_ignored",
        "_merge_gap_us",
        "_names_by_id",
        "_open",
        "_others",
        "_self_names",
        "_sequence",
        "_stopped_at",
        "_turns",
    )

    def __init__(
        self,
        *,
        clock: MediaClock,
        hold_ms: float = 1_500.0,
        merge_gap_ms: float = 1_200.0,
        self_names: tuple[str, ...] = (),
    ) -> None:
        self._clock = clock
        # How long somebody stays "the current speaker" after they stop. Without it, asking
        # who is talking during the gap between two sentences answers "nobody" — which is
        # true of that instant and useless as an answer.
        self._hold_us = max(int(hold_ms * 1_000), 0)
        # How long a gap may be before it ends a turn rather than punctuating one.
        self._merge_gap_us = max(int(merge_gap_ms * 1_000), 0)
        self._turns: list[_Turn] = []
        # track id -> the turn it is currently contributing to. Several tracks (and both
        # observers) can point at one turn; the turn closes when the last of them stops.
        self._open: dict[str, _Turn] = {}
        self._names_by_id: dict[str, str] = {}
        # Everyone in the meeting except the avatar, from the roster. What ``_only_other``
        # eliminates against — see there for why one name in this tuple is an answer and two are
        # not.
        self._others: tuple[str, ...] = ()
        self._stopped_at: dict[str, int] = {}
        self._self_names: tuple[str, ...] = ()
        self._events = 0
        self._ignored = 0
        self._sequence = 0
        # Turns closed at or before this are not eligible for the hold window. Moved by
        # ``reset``, which closes turns nobody observed ending.
        self._hold_floor_us = -1
        for name in self_names:
            self.observe_self_name(name)

    # -- inputs ------------------------------------------------------------

    def offer(self, body: dict[str, Any]) -> bool:
        """Accept one ``ACTIVE_SPEAKER`` payload from the page. Never blocks, never raises.

        Returns True when the event changed what this tracker believes. False covers a
        malformed payload, our own account, and an event that says nothing new — none of which
        is an error, and none of which may reach the read loop as an exception.
        """
        try:
            return self._offer(body)
        except Exception as exc:  # pragma: no cover - defensive; nothing below should raise
            logger.warning("meet_speaker.offer_failed", error=str(exc))
            return False

    def _offer(self, body: dict[str, Any]) -> bool:
        if not isinstance(body, dict):
            return False

        page_id = str(body.get("id") or "").strip()
        raw_name = str(body.get("name") or "")
        # Before cleaning strips the marker — ``" (you)"`` is a status suffix, so a tile labelled
        # "dev Choudhary (You)" would otherwise read as an ordinary participant.
        if looks_like_self(raw_name):
            self._ignored += 1
            return False
        name = _clean(raw_name) or self._names_by_id.get(page_id) or None
        inferred = False
        if name is None:
            # **Somebody already named is speaking, and this is the same voice.** The energy path
            # hears a track it cannot name at the moment Meet's caption or indicator names the
            # person talking — so a live run reported "dev Choudhary and Someone" as two
            # simultaneous speakers when there was one person in the room saying one sentence.
            # Adopting the open turn's name merges them; requiring *exactly one* named turn is what
            # keeps it from guessing when two people genuinely overlap.
            name = self._only_open_name()
            inferred = name is not None
        if name is None:
            name = self._only_other()
            inferred = name is not None
        track_id = str(body.get("trackId") or "").strip()
        if not track_id:
            # Nothing to key an open turn on. Synthesised from whatever identity there is, so
            # a page that omits it still produces coherent turns rather than being dropped.
            track_id = page_id or (f"name:{name.casefold()}" if name else "")
        if not track_id:
            return False

        source = str(body.get("source") or SOURCE_AUDIO)
        speaking = bool(body.get("speaking"))

        if self._is_self(name):
            # The avatar's own audio cannot enter the tap at all — the WebRTC hook is
            # inbound-only — so this can only be the DOM indicator on our own tile. Counting it
            # would have the avatar report itself as the speaker for as long as it talks.
            self._ignored += 1
            return False

        self._events += 1
        now_us = self._clock.now_us()
        if speaking:
            return self._start(
                track_id,
                page_id=page_id,
                name=name,
                source=source,
                now_us=now_us,
                inferred=inferred,
            )
        return self._stop(track_id, now_us=now_us)

    def _only_other(self) -> str | None:
        """The single other participant, when there is exactly one.

        **Attribution by elimination, and it is the answer in the case that matters most.** An
        interview is two people: the avatar and the candidate. If exactly one other person is in
        the meeting, whoever is speaking is that person — no DOM reading, no stream mapping, and no
        dependence on markup Meet is free to change. It is also the case the observation paths are
        *least* likely to cover, because Meet renders a single remote participant differently from
        a grid.

        Returns ``None`` for zero others (nobody to attribute to) and for two or more (naming one
        would be a guess, and a confident wrong name is worse than "Someone").

        **And ``None`` when the one remaining "other" looks like us**, which is not a hypothetical:
        a live run attributed speech to "Backend Services" — the avatar's own account name, which
        self-detection had failed to recognise, leaving the avatar as the only "other" in the room.
        The avatar's audio cannot reach the tap at all (the WebRTC hook is inbound-only), so any
        speech that arrives is by definition *not* ours; crediting it to our own name is the one
        output worse than "Someone", because it is confident and wrong. Checked here as well as at
        the roster, because the two failures compound: if self-detection is broken, elimination
        must fail closed rather than amplify it.
        """
        if len(self._others) != 1:
            return None
        candidate = self._others[0]
        return None if self._is_self(candidate) else candidate

    def _only_open_name(self) -> str | None:
        """The name of the one person currently known to be speaking, if there is exactly one.

        The correlation between the two halves of this feature: energy says *when* with no name, the
        DOM and captions say *who*. When only one named turn is open, an unnamed voice arriving is
        overwhelmingly that person rather than a second, silent-to-the-page speaker — and reporting
        them separately means the agent is told two people are talking when one is.

        ``None`` when nobody named is open, or when more than one is: with two named speakers the
        unnamed voice could be either, and a wrong name is worse than none.
        """
        names = {turn.display_name for turn in self._open.values() if turn.display_name}
        return next(iter(names)) if len(names) == 1 else None

    def _adopt_unnamed(self, *, name: str | None, page_id: str, now_us: int) -> _Turn | None:
        """The anonymous turn this name belongs to — the mirror of ``_only_open_name``.

        **The half that was missing, and the live run it cost.** Energy hears a voice the instant
        it starts and cannot name it; Meet's caption names that voice a second or two *later*,
        once its own transcription has settled. ``_only_open_name`` handles the arrival order
        where the name is already known — but the common order is the other one, and it produced
        two turns for one person saying one sentence:

            meet_speaker.started  attributed=False participant=Someone       source=audio
            meet_speaker.started  attributed=True  participant=dev Choudhary source=dom
            meet_speaker.stopped  participant=Someone        seconds=2.4
            meet_speaker.stopped  participant=dev Choudhary  seconds=2.1

        The agent was then told "Someone is speaking right now" for the whole first half of every
        remark — and asked who was talking, answered with the only name it had, which belonged to
        somebody who had been *typing*. So a name arriving over one voice claims the anonymous
        turn already in progress rather than standing beside it.

        Two windows, both narrow:

        * an unattributed turn **still open** — the energy path is hearing this very voice;
        * an unattributed turn closed within the merge gap — the same pause-and-continue this
          class already treats as one turn, because Meet's caption settles *after* the speaker
          has stopped for a moment.

        Fails closed on ambiguity, which is the rule the rest of this class is built on: exactly
        one anonymous turn, and no *named* turn open beside it. Two people genuinely talking over
        each other must read as two people, and a wrong confident name is the one output worse
        than "Someone".
        """
        if not name:
            return None
        if any(turn.display_name for turn in self._open.values()):
            # Somebody named is already speaking, so an anonymous voice could be a third person.
            return None

        unnamed = {id(turn): turn for turn in self._open.values() if turn.display_name is None}
        if len(unnamed) == 1:
            return self._claim(next(iter(unnamed.values())), name=name, page_id=page_id)

        if unnamed or not self._merge_gap_us:
            # More than one anonymous voice is open: which of them this name belongs to is a
            # guess, and the caller opens a turn of its own instead.
            return None

        # Nothing open. Meet's caption lands after the speaker has paused, so the turn this names
        # has usually just closed — the same gap ``_resume`` treats as punctuation rather than an
        # ending. Only the most recent one, and only if it is genuinely recent.
        for turn in reversed(self._turns):
            if turn.display_name is not None or turn.ended_us is None:
                continue
            if now_us - turn.ended_us > self._merge_gap_us:
                return None
            self._stopped_at.pop(turn.identity, None)
            turn.ended_us = None
            turn.ended_at = None
            return self._claim(turn, name=name, page_id=page_id)
        return None

    def _claim(self, turn: _Turn, *, name: str, page_id: str) -> _Turn:
        """Put a name on an anonymous turn, and move its identity with it.

        The identity has to move for the same reason ``_backfill_names`` moves it: it is what a
        later turn from the same person merges against, and leaving it keyed on a track id would
        have the next remark stand beside this one rather than continuing it.
        """
        self._stopped_at.pop(turn.identity, None)
        self._improve(turn, page_id=page_id, name=name)
        # Inferred, not observed: the page named a voice, and *that this is the same voice* is
        # this class's conclusion. Surfaced so the API and the brief can say so.
        turn.inferred = True
        turn.identity = _identity(page_id=turn.page_id or "", name=name, track_id="")
        logger.info(
            "meet_speaker.identified",
            participant=turn.label,
            note="a name arrived over a voice that was already being heard unattributed",
        )
        return turn

    def _start(
        self,
        track_id: str,
        *,
        page_id: str,
        name: str | None,
        source: str,
        now_us: int,
        inferred: bool = False,
    ) -> bool:
        identity = _identity(page_id=page_id, name=name, track_id=track_id)

        existing = self._open.get(track_id)
        if existing is not None:
            # Already open for this track. The page only repeats itself when it has learned
            # who the track belongs to, so this is a rename — the case that decides whether the
            # first sentence of a meeting is attributed to a person or to a track id.
            return self._rename(track_id, existing, identity=identity, page_id=page_id, name=name)

        already = self._open_for(identity)
        if already is not None:
            # The other observer already has this person speaking. Attaching to their turn is
            # what stops energy and Meet's indicator becoming two speakers — see ``_Turn.refs``.
            self._open[track_id] = already
            already.refs += 1
            self._improve(already, page_id=page_id, name=name)
            return True

        merged = self._resume(identity, now_us=now_us)
        if merged is not None:
            self._open[track_id] = merged
            merged.refs += 1
            self._improve(merged, page_id=page_id, name=name)
            return True

        adopted = self._adopt_unnamed(name=name, page_id=page_id, now_us=now_us)
        if adopted is not None:
            self._open[track_id] = adopted
            adopted.refs += 1
            return True

        self._sequence += 1
        turn = _Turn(
            identity=identity,
            display_name=name,
            page_id=page_id or None,
            source=source,
            started_us=now_us,
            started_at=datetime.now(UTC),
            inferred=inferred,
            refs=1,
            # Ties on ``started_us`` are not hypothetical: two people cut in on each other
            # inside one sampling interval and the clock reads the same microsecond for both.
            # Arrival order is the only thing that can break that tie, and "who started most
            # recently" has to be a total order or the single-name answer flickers.
            sequence=self._sequence,
        )
        self._append(turn)
        self._open[track_id] = turn
        logger.info(
            "meet_speaker.started",
            participant=turn.label,
            source=source,
            attributed=name is not None,
        )
        return True

    def _stop(self, track_id: str, *, now_us: int) -> bool:
        turn = self._open.pop(track_id, None)
        if turn is None:
            return False
        turn.refs -= 1
        if turn.refs > 0:
            # The other observer still has them speaking. Closing here would end a turn that is
            # visibly still happening — the failure two independent signals invite.
            return False
        turn.ended_us = now_us
        turn.ended_at = datetime.now(UTC)
        self._stopped_at[turn.identity] = now_us
        logger.debug(
            "meet_speaker.stopped",
            participant=turn.label,
            seconds=round(turn.duration_us() / 1_000_000, 1),
        )
        return True

    def _rename(
        self, track_id: str, turn: _Turn, *, identity: str, page_id: str, name: str | None
    ) -> bool:
        if identity == turn.identity:
            return self._improve(turn, page_id=page_id, name=name)

        other = self._open_for(identity)
        if other is not None and other is not turn:
            # This track turns out to belong to somebody who is already speaking — the other
            # observer got there first. Fold into their turn rather than reporting one person
            # twice, and discard the placeholder, whose whole interval is inside theirs.
            self._open[track_id] = other
            other.refs += 1
            turn.refs -= 1
            if turn.refs <= 0 and turn.display_name is None:
                self._discard(turn)
            return True

        self._stopped_at.pop(turn.identity, None)
        turn.identity = identity
        self._improve(turn, page_id=page_id, name=name)
        logger.info("meet_speaker.identified", participant=turn.label)
        return True

    def _improve(self, turn: _Turn, *, page_id: str, name: str | None) -> bool:
        """Fill in an identity detail the turn did not have. Never overwrites a name with None."""
        changed = False
        if name and name != turn.display_name:
            turn.display_name = name
            changed = True
        if page_id and page_id != turn.page_id:
            turn.page_id = page_id
            changed = True
        return changed

    def _resume(self, identity: str, *, now_us: int) -> _Turn | None:
        """The turn this identity was in the middle of, if the gap was only a pause."""
        if not self._merge_gap_us:
            return None
        stopped = self._stopped_at.get(identity)
        if stopped is None or now_us - stopped > self._merge_gap_us:
            return None
        for turn in reversed(self._turns):
            if turn.identity == identity and turn.ended_us is not None:
                turn.ended_us = None
                turn.ended_at = None
                self._stopped_at.pop(identity, None)
                return turn
        return None

    def _open_for(self, identity: str) -> _Turn | None:
        for turn in self._open.values():
            if turn.identity == identity:
                return turn
        return None

    def _append(self, turn: _Turn) -> None:
        self._turns.append(turn)
        if len(self._turns) > _MAX_TURNS:
            # Oldest first: this answers "who is talking and who just talked". An open turn is
            # never dropped, because dropping it would strand a reference in ``_open``.
            for index, candidate in enumerate(self._turns):
                if candidate.ended_us is not None:
                    del self._turns[index]
                    break

    def _discard(self, turn: _Turn) -> None:
        # Suppressed rather than checked: the turn was appended by ``_start``, so its absence is
        # impossible — and a bookkeeping method called from the read loop is the wrong place to
        # learn otherwise.
        with contextlib.suppress(ValueError):
            self._turns.remove(turn)

    def observe_roster(self, roster: MeetRoster) -> None:
        """Learn names from the roster stream that already flows. Never raises.

        **This is what makes attribution work when the page can only see an id.** A participant
        tile carries ``data-participant-id`` reliably and an ``aria-label`` only sometimes, so
        the page reports the id and the name is resolved here — against a roster the connector
        was already receiving, at no cost to the page and none to the media path.
        """
        try:
            self.observe_self_name(roster.self_name)
            for participant in roster.participants:
                if participant.is_self:
                    self.observe_self_name(participant.display_name)
                    continue
                if participant.page_id and participant.display_name:
                    self._names_by_id[participant.page_id] = participant.display_name
            # Everyone but us who could actually be the voice — named, deduped — which is what
            # ``_only_other`` eliminates against. Recorded from the roster rather than counted from
            # the tracker's own turns, because somebody who has not spoken yet is still in the room
            # and still makes the count two.
            #
            # **``could_be_speaking`` rather than ``others``, and that is what makes elimination
            # work in a room of two.** Somebody whose microphone Meet says is off is not the voice
            # being heard, so a call with one muted participant and one unmuted one has exactly one
            # candidate — the case elimination used to abandon. A participant whose mute state
            # could not be read stays a candidate, so an unreadable label costs a name and never
            # invents one.
            others: list[str] = []
            for participant in roster.could_be_speaking:
                name = _clean(participant.display_name)
                if not name or self._is_self(name):
                    continue
                if not any(name.casefold() == known.casefold() for known in others):
                    others.append(name)
            self._others = tuple(others)
            self._backfill_names()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("meet_speaker.roster_failed", error=str(exc))

    def _backfill_names(self) -> None:
        """Name turns recorded before the roster explained who their id belonged to.

        Retroactive on purpose: somebody who spoke in the first seconds of the call would
        otherwise be "an unidentified participant" in the history for the rest of the meeting,
        even though we now know exactly who they were.
        """
        only = self._only_other()
        for turn in self._turns:
            if turn.display_name is not None:
                continue
            if turn.page_id:
                name = self._names_by_id.get(turn.page_id)
                if name:
                    turn.display_name = name
                    continue
            if only is not None:
                # Named by elimination once the roster settled. This is the common repair: the
                # first turn of a meeting is often heard before the roster has been read at all,
                # and in a two-person call there is exactly one person it can have been.
                turn.display_name = only
                turn.inferred = True
            if turn.display_name is not None:
                # The identity moves with the name, so a later turn for the same person merges
                # with this one instead of standing beside it under a track id — and two tracks
                # inferred to the same person collapse into one turn rather than two.
                previous, turn.identity = turn.identity, _identity(
                    page_id=turn.page_id or "", name=turn.display_name, track_id=""
                )
                if previous in self._stopped_at:
                    self._stopped_at[turn.identity] = self._stopped_at.pop(previous)

    def observe_self_name(self, name: str | None) -> None:
        """Add a name that means "this is us". Never raises; called from the read loop."""
        cleaned = _clean(str(name or ""))
        if not cleaned:
            return
        if any(cleaned.casefold() == known.casefold() for known in self._self_names):
            return
        self._self_names = (*self._self_names, cleaned)

    def reset(self) -> None:
        """Forget who is *currently* speaking, keeping the history.

        Called when the browser rejoins: the page that reported those turns is gone, so no stop
        edge for them will ever arrive and they would otherwise be "speaking" forever. The turns
        themselves are closed rather than deleted — they did happen.
        """
        now_us = self._clock.now_us()
        for turn in self._open.values():
            if turn.ended_us is None:
                turn.ended_us = now_us
                turn.ended_at = datetime.now(UTC)
        self._open.clear()
        self._stopped_at.clear()
        # Nobody was observed stopping — the page just vanished — so these turns must not hold
        # the floor. "We do not know" is the honest answer after a rejoin, and it is also the
        # safe one: a barge-in in the new call must not be attributed to whoever was talking in
        # the old one.
        self._hold_floor_us = now_us

    # -- output ------------------------------------------------------------

    @property
    def events(self) -> int:
        return self._events

    @property
    def ignored(self) -> int:
        """Edges seen and not counted — our own tile, or a payload with nobody in it."""
        return self._ignored

    @property
    def self_names(self) -> tuple[str, ...]:
        return self._self_names

    def current_speaker(self) -> str | None:
        """Who is talking, as one name, or ``None``.

        Read from the media router's inbound leg when somebody barges in, so it is a scan of a
        handful of open turns and nothing else — no I/O, no allocation worth naming, no lock.

        The hold window is what makes this useful rather than merely accurate: speech has gaps
        at every clause boundary, and an answer that flickers to ``None`` between two words
        would attribute half the interruptions in a meeting to nobody.
        """
        current = self._current(self._clock.now_us())
        return current[0] if current else None

    def _current(self, now_us: int) -> tuple[str, ...]:
        speaking: list[tuple[int, int, str]] = []
        seen: set[str] = set()
        for turn in self._open.values():
            if turn.identity in seen:
                continue
            seen.add(turn.identity)
            speaking.append((turn.started_us, turn.sequence, turn.label))
        if not speaking and self._hold_us:
            # Nobody is talking this instant. Somebody who stopped a moment ago still counts as
            # the person speaking — see the docstring above.
            for turn in reversed(self._turns):
                if turn.ended_us is None or turn.ended_us <= self._hold_floor_us:
                    # ``_hold_floor_us`` is what ``reset`` moves. A turn closed by a rejoin was
                    # not observed ending, so holding it would answer a question about a page
                    # that no longer exists.
                    continue
                if now_us - turn.ended_us <= self._hold_us:
                    speaking.append((turn.started_us, turn.sequence, turn.label))
                    break
        speaking.sort(key=lambda item: (-item[0], -item[1]))
        return tuple(label for _, _, label in speaking)

    def snapshot(self) -> SpeakerSnapshot:
        """A stable, ordered view: turns in the order they began."""
        now_us = self._clock.now_us()
        return SpeakerSnapshot(
            turns=tuple(turn.to_record() for turn in self._turns),
            current=self._current(now_us),
            self_name=self._self_names[0] if self._self_names else None,
            events=self._events,
            candidates=self._others,
            now_us=now_us,
        )

    def _is_self(self, name: str | None) -> bool:
        if not name:
            return False
        # **Meet's first person is the avatar**, because the browser is the avatar's presence in the
        # meeting. A live run reported "You" as a participant taking the floor — timed exactly to
        # the avatar's own greeting — and pushed that to the agent as somebody it should yield to.
        if looks_like_self(name):
            return True
        return any(name.casefold() == known.casefold() for known in self._self_names)


@dataclass(slots=True)
class _Turn:
    """Mutable bookkeeping for one turn. Frozen into a ``SpeakerTurn`` on snapshot."""

    identity: str
    display_name: str | None
    page_id: str | None
    source: str
    started_us: int
    started_at: datetime
    ended_us: int | None = None
    ended_at: datetime | None = None
    sequence: int = 0
    """Arrival order, which breaks a tie on ``started_us`` — two people cutting in on each
    other inside one sampling interval read as the same microsecond."""
    inferred: bool = False
    """Whether the name came from elimination rather than from an observation. Recorded because
    the two are different claims and a caller is entitled to know which it has."""
    refs: int = 0
    """How many observers currently have this person speaking. Energy and the DOM indicator
    both report the same turn, and closing on the first stop would end a turn still happening."""

    @property
    def label(self) -> str:
        return self.to_record().label

    def duration_us(self, *, now_us: int | None = None) -> int:
        return self.to_record().duration_us(now_us=now_us)

    def to_record(self) -> SpeakerTurn:
        return SpeakerTurn(
            display_name=self.display_name,
            page_id=self.page_id,
            source=self.source,
            started_us=self.started_us,
            ended_us=self.ended_us,
            started_at=self.started_at,
            ended_at=self.ended_at,
            inferred=self.inferred,
        )


def _identity(*, page_id: str, name: str | None, track_id: str) -> str:
    """The tracker's key for a speaker.

    Id first, because within one meeting it is exact and a name may be shared. Folded name
    next, which is what merges the *same* person seen through two observers that do not agree
    on ids. The track id last, and prefixed, so an unattributed speaker is still a coherent
    turn rather than being merged with every other unattributed one.
    """
    if page_id:
        return f"id:{page_id}"
    if name:
        return f"name:{name.casefold()}"
    return f"track:{track_id}"


def _clean(value: str) -> str:
    """Reduce a label to a name, or to ``""`` when it is not one.

    The roster's cleaner, deliberately reused rather than a whitespace pass of its own. Speaker
    names come from the same place roster names do — a participant tile's ``aria-label`` — so they
    arrive with the same hazards: a status suffix ("Priya Menon, presenting"), a control's label,
    a doubled name, or icon-font glyphs that mean the label was a container's text rather than a
    person's name. The last of those is why this rejects rather than salvages: a speaker called
    "frame_person Reframe visual_effects" would be a wrong answer delivered confidently, and an
    unattributed turn is merely a gap. Rejected here, the id still resolves through the roster.
    """
    return clean_label(value)[:_MAX_NAME_LEN]


def _join(labels: tuple[str, ...]) -> str:
    """Comma-separate with a final "and", the way a person would say them."""
    if not labels:
        return ""
    if len(labels) == 1:
        return labels[0]
    return ", ".join(labels[:-1]) + f" and {labels[-1]}"
