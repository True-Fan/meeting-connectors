"""Who was in the meeting, and who was invited and never came.

**Why this is a ledger and not a roster.** ``meeting/participants.py`` answers "who is here
*now*" — it is replaced wholesale on every scan, so the moment somebody leaves, the fact that
they were ever present is gone. The questions this exists to answer are historical: *who is
here*, *who was here*, *who never showed up*. None of them can be answered from a point-in-time
view, so something has to accumulate, and this is it.

**Fed from the roster stream that already exists, deliberately.** This module adds no DOM
scanning, no new page observer, and no new wire message. It subscribes to
``ChromiumBridge.add_roster_listener``, which is already fired on every roster change by the
existing ``PARTICIPANTS`` handler. That is not a shortcut — it is the only safe design. The
page's scans run on **the renderer's main thread**, the same one encoding the camera track and
posting the avatar's PCM into the playout worklet, and ``js/bridge.js`` carries a 250 ms scan
floor precisely because over-scanning it once showed up as "the avatar is slow to answer". A
feature about *names* has no business costing audio latency, so it reads a stream that is
already flowing and does its work in Python.

**Identity is the name, not the id, and that is a considered inversion.** ``parse_roster``
dedupes on ``page_id or name.lower()`` because within one scan the id is the stronger key.
Across *time* it is the weaker one: Meet mints a fresh ``data-participant-id`` when somebody
rejoins, and renders the same person with an id on their video tile and no id at all in a
roster row. Keying on the id would report one person who dropped off wifi as two attendees, and
the same person on two selectors as two more. Keying on the folded name merges all of those,
and the cost — two genuinely different people sharing a display name collapsing into one entry
— is both rarer and less misleading than the alternative. Entries without any name at all fall
back to the id, because an anonymous tile is still a body in the room.

**Wall-clock times, unlike everything else here.** The rest of this connector times things on
``MediaClock``, which is monotonic and unanchored — correct for media, useless for "when did
Priya join". So each entry carries a UTC timestamp alongside the monotonic one: the monotonic
value orders events and measures durations, and the wall clock is what a person can read.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import perf_counter_ns

from src.connectors.google_meet.meeting.participants import MeetRoster
from src.infrastructure.logging import get_logger

logger = get_logger(__name__)

COMPONENT_NAME = "google_meet_attendance"

_MAX_NAME_LEN = 120
_MAX_TRACKED = 500
"""Ceiling on distinct people remembered for one meeting.

A guard against a pathological DOM rather than a product limit — a Meet call caps out far
below this, and the entries are a few hundred bytes each. Reached only if the page were to
report a stream of junk names, in which case dropping new ones keeps a long session's memory
flat and leaves the people who were actually there intact."""


@dataclass(frozen=True, slots=True)
class AttendanceRecord:
    """One person, and everything known about their time in the meeting."""

    display_name: str | None
    page_id: str | None
    first_seen_us: int | None
    """Monotonic media-clock time of the first scan they appeared in. ``None`` for an invitee
    who never joined — there is no moment to record."""
    last_seen_us: int | None
    first_seen_at: datetime | None
    last_seen_at: datetime | None
    present: bool
    """Whether they were in the roster as of the most recent scan."""
    was_invited: bool
    """Whether they appeared on the invite list this session was seeded with."""
    rejoins: int = 0
    """How many times they came back after leaving. Worth keeping separately from
    ``present``: "Priya kept dropping" is a different answer to "Priya left"."""

    @property
    def never_joined(self) -> bool:
        """Invited, and never once seen in the roster."""
        return self.was_invited and self.first_seen_us is None

    @property
    def label(self) -> str:
        """What to call this person in an answer. Never empty."""
        if self.display_name:
            return self.display_name
        if self.page_id:
            return f"an unnamed participant ({self.page_id[:12]})"
        return "an unnamed participant"

    def duration_us(self, *, now_us: int | None = None) -> int:
        """How long they were in the meeting, in microseconds.

        Measured first-seen to last-seen, which under-reports by up to one scan interval at
        each end and is the honest bound: the page can only say somebody was there when it
        looked. For a participant still present, ``now_us`` extends the window to the current
        moment rather than freezing it at the last scan.
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

    Returned rather than exposing the ledger itself, so a caller reading this over HTTP
    cannot observe it changing underneath them mid-serialisation.
    """

    records: tuple[AttendanceRecord, ...] = field(default_factory=tuple)
    self_name: str | None = None
    scans: int = 0
    """How many distinct rosters were observed. ``0`` means the page has not reported one
    yet, which is the difference between "nobody is here" and "we do not know" — and the
    reason this is on the snapshot rather than inferred from an empty record list."""
    observed_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def present(self) -> tuple[AttendanceRecord, ...]:
        """Everyone in the meeting as of the last scan, the avatar excluded."""
        return tuple(r for r in self.records if r.present)

    @property
    def departed(self) -> tuple[AttendanceRecord, ...]:
        """Everyone who was here and has since left."""
        return tuple(r for r in self.records if not r.present and r.first_seen_us is not None)

    @property
    def attended(self) -> tuple[AttendanceRecord, ...]:
        """Everyone ever seen in the meeting, present or not."""
        return tuple(r for r in self.records if r.first_seen_us is not None)

    @property
    def invited(self) -> tuple[AttendanceRecord, ...]:
        return tuple(r for r in self.records if r.was_invited)

    @property
    def never_joined(self) -> tuple[AttendanceRecord, ...]:
        """Invited, never seen. Empty when the session was never seeded with an invite list —
        which is not the same as everyone having turned up, and why ``has_invite_list``
        exists."""
        return tuple(r for r in self.records if r.never_joined)

    @property
    def has_invite_list(self) -> bool:
        """Whether an invite list was supplied at all.

        Load-bearing for honesty: without it, "nobody is missing" is unknowable rather than
        true, and an agent asked who failed to show up should say so instead of answering
        "nobody".
        """
        return any(r.was_invited for r in self.records)

    def agent_context(self) -> str:
        """A plain-language brief for the avatar agent.

        Rendered here rather than in the API layer because it is the same sentence regardless
        of who asks, and rendered as *prose* rather than JSON because its destination is an
        LLM's context window — where "Aarav Sharma and Priya Menon are in the meeting" is
        worth more than a field called ``present`` holding a list.

        Deliberately states what is *not* known. An agent that is told only who is present
        will answer "who was invited?" by listing them, and be wrong.
        """
        if not self.scans:
            return (
                "Meeting attendance is not known yet — the avatar has not observed the "
                "participant list."
            )

        lines: list[str] = []

        here = self.present
        if here:
            lines.append(f"Currently in the meeting ({len(here)}): {_join_names(here)}.")
        else:
            lines.append("Nobody else is currently in the meeting — the avatar is alone.")

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


class AttendanceLedger:
    """Accumulates who was in the meeting, from the roster stream.

    Not a component in the health report and not on any media path. It holds no task, opens
    nothing, and every method is synchronous and non-raising — because ``observe_roster`` is
    called from the bridge's read loop, which is the media channel. The same constraint
    ``MeetChatSource.offer`` and ``MeetHandRaiseSource.offer`` are written to, for the same
    reason: a bug in a bookkeeping feature must not be able to stall or tear down audio.
    """

    __slots__ = ("_entries", "_over_capacity", "_scans", "_self_names")

    def __init__(
        self, *, invitees: tuple[str, ...] = (), self_names: tuple[str, ...] = ()
    ) -> None:
        self._entries: dict[str, _Entry] = {}
        self._self_names: tuple[str, ...] = ()
        self._scans = 0
        self._over_capacity = 0
        # Before the invitees, so a self-name that also appears on the invite list cannot create
        # an entry that then never matches anybody.
        for name in self_names:
            self.observe_self_name(name)
        self.seed_invitees(invitees)

    @property
    def scans(self) -> int:
        return self._scans

    # -- inputs ------------------------------------------------------------

    def seed_invitees(self, names: tuple[str, ...] | list[str]) -> int:
        """Record who was invited, from a source outside the meeting.

        The authoritative source is the calendar event the meeting came from — Meet's own UI
        shows an invite list only inside the People panel, and only sometimes. Taking it as
        data means this works without scraping anything and without the avatar opening a panel
        other participants can watch it open.

        Idempotent and additive: seeding twice with overlapping lists marks each person once,
        and seeding after people have already joined marks the existing entries rather than
        creating duplicates. Returns how many names were newly marked as invited.
        """
        marked = 0
        for raw in names or ():
            name = _clean_name(str(raw or ""))
            if not name:
                continue
            key = _key(page_id="", display_name=name)
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
            # An invitee seeded without a name we later see in the roster keeps whichever
            # name the roster reported — the calendar's "priya@example.com" is a worse label
            # than Meet's "Priya Menon", and the roster wins on merge below.
        if marked:
            logger.info(
                "meet_attendance.invitees_seeded", invited=marked, tracked=len(self._entries)
            )
        return marked

    def observe_roster(self, roster: MeetRoster) -> None:
        """Fold one roster into the ledger. Never raises, never blocks.

        Registered with ``ChromiumBridge.add_roster_listener``, so this runs on the bridge's
        read loop. Everything here is dict work on a few hundred entries at most, at the
        page's 250 ms scan floor and only when the roster actually *changed* — the bridge
        already suppresses identical rosters.
        """
        try:
            self._observe(roster)
        except Exception as exc:  # pragma: no cover - defensive; nothing below should raise
            # Swallowed on purpose, and this is the one place in the module that needs saying:
            # this is called from the media read loop, so an unexpected shape in a DOM-derived
            # payload must cost an attendance update and nothing else. Losing a name is a
            # bookkeeping gap; propagating would drop the audio channel.
            logger.warning("meet_attendance.observe_failed", error=str(exc))

    def _observe(self, roster: MeetRoster) -> None:
        self._scans += 1
        self.observe_self_name(roster.self_name)

        # Learn the account's rendered name from any entry Meet marked as ours. The marker is
        # not on every scan — it depends on which selector matched and whether the panel is
        # open — so remembering it is what stops the avatar appearing as an attendee on the
        # scans that lack it.
        for participant in roster.participants:
            if participant.is_self:
                self.observe_self_name(participant.display_name)

        now_us, now_at = _now()

        seen_keys: set[str] = set()

        for participant in roster.others:
            name = _clean_name(participant.display_name)
            if self._is_self(name):
                continue
            key = _key(page_id=participant.page_id, display_name=name)
            seen_keys.add(key)

            entry = self._entries.get(key)
            if entry is None:
                if len(self._entries) >= _MAX_TRACKED:
                    self._over_capacity += 1
                    if self._over_capacity == 1:
                        logger.warning(
                            "meet_attendance.capacity_reached",
                            tracked=len(self._entries),
                            note="further new participants are not recorded for this session",
                        )
                    continue
                entry = _Entry(display_name=name or None)
                self._entries[key] = entry
                logger.info(
                    "meet_attendance.joined",
                    participant=name or participant.page_id or "(unnamed)",
                    present=len(seen_keys),
                )

            # The roster's name is the better label whenever it has one: an entry created by
            # ``seed_invitees`` may be holding a calendar address, and a tile that rendered
            # without a name earlier may have one now.
            if name:
                entry.display_name = name
            if participant.page_id:
                entry.page_id = participant.page_id

            if entry.first_seen_us is None:
                entry.first_seen_us = now_us
                entry.first_seen_at = now_at
            elif not entry.present:
                # Back after a gap. Counted rather than overwriting ``first_seen``, so the
                # ledger can still say how long they have been involved overall.
                entry.rejoins += 1
                logger.info(
                    "meet_attendance.rejoined",
                    participant=entry.display_name or "(unnamed)",
                    rejoins=entry.rejoins,
                )

            entry.present = True
            entry.last_seen_us = now_us
            entry.last_seen_at = now_at

        for key, entry in self._entries.items():
            if entry.present and key not in seen_keys:
                entry.present = False
                logger.info(
                    "meet_attendance.left",
                    participant=entry.display_name or "(unnamed)",
                    stayed_s=round(entry.duration_us(now_us=now_us) / 1_000_000, 1),
                )

    def observe_self_name(self, name: str | None) -> None:
        """Learn a name that means "this is us".

        The avatar is in its own roster, and counting it as an attendee would make every
        answer wrong by one — "there are 3 people here" when there are two and a bot. The
        page's ``isSelf`` already covers the common case via ``MeetRoster.others``; this is
        the half that knows the signed-in account's rendered name, which only the roster
        reveals. Same reasoning as ``MeetChatSource.observe_self_name``.

        Retroactive: a name learned on scan 5 evicts the entry it created on scans 1-4, so a
        late-arriving ``selfName`` does not leave the avatar permanently listed as a guest.
        """
        cleaned = _clean_name(str(name or ""))
        if not cleaned:
            return
        if any(cleaned.lower() == known.lower() for known in self._self_names):
            return
        self._self_names = (*self._self_names, cleaned)
        self._entries.pop(_key(page_id="", display_name=cleaned), None)

    # -- output ------------------------------------------------------------

    def snapshot(self) -> AttendanceSnapshot:
        """A stable, ordered view of the ledger.

        Ordered by when each person first appeared, invitees who never joined last. That is
        the order the meeting happened in, which is what makes the rendered brief read like an
        account of it rather than a set.
        """
        records = tuple(
            entry.to_record() for _, entry in sorted(self._entries.items(), key=_ordering)
        )
        return AttendanceSnapshot(
            records=records,
            self_name=self._self_names[0] if self._self_names else None,
            scans=self._scans,
        )

    def _is_self(self, name: str) -> bool:
        if not name:
            return False
        return any(name.lower() == known.lower() for known in self._self_names)


@dataclass(slots=True)
class _Entry:
    """Mutable bookkeeping for one person. Converted to a frozen record on snapshot."""

    display_name: str | None = None
    page_id: str | None = None
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
            page_id=self.page_id,
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


def _key(*, page_id: str, display_name: str) -> str:
    """The ledger's identity for a person.

    Folded name first — see the module docstring for why that inverts ``parse_roster``'s
    choice. Prefixed so a name that happens to look like an id cannot collide with one.
    """
    name = _clean_name(display_name)
    if name:
        return f"name:{name.casefold()}"
    return f"id:{page_id.strip()}"


def _clean_name(value: str) -> str:
    """Collapse whitespace and bound the length. Names arrive already stripped of Meet's
    status suffixes by ``participants._clean``; this is the second, cheaper pass that also
    covers names arriving from a calendar."""
    return " ".join(str(value or "").split())[:_MAX_NAME_LEN].strip()


def _now() -> tuple[int, datetime]:
    """A monotonic microsecond stamp and its wall-clock partner.

    Not taken from ``MediaClock``: this is not media, and a ledger that outlives a media
    clock reset should not have its history reordered by one. ``perf_counter_ns`` is the same
    monotonic guarantee without the coupling.
    """
    return perf_counter_ns() // 1_000, datetime.now(UTC)
