"""Who is speaking now, and who has spoken.

**One signal, and it is a rendering.** Teams marks the active speaker by drawing an animated
ring on their tile and a matching indicator on their roster row; the page holds a candidate
for ``speaker_min_ms`` before believing it and then reports a name. That is a poorer signal
than the Graph connector's — which receives dominant-speaker events with a source id — and a
better-behaved one than the Google Meet connector's, which has to reconcile per-track audio
energy (*when* but never *who*) against a participant tile (*who* but only once Meet has drawn
it). There is no such reconciliation here, and adding the machinery for it would be inventing
ambiguity in order to resolve it.

**What the signal does not give, and how this covers it.** It is a *level*, not a pair of
edges: the page says the floor has moved to somebody and never says that anybody stopped. So
the previous turn is closed here, when the next one opens — and a turn stays open until either
somebody else takes the floor or ``release`` is called, which is what the session does when it
stops. Everything else follows from that one asymmetry.

**Three things are kept from the Meet tracker deliberately.** The hold window, because speech
has gaps at every clause boundary and an answer that flickers to "nobody" between sentences
would attribute half the interruptions in a meeting to no one. The merge gap, because one
person talking through a pause is one turn and not two — without it, "who has been speaking"
answers with the same name forty times. And the rule that ``current_speaker`` is a dictionary
lookup and nothing more, because the media router reads it on the frame that triggers a
barge-in.

Everything here is synchronous, total and non-blocking: ``observe`` is called from the page
server's read loop, which also carries the avatar's voice into the page.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from src.connectors.teams_web.observations import SpeakerEvent
from src.infrastructure.logging import get_logger
from src.services.media.clock import MediaClock

logger = get_logger(__name__)

COMPONENT_NAME = "teams_web_active_speaker"

SOURCE_PAGE = "page"
"""How every turn here is observed. A single value, where the Meet tracker has two, and that
is the point: there is one signal and it is the page's."""

ANONYMOUS = "Someone"
"""What an unattributed speaker is called. The answer has to name somebody, and an honest
placeholder beats a confident guess."""

_MAX_NAME_LEN = 120
_MAX_TURNS = 2_000
"""Ceiling on remembered turns. Past it the *oldest* go: this answers "who is talking and who
just talked", where the newest matter most — the opposite of the attendance ledger's choice,
for the opposite reason."""


@dataclass(frozen=True, slots=True)
class SpeakerTurn:
    """One stretch of one participant holding the floor."""

    display_name: str | None
    user_id: int | None
    source: str
    started_us: int
    ended_us: int | None
    started_at: datetime
    ended_at: datetime | None
    inferred: bool = False
    """True when the name was reached by elimination — exactly one other person was in the
    meeting — rather than read off the page. Kept distinct because they are different claims,
    and a caller that cares should not have to guess which it has."""

    @property
    def is_open(self) -> bool:
        return self.ended_us is None

    @property
    def label(self) -> str:
        """What to call this speaker in an answer. Never empty."""
        if self.display_name:
            return self.display_name
        if self.user_id is not None:
            return f"an unidentified participant ({self.user_id})"
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

    Field-for-field compatible with the Google Meet and Zoom-web snapshots, so ``GET
    /sessions/{id}/speakers`` serves a Teams-web session through the same duck-typed path —
    see ``meeting/attendance.AttendanceSnapshot`` for why that compatibility is a design
    choice rather than a coincidence.
    """

    turns: tuple[SpeakerTurn, ...] = field(default_factory=tuple)
    current: tuple[str, ...] = field(default_factory=tuple)
    """Everyone speaking as of now. **At most one on this connector**, because the page
    reports a single active speaker rather than a set — a tuple because the shared response
    shape carries one, and because reporting a plural honestly is better than a field that
    lies about its cardinality."""

    self_name: str | None = None
    events: int = 0
    """Speaker changes observed. ``0`` is the difference between "nobody has spoken" and "we
    do not know"."""

    candidates: tuple[str, ...] = field(default_factory=tuple)
    """Everyone present who could be the voice, the avatar excluded. Carried so the brief can
    name the field even before the first speaker change arrives."""

    now_us: int = 0
    observed_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def current_speaker(self) -> str | None:
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

        Prose rather than JSON because its destination is a context window. States what is
        *not* known, deliberately: an agent told only that somebody is speaking will answer
        "who is talking?" with a name it invented.
        """
        if not self.events:
            heard = (
                "Nobody has been heard speaking yet — the avatar has not seen anyone marked "
                "as the active speaker."
            )
            return f"{heard} {self._unattributed_guard()}" if self.candidates else heard

        lines: list[str] = []
        if self.current:
            speaker = self.current[0]
            if speaker == ANONYMOUS:
                lines.append(
                    "Somebody is speaking right now, but the avatar cannot tell which "
                    f"participant it is. {self._unattributed_guard()}"
                )
            else:
                # **Stated as the answer to the question that actually gets asked.** Having
                # the fact and connecting it to a first-person question are different things
                # for a model, so the connection is written down rather than left implied —
                # the wording the Meet brief arrived at after an avatar answered "who is
                # speaking?" correctly and "what is my name?" with "I do not know" from one
                # brief, a minute apart.
                lines.append(
                    f"{speaker} is speaking right now. That is the person talking to the "
                    f'avatar, so when they say "I", "me" or "my" they mean {speaker} — '
                    f'asked "what is my name?" or "who am I?", the answer is {speaker}.'
                )
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

        # **Voice only, said out loud, because the two channels get conflated.** This
        # paragraph is built from who was drawn as the active speaker: somebody who has only
        # typed in the chat has never taken the floor and does not appear here. An agent that
        # reads "recent speakers" as "people who have communicated" answers "who was talking
        # to you?" with whoever typed last.
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

        Naming the candidates rather than saying "unknown", because "unknown" invites the
        model to resolve it from whatever else is in the frame — and what is in the frame is a
        chat history with one name in it. The answer *is* one of these people, which is real
        information, and the wrong inference is ruled out explicitly.
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
            f"{who}. Do not assume it is whoever spoke or typed most recently — somebody "
            "typing in the chat is not the voice being heard — and ask who is speaking if the "
            "answer matters."
        )


class TeamsSpeakerTracker:
    """Turns the page's active-speaker observations into a current speaker and a history."""

    __slots__ = (
        "_clock",
        "_events",
        "_hold_us",
        "_ignored",
        "_merge_gap_us",
        "_names_by_id",
        "_open",
        "_others",
        "_self_names",
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
        # How long somebody stays "the current speaker" after the floor moves off them.
        self._hold_us = max(int(hold_ms * 1_000), 0)
        # How long a gap may be before it ends a turn rather than punctuating one.
        self._merge_gap_us = max(int(merge_gap_ms * 1_000), 0)
        self._turns: list[_Turn] = []
        self._open: _Turn | None = None
        self._names_by_id: dict[int, str] = {}
        self._others: tuple[str, ...] = ()
        self._stopped_at: dict[str, int] = {}
        self._self_names: tuple[str, ...] = ()
        self._events = 0
        self._ignored = 0
        for name in self_names:
            self.observe_self_name(name)

    # -- inputs ------------------------------------------------------------

    def observe(self, event: SpeakerEvent) -> bool:
        """Accept one active-speaker observation. Never blocks, never raises.

        Returns True when this changed who the tracker believes is speaking. False covers our
        own account and an observation naming the person already on the floor — neither is an
        error, and neither may reach the page read loop as an exception.
        """
        try:
            return self._observe(event)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("teams_speaker.observe_failed", error=str(exc))
            return False

    def _observe(self, event: SpeakerEvent) -> bool:
        name = _clean(event.display_name or "")
        if not name and event.user_id is not None:
            name = self._names_by_id.get(event.user_id, "")
        inferred = False
        if not name:
            # Elimination. Fails closed at two or more others, because naming one of two would
            # be a guess and a confident wrong name is worse than "Someone".
            only = self._only_other()
            if only is not None:
                name, inferred = only, True

        if self._is_self(name):
            # The avatar is a participant, so the page draws it as the active speaker whenever
            # it talks. Counting that would have the avatar report itself as the speaker for as
            # long as it talks — and, worse, would name it as the person to yield to.
            self._ignored += 1
            return False

        self._events += 1
        now_us = self._clock.now_us()
        identity = _identity(user_id=event.user_id, name=name or None)

        open_turn = self._open
        if open_turn is not None and open_turn.identity == identity:
            # The same person still holding the floor. The page can re-report a continuing
            # turn; treat it as an identity refresh rather than a new turn, so one
            # uninterrupted speech does not read as a dozen.
            self._improve(open_turn, user_id=event.user_id, name=name or None)
            return False

        # The floor has moved. Nothing sends a "stopped", so closing the previous turn is this
        # class's job and this is the only place it happens on a live meeting.
        self._close(now_us=now_us)

        resumed = self._resume(identity, now_us=now_us)
        if resumed is not None:
            self._open = resumed
            self._improve(resumed, user_id=event.user_id, name=name or None)
            return True

        turn = _Turn(
            identity=identity,
            display_name=name or None,
            user_id=event.user_id,
            source=SOURCE_PAGE,
            started_us=now_us,
            started_at=datetime.now(UTC),
            inferred=inferred,
        )
        self._append(turn)
        self._open = turn
        logger.info("teams_speaker.started", participant=turn.label, attributed=bool(name))
        return True

    def observe_participants(self, present: tuple[str, ...]) -> None:
        """Learn who else is in the meeting, from the attendance ledger. Never raises.

        What ``_only_other`` eliminates against, and what the brief names as candidates. Taken
        from the ledger rather than tracked here because the ledger already holds it and two
        accounts of who is present is one account too many.
        """
        try:
            others = [
                name
                for name in (_clean(candidate) for candidate in present)
                if name and not self._is_self(name)
            ]
            deduped: list[str] = []
            for name in others:
                if not any(name.casefold() == known.casefold() for known in deduped):
                    deduped.append(name)
            self._others = tuple(deduped)
            self._backfill_names()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("teams_speaker.participants_failed", error=str(exc))

    def observe_self_name(self, name: str | None) -> None:
        """Add a name that means "this is us". Never raises; called from the read loop."""
        cleaned = _clean(str(name or ""))
        if not cleaned:
            return
        if any(cleaned.casefold() == known.casefold() for known in self._self_names):
            return
        self._self_names = (*self._self_names, cleaned)

    def release(self) -> None:
        """Close whatever turn is open, keeping the history.

        Called when the session stops: no further observation for that turn will ever arrive,
        so it would otherwise be "speaking" forever in whatever the API serves after the
        session ends.
        """
        self._close(now_us=self._clock.now_us())

    # -- internals ---------------------------------------------------------

    def _close(self, *, now_us: int) -> None:
        turn, self._open = self._open, None
        if turn is None or turn.ended_us is not None:
            return
        turn.ended_us = now_us
        turn.ended_at = datetime.now(UTC)
        self._stopped_at[turn.identity] = now_us
        logger.debug(
            "teams_speaker.stopped",
            participant=turn.label,
            seconds=round(turn.duration_us() / 1_000_000, 1),
        )

    def _resume(self, identity: str, *, now_us: int) -> _Turn | None:
        """The turn this person was in the middle of, if the gap was only a pause.

        What makes the history read like a conversation rather than like a waveform: two people
        alternating quickly are two people talking, not forty turns.
        """
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

    def _improve(self, turn: _Turn, *, user_id: int | None, name: str | None) -> None:
        """Fill in an identity detail the turn did not have. Never overwrites a name."""
        if name and name != turn.display_name:
            turn.display_name = name
        if user_id is not None and user_id != turn.user_id:
            turn.user_id = user_id

    def _append(self, turn: _Turn) -> None:
        self._turns.append(turn)
        if len(self._turns) > _MAX_TURNS:
            for index, candidate in enumerate(self._turns):
                if candidate.ended_us is not None:
                    del self._turns[index]
                    break

    def _backfill_names(self) -> None:
        """Name turns recorded before anything explained who they were.

        Retroactive on purpose: somebody who spoke in the first seconds of the call would
        otherwise stay "Someone" in the history for the rest of the meeting, when we now know
        exactly who they were.
        """
        only = self._only_other()
        for turn in self._turns:
            if turn.display_name is not None:
                continue
            if turn.user_id is not None:
                name = self._names_by_id.get(turn.user_id)
                if name:
                    turn.display_name = name
            if turn.display_name is None and only is not None:
                turn.display_name = only
                turn.inferred = True
            if turn.display_name is not None:
                # The identity moves with the name, so a later turn from the same person
                # merges with this one rather than standing beside it under a bare id.
                previous = turn.identity
                turn.identity = _identity(user_id=turn.user_id, name=turn.display_name)
                if previous in self._stopped_at:
                    self._stopped_at[turn.identity] = self._stopped_at.pop(previous)

    def _only_other(self) -> str | None:
        """The single other participant, when there is exactly one and it is not us."""
        if len(self._others) != 1:
            return None
        candidate = self._others[0]
        return None if self._is_self(candidate) else candidate

    def _is_self(self, name: str | None) -> bool:
        if not name:
            return False
        return any(name.casefold() == known.casefold() for known in self._self_names)

    # -- output ------------------------------------------------------------

    @property
    def events(self) -> int:
        return self._events

    @property
    def ignored(self) -> int:
        """Observations seen and not counted — the avatar's own turns."""
        return self._ignored

    @property
    def self_names(self) -> tuple[str, ...]:
        return self._self_names

    def current_speaker(self) -> str | None:
        """Who is talking, as one name, or ``None``.

        Read from the media router's inbound leg when somebody barges in, so it is a couple of
        comparisons and nothing else — no I/O, no lock, no allocation worth naming.
        """
        current = self._current(self._clock.now_us())
        return current[0] if current else None

    def _current(self, now_us: int) -> tuple[str, ...]:
        if self._open is not None:
            return (self._open.label,)
        if not self._hold_us:
            return ()
        # Nobody holds the floor this instant. Somebody who stopped a moment ago still counts
        # as the person speaking — speech has gaps at every clause boundary, and an answer
        # that flickers to "nobody" between two words is the wrong answer to the question that
        # was asked.
        for turn in reversed(self._turns):
            if turn.ended_us is None:
                continue
            if now_us - turn.ended_us <= self._hold_us:
                return (turn.label,)
            break
        return ()

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


@dataclass(slots=True)
class _Turn:
    """Mutable bookkeeping for one turn. Frozen into a ``SpeakerTurn`` on snapshot."""

    identity: str
    display_name: str | None
    user_id: int | None
    source: str
    started_us: int
    started_at: datetime
    ended_us: int | None = None
    ended_at: datetime | None = None
    inferred: bool = False

    @property
    def label(self) -> str:
        return self.to_record().label

    def duration_us(self, *, now_us: int | None = None) -> int:
        return self.to_record().duration_us(now_us=now_us)

    def to_record(self) -> SpeakerTurn:
        return SpeakerTurn(
            display_name=self.display_name,
            user_id=self.user_id,
            source=self.source,
            started_us=self.started_us,
            ended_us=self.ended_us,
            started_at=self.started_at,
            ended_at=self.ended_at,
            inferred=self.inferred,
        )


def _identity(*, user_id: int | None, name: str | None) -> str:
    """The tracker's key for a speaker.

    Folded name first, and on this connector the name is all there is. The id branch is the
    fallback the observation types allow for and the page never populates: a DOM has no
    participant id, so ``SpeakerEvent.user_id`` is always ``None`` here.
    """
    if name:
        return f"name:{name.casefold()}"
    if user_id is not None:
        return f"id:{user_id}"
    return "id:unknown"


def _clean(value: str) -> str:
    """Collapse whitespace and bound the length."""
    return " ".join(str(value or "").split())[:_MAX_NAME_LEN].strip()


def _join(labels: tuple[str, ...]) -> str:
    """Comma-separate with a final "and", the way a person would say them."""
    if not labels:
        return ""
    if len(labels) == 1:
        return labels[0]
    return ", ".join(labels[:-1]) + f" and {labels[-1]}"
