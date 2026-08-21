"""What each person actually said, attributed to them.

**The question this exists to answer, and why nothing else on this connector could.** An
avatar asked *"what did they ask you?"* or *"what did Dev say?"* has no other way to answer.
Its own transcription is upstream in the agent, which receives **one mixed stream** and so
knows the words without knowing whose they are; the speaker observer knows who is speaking
without knowing the words. Both halves exist and neither is joined to the other.

Teams' live captions are where they are already joined: Teams attributes each caption line to
the participant who said it. This module is a ledger of those lines — the meeting's
conversation, in order, with names.

**And what was typed, on the same terms.** The chat panel is the other half of a meeting's
conversation, and without this it would be recorded nowhere: each message crosses the avatar
socket once, is answered, and leaves no trace. A meeting held largely in chat would then
produce a ledger containing only what was spoken aloud, and the avatar — asked what had been
discussed — would describe a conversation that was not the one that happened. Typed lines are
folded in here rather than tracked separately because "what has been said in this meeting" is
one question. They are *marked* (``in_chat``), because typing is not speaking and an agent
must not report that it heard somebody who never unmuted.

**What it is not.** Not the avatar's transcript and not a replacement for one: the agent's own
STT is better tuned, hears the avatar's turns, and drives the conversation. This is the *other
participants'* speech, attributed — which is exactly the part the agent cannot attribute
itself. Where the two disagree on wording, the agent's transcript is the one to trust for what
was said; this one is authoritative about **who** said it.

Everything here is synchronous, total and non-blocking: ``offer`` is called from the page
server's read loop, which also carries the avatar's voice into the page.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import perf_counter_ns

from src.connectors.teams_web.observations import TranscriptLine as PageTranscriptLine
from src.domain.meeting import ChatMessage
from src.infrastructure.logging import get_logger

logger = get_logger(__name__)

COMPONENT_NAME = "teams_web_transcript"

ANONYMOUS = "Someone"
"""What an unattributed line is credited to. Reachable on a caption whose author element the
page could not read."""

SELF_LABEL = "The avatar (you)"
"""How the avatar's own lines are credited in the brief.

Not its display name, because the brief's reader *is* the avatar: "AI Avatar asked about
Delhi" reads as a third party whose question is owed an answer, when it was the avatar's own
sentence. Naming it plainly is what stops the agent replying to itself."""

_MAX_NAME_LEN = 120
_MAX_TEXT_LEN = 2_000
_MAX_LINES = 500
"""Ceiling on remembered lines, oldest dropped — this answers "what was just said" far more
often than "what was said an hour ago"."""

_BRIEF_LINES = 8
"""How many lines the agent's brief carries.

A window rather than the lot, because the brief is standing context that is re-sent whenever
it changes: an unbounded transcript would grow the frame without limit and crowd out the
conversation the agent is actually having. Eight is comfortably more than a question and its
follow-ups, and every line here is tokens the agent re-reads before it can start answering."""


@dataclass(frozen=True, slots=True)
class TranscriptLine:
    """One thing one person said — aloud, or in the meeting's chat."""

    speaker: str | None
    text: str
    at_us: int
    at: datetime
    is_self: bool = False
    inferred: bool = False
    """True when the name came from elimination rather than from the caption's own author."""
    in_chat: bool = False
    """True when this was typed rather than spoken.

    **Marked rather than merged, and that is load-bearing in both directions.** A typed
    question is part of the conversation and belongs in this ledger. But it is not the same act
    as speaking: "nobody has spoken yet, and Dev asked this in chat" is the truth, and
    flattening the two would have the agent claim it heard somebody who never opened their
    microphone."""

    @property
    def label(self) -> str:
        """Who to credit. Never empty."""
        if self.is_self:
            return SELF_LABEL
        return self.speaker or ANONYMOUS

    def render(self) -> str:
        """The line as prose, for a context window."""
        if self.in_chat:
            return f"{self.label} (in chat): {self.text}"
        return f"{self.label}: {self.text}"


@dataclass(frozen=True, slots=True)
class TranscriptSnapshot:
    """An immutable view of the conversation so far.

    Shaped to match the Google Meet and Zoom-web snapshots so ``GET
    /sessions/{id}/transcript`` serves a Teams-web session unchanged — see
    ``meeting/attendance.AttendanceSnapshot`` for why that compatibility is deliberate.
    """

    lines: tuple[TranscriptLine, ...] = field(default_factory=tuple)
    self_name: str | None = None
    observed_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def speakers(self) -> tuple[str, ...]:
        """Everyone who has said something, in the order they first did."""
        seen: list[str] = []
        for line in self.lines:
            if line.label not in seen:
                seen.append(line.label)
        return tuple(seen)

    def by_speaker(self, name: str) -> tuple[TranscriptLine, ...]:
        """Everything one person said. Case-insensitive: the caller is quoting a human."""
        folded = " ".join(name.split()).casefold()
        return tuple(line for line in self.lines if line.label.casefold() == folded)

    @property
    def chat_lines(self) -> int:
        """How many lines were typed rather than spoken.

        **The count a pusher should watch, and the reason is latency rather than tidiness.**
        The agent transcribes the meeting's audio itself, so a captioned line tells it
        something it already knows — while re-sending standing context invalidates the reply it
        had started preparing. Caption lines arrive *while somebody is talking*, which is the
        worst possible moment for that. A typed line is the opposite: the agent cannot see the
        chat panel, so it is news, and it arrives at typing speed.
        """
        return sum(1 for line in self.lines if line.in_chat)

    def recent(self, limit: int = _BRIEF_LINES) -> tuple[TranscriptLine, ...]:
        return self.lines[-limit:] if limit > 0 else ()

    def agent_context(self, *, limit: int = _BRIEF_LINES) -> str:
        """The conversation as prose, for the agent's context window.

        Rendered as dialogue rather than as JSON because that is the form an LLM can use: a
        list of ``{speaker, text}`` objects is a data structure, and "Dev Choudhary: I want to
        know about Delhi" is a conversation.

        Says where it came from, deliberately. Teams' live captions are a *transcription* —
        they mishear names and technical words — and an agent that quotes them as verbatim fact
        will occasionally be confidently wrong about what somebody said.
        """
        recent = self.recent(limit)
        if not recent:
            return ""
        # Says which of the two it is carrying, because the caveat applies to only one: a
        # captioned line can mishear a name, and a chat message is the exact characters
        # somebody typed. An agent told everything here is approximate will hedge on a line it
        # should quote.
        typed = any(line.in_chat for line in recent)
        spoken = any(not line.in_chat for line in recent)
        if typed and spoken:
            preamble = (
                "What has been said in the meeting so far — spoken lines transcribed by "
                "Teams' own live captions (the wording may be imperfect), and lines marked "
                '"in chat" typed verbatim into the meeting chat — attributed to whoever said '
                "them:"
            )
        elif typed:
            preamble = (
                "What has been said in the meeting so far. Every line was typed into the "
                "meeting chat rather than spoken aloud:"
            )
        else:
            preamble = (
                "What has been said in the meeting so far, transcribed by Teams' own live "
                "captions and attributed to whoever said it (the wording may be imperfect):"
            )
        lines = [preamble]
        lines.extend(f"- {line.render()}" for line in recent)
        if len(self.lines) > len(recent):
            lines.append(f"({len(self.lines) - len(recent)} earlier lines not shown.)")
        return "\n".join(lines)


class TeamsTranscript:
    """Accumulates attributed lines, spoken and typed.

    Not a component in the health report and not on any media path: it holds no task, opens
    nothing, and every method returns rather than raising.
    """

    __slots__ = ("_dropped", "_keys", "_lines", "_others", "_seen", "_self_names")

    def __init__(self, *, self_names: tuple[str, ...] = ()) -> None:
        self._lines: list[TranscriptLine] = []
        # The dedupe key of each remembered line, in lockstep with ``_lines``. Kept beside
        # them rather than recomputed on eviction, so the dedupe set cannot slowly leak.
        self._keys: list[str] = []
        self._others: tuple[str, ...] = ()
        self._seen: set[str] = set()
        self._self_names: tuple[str, ...] = ()
        self._dropped = 0
        for name in self_names:
            self.observe_self_name(name)

    # -- inputs ------------------------------------------------------------

    def offer(self, line: PageTranscriptLine) -> bool:
        """Accept one captioned line. Never blocks, never raises."""
        try:
            return self._offer(line)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("teams_transcript.offer_failed", error=str(exc))
            return False

    def _offer(self, line: PageTranscriptLine) -> bool:
        text = _clean_text(line.text)
        if not text:
            return False

        speaker = _clean_name(line.display_name)
        inferred = False
        if speaker is None:
            # Elimination, for a caption whose author element the page could not read. Fails
            # closed at two or more others, because naming one of two would be a guess.
            speaker = self._only_other()
            inferred = speaker is not None

        is_self = self._is_self(speaker)

        # Dedupe by content, because a caption panel re-renders and the settle rule can let
        # the same settled line be read twice across a re-render.
        key = f"said|{(speaker or '').casefold()}|{text}"
        if key in self._seen:
            return False

        record = TranscriptLine(
            speaker=speaker,
            text=text,
            at_us=line.at_us or _monotonic_us(),
            at=datetime.now(UTC),
            is_self=is_self,
            inferred=inferred,
        )
        self._append(record, key)
        logger.info(
            "teams_transcript.line",
            speaker=record.label,
            chars=len(text),
            is_self=is_self,
            name_from="inferred" if inferred else ("page" if line.display_name else "none"),
            total=len(self._lines),
        )
        return True

    def offer_chat(self, message: ChatMessage) -> bool:
        """Accept one chat message as a line of the conversation. Never raises.

        Recorded **before** the ``@mention`` filter that decides what deserves an answer, and
        including messages the avatar itself sent. Those are two different questions: whether
        to reply to a message is policy (``meeting/chat.py``), and what was said in the room is
        history. A question two participants asked each other is not the avatar's to answer and
        is absolutely part of the conversation it may be asked to summarise.
        """
        try:
            return self._offer_chat(message)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("teams_transcript.chat_failed", error=str(exc))
            return False

    def _offer_chat(self, message: ChatMessage) -> bool:
        text = _clean_text(message.text)
        if not text:
            return False

        speaker = _clean_name(message.sender)
        inferred = False
        if speaker is None:
            speaker = self._only_other()
            inferred = speaker is not None

        is_self = message.is_self or self._is_self(speaker)

        # Namespaced separately from a spoken line, so somebody reading their own question
        # aloud is two entries rather than one — which is a real thing that happens.
        key = f"chat|{(speaker or '').casefold()}|{text}"
        if key in self._seen:
            return False

        record = TranscriptLine(
            speaker=speaker,
            text=text,
            at_us=message.received_at_us or _monotonic_us(),
            at=datetime.now(UTC),
            is_self=is_self,
            inferred=inferred,
            in_chat=True,
        )
        self._append(record, key)
        logger.info(
            "teams_transcript.chat_line",
            speaker=record.label,
            chars=len(text),
            is_self=is_self,
            name_from="inferred" if inferred else ("page" if message.sender else "none"),
            total=len(self._lines),
        )
        return True

    def _append(self, line: TranscriptLine, key: str) -> None:
        """Record one line and retire the oldest if the ledger is full."""
        self._seen.add(key)
        self._lines.append(line)
        self._keys.append(key)
        if len(self._lines) > _MAX_LINES:
            self._lines.pop(0)
            self._seen.discard(self._keys.pop(0))
            self._dropped += 1

    def observe_participants(self, present: tuple[str, ...]) -> None:
        """Learn who else is in the meeting, for elimination. Never raises.

        Taken from the attendance ledger, which already holds it — the same source the speaker
        tracker reads, so the two cannot disagree about who is in the room.
        """
        try:
            names: list[str] = []
            for candidate in present:
                name = _clean_name(candidate)
                if not name or self._is_self(name):
                    continue
                if not any(name.casefold() == known.casefold() for known in names):
                    names.append(name)
            self._others = tuple(names)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("teams_transcript.participants_failed", error=str(exc))

    def observe_self_name(self, name: str | None) -> None:
        """Add a name that means "this is us".

        The avatar's own speech is captioned like anybody else's, and it is kept rather than
        dropped — a transcript that omits half a conversation is not one. It is *marked*, so
        the brief presents it as the avatar's own turn instead of as something a participant
        said.
        """
        cleaned = " ".join(str(name or "").split())
        if not cleaned:
            return
        if any(cleaned.casefold() == known.casefold() for known in self._self_names):
            return
        self._self_names = (*self._self_names, cleaned)

    def _only_other(self) -> str | None:
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
    def count(self) -> int:
        return len(self._lines)

    @property
    def dropped(self) -> int:
        return self._dropped

    def snapshot(self) -> TranscriptSnapshot:
        return TranscriptSnapshot(
            lines=tuple(self._lines),
            self_name=self._self_names[0] if self._self_names else None,
        )


def _clean_name(value: str | None) -> str | None:
    cleaned = " ".join(str(value or "").split())[:_MAX_NAME_LEN].strip()
    return cleaned or None


def _clean_text(value: str) -> str:
    return " ".join(str(value or "").split())[:_MAX_TEXT_LEN].strip()


def _monotonic_us() -> int:
    return perf_counter_ns() // 1_000
