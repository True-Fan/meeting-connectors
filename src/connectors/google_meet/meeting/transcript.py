"""What each person actually said, attributed to them.

**The question this exists to answer, and why nothing else could.** An avatar asked *"what did
they ask you?"* or *"what did Dev say?"* had no way to answer. Its own transcription is upstream
in the agent, which hears one mixed stream and therefore knows the words without knowing whose
they are; this connector knows who is speaking without knowing the words, because attribution is
built from audio *levels* and DOM observation rather than from speech. Both halves existed and
neither was joined to the other.

Meet's own captions are where they are already joined. Meet writes the speaker's name and the
words that person said next to each other in the DOM, from its own transcription, and the page
reads that (``js/bridge.js``, ``scanCaptions``). So this module is a ledger of attributed lines —
the meeting's conversation, as Meet transcribed it, in order, with names.

**And what was typed, on the same terms.** The chat panel is the other half of a meeting's
conversation, and it was recorded nowhere: each message crossed the avatar socket once, was
answered, and left no trace. A meeting held entirely in chat therefore produced a ledger
containing only the avatar's own captioned voice, and the avatar — asked what had been discussed
— truthfully described itself greeting somebody. Typed lines are folded in here rather than
tracked separately because "what has been said in this meeting" is one question, and answering it
from two ledgers means a caller that forgets one. They are *marked* (``in_chat``), because typing
is not speaking and an agent must not report that it heard somebody who never unmuted.

**What it is not.** Not the avatar's transcript and not a replacement for one: the agent's STT is
better tuned, hears the avatar's own turns, and drives the conversation. This is the *other
participants'* speech, attributed — which is exactly the part the agent cannot attribute itself.
Where the two disagree on wording, the agent's own transcript is the one it should trust for what
was said to it; this one is authoritative about **who** said it.

**Wall-clock times, like the attendance ledger's.** "Dev asked about Delhi two minutes ago" is a
statement about the meeting, not about a media timeline, so each line carries a UTC stamp beside
the monotonic one.

Everything here is synchronous, total and non-blocking: ``offer`` is called from the bridge's read
loop, which is the media channel. A bookkeeping feature must never be able to stall or tear down
audio — the same rule ``MeetChatSource.offer`` and ``AttendanceLedger.observe_roster`` follow.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import perf_counter_ns

from src.connectors.google_meet.meeting.participants import (
    MeetParticipant,
    MeetRoster,
    clean_label,
    looks_like_self,
)
from src.infrastructure.logging import get_logger

logger = get_logger(__name__)

COMPONENT_NAME = "google_meet_transcript"

ANONYMOUS = "Someone"
"""What an unattributed line is credited to. Meet nearly always names a caption; a continuation
block sometimes carries only the words."""

SELF_LABEL = "The avatar (you)"
"""How the avatar's own captioned turns are credited in the brief.

Not its account name, because the brief's reader *is* the avatar: "Backend Services asked about
Delhi" reads as a third party whose question is owed an answer, and it was the avatar's own
sentence. Naming it plainly is what stops the agent replying to itself."""

_MAX_NAME_LEN = 120
_MAX_TEXT_LEN = 2_000
_MAX_LINES = 500
"""Ceiling on remembered lines.

Oldest dropped, like the speaker turn history and for the same reason: this answers "what was
just said" far more often than "what was said an hour ago". Five hundred lines is a long meeting's
worth of captions at a few hundred bytes each."""

_BRIEF_LINES = 8
"""How many lines the agent's brief carries.

A window rather than the lot, because the brief is standing context that is re-sent whenever it
changes: an unbounded transcript would grow the frame without limit and crowd out the
conversation the agent is actually having. Eight lines is comfortably more than a question and its
follow-ups — and it was twelve until a live run complained about reply latency, where every line
here is tokens the agent re-reads before it can start answering."""


@dataclass(frozen=True, slots=True)
class TranscriptLine:
    """One thing one person said — aloud, or in the meeting's chat."""

    speaker: str | None
    text: str
    at_us: int
    at: datetime
    is_self: bool = False
    inferred: bool = False
    """True when the name came from elimination — exactly one other person was in the meeting —
    rather than from the caption itself."""
    in_chat: bool = False
    """True when this was typed rather than spoken.

    **Marked rather than merged, and that distinction is load-bearing in both directions.** A
    typed question is part of the conversation and belongs in this ledger — a whole meeting was
    held in chat and the transcript recorded nothing but the avatar's own voice, which is the
    failure this field exists inside the fix for. But it is not the same act as speaking: "nobody
    has spoken yet, and Dev asked this in chat" is the truth, and flattening the two would have
    the agent claim it heard somebody who never opened their microphone."""

    @property
    def label(self) -> str:
        """Who to credit. Never empty."""
        if self.is_self:
            return SELF_LABEL
        return self.speaker or ANONYMOUS

    def render(self) -> str:
        """The line as prose, for a context window.

        The avatar's own turns are labelled as the avatar rather than by name, because the
        destination is the avatar's *own* context: "The avatar (you) said" is unambiguous where its
        account name would read as a third party it should answer.
        """
        if self.in_chat:
            return f"{self.label} (in chat): {self.text}"
        return f"{self.label}: {self.text}"


@dataclass(frozen=True, slots=True)
class TranscriptSnapshot:
    """An immutable view of the conversation so far."""

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
        """Everything one person said. Case-insensitive, because the caller is quoting a human."""
        folded = " ".join(name.split()).casefold()
        return tuple(line for line in self.lines if line.label.casefold() == folded)

    @property
    def chat_lines(self) -> int:
        """How many lines were typed rather than spoken.

        **The count a pusher should watch, and the reason is latency rather than tidiness.** The
        agent transcribes the meeting's audio itself, so a captioned line tells it something it
        already knows — while re-sending standing context invalidates the reply it had started
        preparing. Captions arrive every couple of seconds *while somebody is talking*, which is
        the worst possible moment to do that. A typed line is the opposite: the agent cannot hear
        the chat panel, so it is news, and it arrives at typing speed.
        """
        return sum(1 for line in self.lines if line.in_chat)

    def recent(self, limit: int = _BRIEF_LINES) -> tuple[TranscriptLine, ...]:
        return self.lines[-limit:] if limit > 0 else ()

    def agent_context(self, *, limit: int = _BRIEF_LINES) -> str:
        """The conversation as prose, for the agent's context window.

        Rendered here rather than in the API layer because it is the same paragraph whoever asks,
        and rendered as dialogue rather than as JSON because that is the form an LLM can use: a
        list of ``{speaker, text}`` objects is a data structure, and "Dev Choudhary: I want to know
        about Delhi" is a conversation.

        Says where it came from, deliberately. Meet's captions are a *transcription* — they
        mishear names and technical words — and an agent that quotes them as verbatim fact will
        occasionally be confidently wrong about what somebody said.
        """
        recent = self.recent(limit)
        if not recent:
            return ""
        # Says which of the two it is carrying, because the caveat only applies to one of them:
        # a caption is a transcription and can mishear a name, and a chat message is the exact
        # characters somebody typed. An agent told everything here is "imperfect wording" will
        # hedge on a line it should quote.
        typed = any(line.in_chat for line in recent)
        spoken = any(not line.in_chat for line in recent)
        if typed and spoken:
            preamble = (
                "What has been said in the meeting so far — spoken lines transcribed by Google "
                "Meet's own captions (the wording may be imperfect), and lines marked \"in "
                "chat\" typed verbatim into the meeting chat — attributed to whoever said them:"
            )
        elif typed:
            preamble = (
                "What has been said in the meeting so far. Every line was typed into the "
                "meeting chat rather than spoken aloud:"
            )
        else:
            preamble = (
                "What has been said in the meeting so far, transcribed by Google Meet's own "
                "captions and attributed to whoever said it (the wording may be imperfect):"
            )
        lines = [preamble]
        lines.extend(f"- {line.render()}" for line in recent)
        if len(self.lines) > len(recent):
            lines.append(f"({len(self.lines) - len(recent)} earlier lines not shown.)")
        return "\n".join(lines)


class MeetTranscript:
    """Accumulates attributed caption lines.

    Not a component in the health report and not on any media path: it holds no task, opens
    nothing, and every method returns rather than raising.
    """

    __slots__ = ("_dropped", "_keys", "_lines", "_others", "_seen", "_self_names", "_voices")

    def __init__(self, *, self_names: tuple[str, ...] = ()) -> None:
        self._lines: list[TranscriptLine] = []
        # The dedupe key of each remembered line, in lockstep with ``_lines``. Kept beside them
        # rather than recomputed on eviction because a chat line's key is its message id, which
        # is not derivable from the line: recomputing would leave the id in ``_seen`` forever and
        # slowly turn the dedupe set into a leak.
        self._keys: list[str] = []
        # Everyone but the avatar, from the roster — what a *typed* line is eliminated against.
        self._others: tuple[str, ...] = ()
        # The subset who could be the voice: everyone not known to be muted. A muted participant
        # can type all day and cannot have said a captioned word, so the two ledgers eliminate
        # against different sets — and conflating them would credit somebody's speech to a person
        # sitting there with their microphone off.
        self._voices: tuple[str, ...] = ()
        # Dedupe by content, because Meet re-renders the caption panel constantly and a settled
        # line can be presented again — the same hazard chat's message ids exist for.
        self._seen: set[str] = set()
        self._self_names: tuple[str, ...] = ()
        self._dropped = 0
        for name in self_names:
            self.observe_self_name(name)

    # -- inputs ------------------------------------------------------------

    def offer(self, body: dict[str, object]) -> bool:
        """Accept one ``CAPTION`` payload from the page. Never blocks, never raises."""
        try:
            return self._offer(body)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("meet_transcript.offer_failed", error=str(exc))
            return False

    def _offer(self, body: dict[str, object]) -> bool:
        if not isinstance(body, dict):
            return False
        text = " ".join(str(body.get("text") or "").split())[:_MAX_TEXT_LEN]
        if not text:
            return False

        raw_speaker = str(body.get("speaker") or "")
        # Read before cleaning, which is the hazard ``_SELF_MARKERS`` documents: ``" (you)"`` is a
        # status suffix, so ``clean_label`` strips it — and a tile labelled "dev Choudhary (You)"
        # then looks like an ordinary participant.
        first_person = looks_like_self(raw_speaker)
        speaker = clean_label(raw_speaker) or None
        inferred = False
        if speaker is None:
            # **Attribution by elimination, exactly as the speaker tracker does it, and for the same
            # reason it had to exist there.** A live run captured eleven caption lines and Meet's
            # name was in none of them — the panel renders it in a sibling of the element the block
            # selectors match. In a two-person meeting there is exactly one person the words can
            # belong to, so the transcript reads "Dev Choudhary: …" instead of "Someone: …" without
            # depending on markup at all.
            #
            # Fails closed on two or more others (naming one would be a guess) and on a name that
            # looks like ours (the avatar's own speech is marked, never credited to a participant).
            speaker = self._only_voice()
            inferred = speaker is not None

        is_self = bool(body.get("isSelf")) or self._is_self(speaker)
        if first_person:
            # **Meet captions the local participant as "You" — and the local participant is the
            # avatar.** A live run recorded the avatar's own greeting as a participant's line, so
            # the agent was handed its own words back as something it had been asked. Relabelled
            # rather than dropped: the avatar's turns belong in the transcript, as its own.
            is_self = True
            speaker = self._self_names[0] if self._self_names else None
            inferred = False

        key = f"{(speaker or '').casefold()}|{text}"
        if key in self._seen:
            return False

        line = TranscriptLine(
            speaker=speaker,
            text=text,
            at_us=perf_counter_ns() // 1_000,
            at=datetime.now(UTC),
            is_self=is_self,
            inferred=inferred,
        )
        self._append(line, key)

        logger.info(
            "meet_transcript.line",
            speaker=line.label,
            chars=len(text),
            is_self=is_self,
            # Where the page got the name: ``img`` is the participant photo's alt text, ``line``
            # is the caption row's first rendered line, ``none`` means Meet named nobody and this
            # was inferred or left anonymous. A name that turns out to be wrong is diagnosable
            # only if the source is recorded beside it.
            name_from="inferred" if inferred else str(body.get("nameFrom") or "none"),
            total=len(self._lines),
        )
        return True

    def offer_chat(self, body: dict[str, object], *, message_id: str | None = None) -> bool:
        """Accept one ``CHAT_MESSAGE`` payload as a line of the conversation. Never raises.

        **Why the chat panel feeds the same ledger the caption panel does.** A live meeting was
        held entirely in chat: five typed questions, every one of them answered aloud by the
        avatar — and asked afterwards what conversation had taken place, it said it had only
        greeted somebody. It was right about what it had been told. Chat reached the agent as a
        single transient frame per message and was recorded nowhere, so the standing brief held
        the avatar's own captioned voice and nothing else. A ledger of "what was said in this
        meeting" that excludes the half of it that was typed is not one.

        Recorded **before** the ``@mention`` filter that decides what deserves an answer, and
        including the avatar's own messages. Those are two different questions: whether to reply
        to a message is policy (``MeetChatSource``), and what was said in the room is history.
        A question two participants asked each other is not the avatar's to answer and is
        absolutely part of the conversation it is being asked to summarise.
        """
        try:
            return self._offer_chat(body, message_id=message_id)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("meet_transcript.chat_failed", error=str(exc))
            return False

    def _offer_chat(self, body: dict[str, object], *, message_id: str | None = None) -> bool:
        if not isinstance(body, dict):
            return False
        text = " ".join(str(body.get("text") or "").split())[:_MAX_TEXT_LEN]
        if not text:
            return False

        raw_sender = str(body.get("sender") or "")
        first_person = looks_like_self(raw_sender)
        speaker = clean_label(raw_sender) or None
        inferred = False
        if speaker is None:
            # Elimination, exactly as a nameless caption gets it and worth as much here: the page
            # could not read a name off the row, and in a two-person meeting there is only one
            # person the message can be from. This is what named the sender of every message in
            # the run this was written against, with no dependence on Meet's markup at all.
            speaker = self._only_other()
            inferred = speaker is not None

        is_self = bool(body.get("isSelf")) or self._is_self(speaker)
        if first_person:
            is_self = True
            speaker = self._self_names[0] if self._self_names else None
            inferred = False

        # The page's message id when there is one, because it is the identity Meet itself
        # maintains across the re-renders that made chat need a dedupe in the first place. The
        # content key is the fallback, and is namespaced so a typed line and a captioned line
        # with the same words are two entries rather than one — somebody reading their own
        # question aloud is a real thing that happens.
        key = f"chat:{message_id}" if message_id else f"chat|{(speaker or '').casefold()}|{text}"
        if key in self._seen:
            return False

        line = TranscriptLine(
            speaker=speaker,
            text=text,
            at_us=perf_counter_ns() // 1_000,
            at=datetime.now(UTC),
            is_self=is_self,
            inferred=inferred,
            in_chat=True,
        )
        self._append(line, key)

        logger.info(
            "meet_transcript.chat_line",
            speaker=line.label,
            chars=len(text),
            is_self=is_self,
            # ``group`` means Meet grouped this under an earlier message's name, ``inferred``
            # means Python named it by elimination, ``none`` means nobody could. Recorded for
            # the reason a caption's ``nameFrom`` is: a wrong name is only diagnosable beside
            # the thing that produced it.
            name_from="inferred" if inferred else str(body.get("senderFrom") or "none"),
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

    def observe_roster(self, roster: MeetRoster) -> None:
        """Learn which name is the avatar's own, and who else is here. Never raises."""
        try:
            self.observe_self_name(roster.self_name)
            for participant in roster.participants:
                if participant.is_self:
                    self.observe_self_name(participant.display_name)
            self._others = self._candidates(roster.others)
            self._voices = self._candidates(roster.could_be_speaking)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("meet_transcript.roster_failed", error=str(exc))

    def _candidates(self, participants: tuple[MeetParticipant, ...]) -> tuple[str, ...]:
        """Their names, cleaned, deduped, with anything that looks like us removed."""
        names: list[str] = []
        for participant in participants:
            name = " ".join(str(participant.display_name or "").split())[:_MAX_NAME_LEN]
            if not name or self._is_self(name):
                continue
            if not any(name.casefold() == known.casefold() for known in names):
                names.append(name)
        return tuple(names)

    def _only_other(self) -> str | None:
        """The single other participant, when there is exactly one and it is not us.

        Used for a *typed* line, so it eliminates against everybody in the room: sitting muted is
        no obstacle to writing in the chat.
        """
        return self._sole(self._others)

    def _only_voice(self) -> str | None:
        """The single participant who could be the voice, when there is exactly one.

        Used for a *captioned* line, so somebody Meet says is muted is not a candidate — which is
        what lets a two-person room still be eliminated down to one person.
        """
        return self._sole(self._voices)

    def _sole(self, candidates: tuple[str, ...]) -> str | None:
        if len(candidates) != 1:
            return None
        candidate = candidates[0]
        return None if self._is_self(candidate) else candidate

    def observe_self_name(self, name: str | None) -> None:
        """Add a name that means "this is us".

        The avatar's own speech is captioned like anybody else's, and it is kept rather than
        dropped — a transcript that omits half a conversation is not one. It is *marked*, so the
        brief can present it as the avatar's own turn instead of as something a participant said.
        """
        cleaned = " ".join(str(name or "").split())
        if not cleaned:
            return
        if any(cleaned.casefold() == known.casefold() for known in self._self_names):
            return
        self._self_names = (*self._self_names, cleaned)

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

    def _is_self(self, name: str | None) -> bool:
        if not name:
            return False
        return any(name.casefold() == known.casefold() for known in self._self_names)
