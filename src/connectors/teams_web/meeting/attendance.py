"""Who is in the meeting, who was, and who never came.

**Fed by a diff over a DOM roster**, which is the fundamental difference between this ledger
and the Graph connector's position. Teams reports participant events over Graph to an
application that has been granted access to the call; a guest in a browser gets a list of
names that is re-rendered constantly. ``meeting/observer.py`` turns that level into edges and
hands them here, so this file itself is an event log exactly as the Zoom-web ledger is — the
difference is entirely in what produced the events and how much they can be trusted.

**Identity is the name, and here that is not a choice.** ``user_id`` is always ``None``: a
DOM row carries no participant id, so there is nothing else to key on. The consequence is
worth stating rather than discovering — two people who share a display name are one entry, and
nothing can separate them. The upside, which the Zoom-web ledger chose deliberately and this
one gets for free, is that somebody whose wifi dropped and came back is *one* attendee rather
than two.

**Wall-clock times beside the monotonic ones**, because the monotonic value orders events and
measures durations, and "when did Priya join" needs a number a person can read.

Every method is synchronous, total and non-blocking, because ``observe`` is called from the
page server's read loop — the loop that also carries the avatar's voice into the page. A bug
in a bookkeeping feature must never be able to stall that or tear it down.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import perf_counter_ns

from src.connectors.teams_web.observations import ParticipantEvent
from src.infrastructure.logging import get_logger

logger = get_logger(__name__)

COMPONENT_NAME = "teams_web_attendance"

_MAX_NAME_LEN = 120
_MAX_TRACKED = 500
"""Ceiling on distinct people remembered for one meeting.

A guard against a pathological roster rather than a product limit — each entry is a few
hundred bytes. Past it, new arrivals are dropped so that a long session's memory stays flat
and the people who were actually there stay intact."""


@dataclass(frozen=True, slots=True)
class AttendanceRecord:
    """One person, and everything known about their time in the meeting."""

    display_name: str | None
    user_id: int | None
    first_seen_us: int | None
    """Monotonic time of their arrival. ``None`` for an invitee who never joined — there is
    no moment to record."""
    last_seen_us: int | None
    first_seen_at: datetime | None
    last_seen_at: datetime | None
    present: bool
    was_invited: bool
    rejoins: int = 0
    """How many times they came back after leaving. Worth keeping separately from ``present``:
    "Priya kept dropping" is a different answer to "Priya left"."""

    @property
    def never_joined(self) -> bool:
        """Invited, and never once seen in the meeting."""
        return self.was_invited and self.first_seen_us is None

    @property
    def label(self) -> str:
        """What to call this person in an answer. Never empty."""
        if self.display_name:
            return self.display_name
        if self.user_id is not None:
            return f"an unnamed participant ({self.user_id})"
        return "an unnamed participant"

    def duration_us(self, *, now_us: int | None = None) -> int:
        """How long they were in the meeting, in microseconds.

        **Approximate at both ends, and the reason is the signal rather than the arithmetic.**
        This can only say somebody was there when the page last looked, so it under-reports by
        up to one observe interval at each end, and a departure is additionally held for the
        leave-grace window before it is believed. The Graph connector's equivalent would be
        exact. Read this as "about how long", which is what it is asked for.
        """
        if self.first_seen_us is None:
            return 0
        end = self.last_seen_us or self.first_seen_us
        if self.present and now_us is not None:
            end = max(end, now_us)
        return max(end - self.first_seen_us, 0)


@dataclass(frozen=True, slots=True)
class AttendanceSnapshot:
    """An immutable answer to "who was in this meeting".

    Returned rather than exposing the ledger, so a caller serialising this over HTTP cannot
    observe it changing underneath them mid-response.

    **Field-for-field compatible with the Google Meet and Zoom-web snapshots**, and that is
    load-bearing rather than incidental: ``MeetingService.attendance_snapshot`` and
    ``api/routers/participants.py`` read all of them duck-typed, so ``GET
    /sessions/{id}/participants`` serves a Teams-web session with no change to either.
    Widening ``ConnectorSession`` instead would oblige every connector to answer this, which
    is the trade doc 003 §0 rejects.
    """

    records: tuple[AttendanceRecord, ...] = field(default_factory=tuple)
    self_name: str | None = None
    scans: int = 0
    """Roster changes observed. ``0`` is the difference between "nobody is here" and "we do
    not know yet", which is why it is carried rather than derived from ``records``."""
    observed_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def present(self) -> tuple[AttendanceRecord, ...]:
        return tuple(r for r in self.records if r.present)

    @property
    def departed(self) -> tuple[AttendanceRecord, ...]:
        return tuple(r for r in self.records if not r.present and r.first_seen_us is not None)

    @property
    def attended(self) -> tuple[AttendanceRecord, ...]:
        return tuple(r for r in self.records if r.first_seen_us is not None)

    @property
    def invited(self) -> tuple[AttendanceRecord, ...]:
        return tuple(r for r in self.records if r.was_invited)

    @property
    def never_joined(self) -> tuple[AttendanceRecord, ...]:
        return tuple(r for r in self.records if r.never_joined)

    @property
    def has_invite_list(self) -> bool:
        """Whether an invite list was supplied at all.

        Load-bearing for honesty: without one, "nobody is missing" is unknowable rather than
        true, and an agent asked who failed to show up should say so.
        """
        return any(r.was_invited for r in self.records)

    def agent_context(self) -> str:
        """A plain-language brief for the avatar agent.

        Prose rather than JSON because its destination is a context window, where "Aarav
        Sharma and Priya Menon are in the meeting" is worth more than a field called
        ``present`` holding a list. Deliberately states what is *not* known: an agent told
        only who is present will answer "who was invited?" and be wrong.
        """
        if not self.scans:
            return (
                "Meeting attendance is not known yet — the avatar has not been able to read "
                "the participant list."
            )

        lines: list[str] = []

        here = self.present
        if here:
            lines.append(f"Currently in the meeting ({len(here)}): {_join_names(here)}.")
        else:
            lines.append("Nobody else is currently in the meeting — the avatar is alone.")

        if len(here) == 1:
            # **Stated outright, because the agent demonstrably will not infer it.** A live
            # run on the Zoom-web connector gave the agent exactly the line above and it still
            # answered "I'm sorry, but I don't know your name" when asked by voice — while the
            # same question typed into the chat was answered correctly, because a chat message
            # arrives with its sender attached and a spoken turn does not. The avatar hears one
            # mixed stream carrying no attribution at all, which is equally true here.
            #
            # With exactly one other participant the inference is not a guess — there is
            # nobody else the voice could belong to — so it is the brief's job to make it, not
            # the agent's. At two or more this stays silent, because naming one of several
            # would be a guess and greeting the wrong person by name is worse than not
            # knowing.
            only = here[0].label
            lines.append(
                f'"{only}" is the only other person here, so anyone speaking to the avatar '
                f'right now is "{only}" — answer with that name when asked who they are.'
            )

        gone = self.departed
        if gone:
            lines.append(f"Was in the meeting but has left ({len(gone)}): {_join_names(gone)}.")

        if self.has_invite_list:
            missing = self.never_joined
            if missing:
                lines.append(f"Invited but never joined ({len(missing)}): {_join_names(missing)}.")
            else:
                lines.append("Everyone on the invite list joined at some point.")
        else:
            lines.append(
                "No invite list is available for this meeting, so who was invited but did "
                "not attend is unknown."
            )

        if self.self_name:
            lines.append(f'The avatar itself appears in the meeting as "{self.self_name}".')

        return " ".join(lines)


def _join_names(records: tuple[AttendanceRecord, ...]) -> str:
    """Comma-separate labels with a final "and", the way a person would say them."""
    labels = [r.label for r in records]
    if len(labels) == 1:
        return labels[0]
    return ", ".join(labels[:-1]) + f" and {labels[-1]}"


class TeamsAttendanceLedger:
    """Accumulates who was in the meeting, from roster changes the page observed.

    Not a component in the health report and not on any media path: it holds no task, opens
    nothing, and every method returns rather than raising — because ``observe`` is called from
    the page read loop.
    """

    __slots__ = ("_entries", "_events", "_over_capacity", "_self_names")

    def __init__(
        self, *, invitees: tuple[str, ...] = (), self_names: tuple[str, ...] = ()
    ) -> None:
        self._entries: dict[str, _Entry] = {}
        self._self_names: tuple[str, ...] = ()
        self._events = 0
        self._over_capacity = 0
        # Before the invitees, so a self-name that also appears on the invite list cannot
        # create an entry that then never matches anybody.
        for name in self_names:
            self.observe_self_name(name)
        self.seed_invitees(invitees)

    @property
    def events(self) -> int:
        return self._events

    @property
    def present_names(self) -> tuple[str, ...]:
        """Everyone in the meeting right now, named, avatar excluded.

        Read by the speaker tracker, the transcript and the chat source, which use it for the
        same thing: the "is there exactly one other person here" answer that lets an
        unattributed voice, line or message be named by elimination. A plain method call
        rather than a listener, because it is read at the moment it is needed.
        """
        return tuple(
            entry.display_name
            for entry in self._entries.values()
            if entry.present and entry.display_name
        )

    # -- inputs ------------------------------------------------------------

    def seed_invitees(self, names: tuple[str, ...] | list[str]) -> int:
        """Record who was invited, from a source outside the meeting.

        The authoritative source is the calendar event the meeting came from. Taking it as
        data means this works without reading anything out of Teams' UI — and it is the only
        way this connector can answer "who never turned up", since a roster shows only the
        people who did.

        Idempotent and additive: seeding twice with overlapping lists marks each person once,
        and seeding after people have joined marks the existing entries rather than creating
        duplicates. Returns how many names were newly marked as invited.
        """
        marked = 0
        for raw in names or ():
            name = _clean_name(str(raw or ""))
            if not name:
                continue
            key = _key(user_id=None, display_name=name)
            entry = self._entries.get(key)
            if entry is None:
                if len(self._entries) >= _MAX_TRACKED:
                    self._over_capacity += 1
                    continue
                entry = _Entry(display_name=name)
                self._entries[key] = entry
            if not entry.was_invited:
                entry.was_invited = True
                marked += 1
        if marked:
            logger.info(
                "teams_attendance.invitees_seeded", invited=marked, tracked=len(self._entries)
            )
        return marked

    def observe(self, event: ParticipantEvent) -> None:
        """Fold one arrival or departure into the ledger. Never raises, never blocks."""
        try:
            self._observe(event)
        except Exception as exc:  # pragma: no cover - defensive
            # Swallowed on purpose: this runs on the page read loop, so an unexpected payload
            # must cost an attendance update and nothing else. Losing a name is a bookkeeping
            # gap; propagating would drop the meeting's audio.
            logger.warning("teams_attendance.observe_failed", error=str(exc))

    def _observe(self, event: ParticipantEvent) -> None:
        name = _clean_name(event.display_name or "")
        if self._is_self(name):
            # The avatar is a participant like anybody else and appears in its own roster.
            # Counting it would make every answer wrong by one — "there are 3 people here"
            # when there are two and a bot.
            return

        self._events += 1
        key = _key(user_id=event.user_id, display_name=name)
        entry = self._entries.get(key)

        if entry is None:
            if not event.joined:
                # A departure for somebody never seen arriving. Recording it would put
                # somebody in "was here and left" who, as far as this ledger can honestly
                # say, was never observed at all.
                logger.info("teams_attendance.left_unknown", participant=name or "(unnamed)")
                return
            if len(self._entries) >= _MAX_TRACKED:
                self._over_capacity += 1
                if self._over_capacity == 1:
                    logger.warning(
                        "teams_attendance.capacity_reached",
                        tracked=len(self._entries),
                        note="further new participants are not recorded for this session",
                    )
                return
            entry = _Entry(display_name=name or None)
            self._entries[key] = entry

        now_us, now_at = event.at_us or _monotonic_us(), datetime.now(UTC)

        if name:
            # The event's name is the better label whenever it has one: an entry created by
            # ``seed_invitees`` may be holding a calendar address.
            entry.display_name = name
        if event.user_id is not None:
            entry.user_id = event.user_id

        if not event.joined:
            if entry.present:
                entry.present = False
                entry.last_seen_us = now_us
                entry.last_seen_at = now_at
                logger.info(
                    "teams_attendance.left",
                    participant=entry.display_name or "(unnamed)",
                    stayed_s=round(entry.duration_us(now_us=now_us) / 1_000_000, 1),
                )
            return

        if entry.first_seen_us is None:
            entry.first_seen_us = now_us
            entry.first_seen_at = now_at
            logger.info(
                "teams_attendance.joined",
                participant=entry.display_name or "(unnamed)",
                present=sum(1 for e in self._entries.values() if e.present) + 1,
            )
        elif not entry.present:
            # Back after a gap. Counted rather than overwriting ``first_seen``, so the ledger
            # can still say how long they have been involved overall.
            entry.rejoins += 1
            logger.info(
                "teams_attendance.rejoined",
                participant=entry.display_name or "(unnamed)",
                rejoins=entry.rejoins,
            )

        entry.present = True
        entry.last_seen_us = now_us
        entry.last_seen_at = now_at

    def observe_self_name(self, name: str | None) -> None:
        """Learn a name that means "this is us".

        Retroactive: a name learned late evicts the entry it created earlier, so the avatar
        does not stay permanently listed as a guest because its own row was read before the
        session knew what it had joined as.
        """
        cleaned = _clean_name(str(name or ""))
        if not cleaned:
            return
        if any(cleaned.lower() == known.lower() for known in self._self_names):
            return
        self._self_names = (*self._self_names, cleaned)
        self._entries.pop(_key(user_id=None, display_name=cleaned), None)

    # -- output ------------------------------------------------------------

    def snapshot(self) -> AttendanceSnapshot:
        """A stable, ordered view: people in arrival order, never-joined invitees last.

        That is the order the meeting happened in, which is what makes the rendered brief read
        like an account of it rather than a set.
        """
        records = tuple(
            entry.to_record() for _, entry in sorted(self._entries.items(), key=_ordering)
        )
        return AttendanceSnapshot(
            records=records,
            self_name=self._self_names[0] if self._self_names else None,
            scans=self._events,
        )

    def _is_self(self, name: str) -> bool:
        if not name:
            return False
        return any(name.lower() == known.lower() for known in self._self_names)


@dataclass(slots=True)
class _Entry:
    """Mutable bookkeeping for one person. Frozen into a record on snapshot."""

    display_name: str | None = None
    user_id: int | None = None
    first_seen_us: int | None = None
    last_seen_us: int | None = None
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    present: bool = False
    was_invited: bool = False
    rejoins: int = 0

    def duration_us(self, *, now_us: int | None = None) -> int:
        return self.to_record().duration_us(now_us=now_us)

    def to_record(self) -> AttendanceRecord:
        return AttendanceRecord(
            display_name=self.display_name,
            user_id=self.user_id,
            first_seen_us=self.first_seen_us,
            last_seen_us=self.last_seen_us,
            first_seen_at=self.first_seen_at,
            last_seen_at=self.last_seen_at,
            present=self.present,
            was_invited=self.was_invited,
            rejoins=self.rejoins,
        )


def _ordering(item: tuple[str, _Entry]) -> tuple[int, int, str]:
    """Sort key: people in arrival order, then never-joined invitees by name."""
    _, entry = item
    if entry.first_seen_us is None:
        return (1, 0, (entry.display_name or "").lower())
    return (0, entry.first_seen_us, (entry.display_name or "").lower())


def _key(*, user_id: int | None, display_name: str) -> str:
    """The ledger's identity for a person.

    Folded name first. On this connector the name is all there is — see the module docstring
    — and the prefix is kept so a name that happens to look like an id cannot collide with
    one, which matters the day a Teams build starts exposing one.
    """
    name = _clean_name(display_name)
    if name:
        return f"name:{name.casefold()}"
    return f"id:{user_id}"


def _clean_name(value: str) -> str:
    """Collapse whitespace and bound the length."""
    return " ".join(str(value or "").split())[:_MAX_NAME_LEN].strip()


def _monotonic_us() -> int:
    """A monotonic microsecond stamp, for an event that arrived without one.

    Not taken from ``MediaClock``: this is not media, and a ledger that outlives a media clock
    reset should not have its history reordered by one.
    """
    return perf_counter_ns() // 1_000
