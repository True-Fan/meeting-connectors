"""Parsing chat messages the page observed, and serving them as a ``ChatSource``.

Three responsibilities that are deliberately separate: turning one ``CHAT_MESSAGE`` payload into
a domain ``ChatMessage`` (a pure function, so it is testable against every malformed shape Meet
can produce), deciding whether that message was addressed to the avatar at all, and handing the
survivors to the router as they arrive.

**Why the page cannot be trusted to filter.** ``bridge.js`` reports what the chat panel renders
and nothing more, including the avatar's own messages and whatever text a UI element happens to
carry. Deciding what deserves an answer is policy, and policy lives in Python — the same split
that keeps the browser layer free of judgement everywhere else in this connector.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from typing import Any

from src.connectors.google_meet.meeting.participants import MeetRoster
from src.domain.health import ComponentHealth
from src.domain.meeting import ChatMessage
from src.infrastructure.logging import get_logger
from src.services.media.clock import MediaClock

logger = get_logger(__name__)

COMPONENT_NAME = "google_meet_chat"

MAX_CHARS = 2_000
"""Longest message forwarded, in characters.

A cap rather than unbounded, because the text becomes an LLM prompt: a pasted wall of text
would cost tokens, delay the reply, and is far more likely to be spam than a question. Truncated
rather than dropped, so a long but genuine question still gets an answer."""


_MENTION_CACHE: dict[str, re.Pattern[str]] = {}

_LEADING_PUNCTUATION = " \t\n\r,.:;!?-–—@"  # noqa: RUF001 — people type both dashes
"""Trimmed from the front of what is left once a mention is removed, so "@Avatar, what is the
CTC?" reaches the agent as "what is the CTC?" rather than ", what is the CTC?"."""


def _mention_pattern(name: str) -> re.Pattern[str] | None:
    """A compiled matcher for one name the avatar answers to, or ``None`` if unusable.

    **The ``@`` is required.** Meet has no mention feature — no autocomplete, no participant
    token, nothing structural in the DOM to key on — so the ``@`` is the only deliberate
    signal a participant can give, and it is the difference between talking *to* the avatar
    and talking *about* it. "did the AI avatar join?" is a question for the room; "@AI Avatar
    are you there?" is a question for the avatar.

    Loose about everything else, because the ``@`` has already established intent: case is
    ignored and the separators between the name's words are optional, so ``@AI Avatar``,
    ``@ai_avatar``, ``@ai-avatar`` and ``@AIAvatar`` all count. The name must still stand as
    whole words, so ``@Aisha`` does not trigger an avatar named "AI".
    """
    cached = _MENTION_CACHE.get(name)
    if cached is not None:
        return cached

    tokens = [re.escape(token) for token in re.split(r"[^0-9A-Za-z]+", name) if token]
    if not tokens:
        return None

    body = r"[\s._\-]*".join(tokens)
    pattern = re.compile(rf"(?<![0-9A-Za-z_@])@{body}(?![0-9A-Za-z_])", re.IGNORECASE)
    _MENTION_CACHE[name] = pattern
    return pattern


def strip_mention(text: str, names: Sequence[str]) -> str | None:
    """Return ``text`` with the ``@mention`` removed, or ``None`` when the avatar was not tagged.

    The mention is stripped rather than left in place because what survives here becomes an LLM
    prompt: "@Avatar what is the notice period?" is a question about the notice period, and
    leaving the vocative in invites the agent to answer a question about its own name.

    A message that is *only* a mention keeps its original text. "@Avatar" alone is somebody
    getting the bot's attention, and forwarding an empty string would have it drop the message
    silently — better that the agent sees the greeting and says something.
    """
    for name in names:
        pattern = _mention_pattern(name)
        if pattern is None:
            continue
        match = pattern.search(text)
        if match is None:
            continue
        remainder = f"{text[: match.start()]} {text[match.end() :]}".strip()
        remainder = remainder.lstrip(_LEADING_PUNCTUATION).strip()
        # Only the gap the removal left, not every run in the message: line structure is left
        # alone because a pasted question can carry meaningful newlines.
        remainder = re.sub(r"[ \t]{2,}", " ", remainder)
        return remainder or text
    return None


def parse_chat_message(body: dict[str, Any], *, received_at_us: int = 0) -> ChatMessage | None:
    """Build a ``ChatMessage`` from a ``CHAT_MESSAGE`` payload, or ``None`` if unusable.

    Never raises, for the reason ``parse_roster`` does not: the page is reporting on a DOM it
    does not control, and one odd entry must not break the feature.
    """
    if not isinstance(body, dict):
        return None

    text = str(body.get("text") or "").strip()
    if not text:
        return None
    if len(text) > MAX_CHARS:
        logger.info("meet_chat.truncated", original_chars=len(text), kept=MAX_CHARS)
        text = text[:MAX_CHARS]

    sender = str(body.get("sender") or "").strip() or None
    return ChatMessage(
        text=text,
        sender=sender,
        received_at_us=received_at_us,
        is_self=bool(body.get("isSelf")),
    )


class MeetChatSource:
    """``ChatSource`` fed by the page's chat observer.

    A bounded queue, and small: chat arrives at human typing speed, so anything that fills this
    is a malfunction rather than load. Overflow drops the *newest* — the opposite of the audio
    policy, and correct for the same reason it is wrong there. Audio must stay in real time so
    stale frames go; a conversation must stay coherent, so an earlier question keeps its place
    ahead of a later one.

    **Only messages that ``@``-tag the avatar are forwarded** when ``require_mention`` is set,
    which is the default. A meeting's chat is a conversation between people: participants swap
    links, greet each other and answer among themselves, and an avatar that speaks up after
    every line is interrupting a room that was not talking to it. Requiring the tag makes
    answering opt-in per message, which is how a person in the room behaves.
    """

    __slots__ = (
        "_clock",
        "_dropped",
        "_ignored",
        "_mention_names",
        "_others",
        "_queue",
        "_received",
        "_require_mention",
        "_seen_ids",
        "_started",
        "_warned_nameless",
    )

    def __init__(
        self,
        *,
        clock: MediaClock,
        maxsize: int = 32,
        mention_names: Sequence[str] = (),
        require_mention: bool = True,
    ) -> None:
        self._clock = clock
        self._queue: asyncio.Queue[ChatMessage] = asyncio.Queue(maxsize=maxsize)
        self._seen_ids: set[str] = set()
        self._received = 0
        self._dropped = 0
        self._ignored = 0
        self._started = False
        self._require_mention = require_mention
        self._mention_names: tuple[str, ...] = ()
        # Everyone but the avatar, from the roster — what ``_only_other`` eliminates against.
        self._others: tuple[str, ...] = ()
        self._warned_nameless = False
        for name in mention_names:
            self.observe_self_name(name)

    @property
    def received(self) -> int:
        return self._received

    @property
    def dropped(self) -> int:
        return self._dropped

    @property
    def ignored(self) -> int:
        """Messages seen but not addressed to the avatar. Not a fault — the expected case."""
        return self._ignored

    @property
    def mention_names(self) -> tuple[str, ...]:
        return self._mention_names

    def observe_self_name(self, name: str | None) -> None:
        """Add a name the avatar answers to. Never raises; called from the bridge's read loop.

        The configured ``display_name`` is only what Meet is *told* if it asks for a name, and a
        signed-in profile joins under the Google account's own name instead — so the name
        participants actually see, and therefore the one they will type after the ``@``, is only
        knowable from the roster. Both are registered: the roster's is authoritative, and the
        configured one keeps chat working before the first roster arrives.
        """
        cleaned = " ".join(str(name or "").split())
        if not cleaned:
            return
        if any(cleaned.lower() == known.lower() for known in self._mention_names):
            return
        self._mention_names = (*self._mention_names, cleaned)
        logger.info("meet_chat.mention_name", name=cleaned, total=len(self._mention_names))

    def observe_roster(self, roster: MeetRoster) -> None:
        """Learn the avatar's rendered name, and who else is in the meeting. Never raises.

        Registered as a roster listener, so this runs on the bridge's read loop — dict work over
        a handful of names, on rosters the bridge has already deduplicated.

        The second half is what ``_only_other`` needs. It is the same elimination the transcript
        and the speaker tracker each do, applied where it was missing: a message whose sender the
        page could not read is still, in a two-person meeting, from the one other person present.
        """
        try:
            self.observe_self_name(roster.self_name)
            for participant in roster.participants:
                if participant.is_self:
                    self.observe_self_name(participant.display_name)
            others: list[str] = []
            for participant in roster.others:
                name = " ".join(str(participant.display_name or "").split())
                if not name or self._is_self(name):
                    continue
                if not any(name.casefold() == known.casefold() for known in others):
                    others.append(name)
            self._others = tuple(others)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("meet_chat.roster_failed", error=str(exc))

    def _is_self(self, name: str | None) -> bool:
        if not name:
            return False
        return any(name.casefold() == known.casefold() for known in self._mention_names)

    def _only_other(self) -> str | None:
        """The single other participant, when there is exactly one and it is not us.

        Fails closed on two or more: naming one of them would be a guess, and a confidently wrong
        "Priya asked…" is worse for every downstream answer than an unattributed question.
        """
        if len(self._others) != 1:
            return None
        candidate = self._others[0]
        return None if self._is_self(candidate) else candidate

    async def start(self) -> None:
        self._started = True

    async def stop(self) -> None:
        self._started = False

    def offer(self, body: dict[str, Any], *, message_id: str | None = None) -> bool:
        """Accept one payload from the page. Never blocks, never raises.

        Called from the bridge's read loop, which is the media channel — so this must not be
        able to stall it or throw into it. Both properties are why this is a plain method that
        returns a bool rather than a coroutine that might await.

        Deduplicated on the page's message id because Meet re-renders the chat list on almost
        every DOM mutation: without this, one typed question would be forwarded on every scan
        and the avatar would answer it repeatedly.

        The dedupe happens **before** the mention check, and the id of an ignored message is
        remembered like any other: a message nobody addressed to the avatar does not become
        addressed to it on the next scan, and re-testing it every time the panel re-renders
        would be work with no possible outcome.
        """
        if message_id:
            if message_id in self._seen_ids:
                return False
            self._seen_ids.add(message_id)

        message = parse_chat_message(body, received_at_us=self._clock.now_us())
        if message is None:
            return False

        attributed_by = str(body.get("senderFrom") or "page") if message.sender else "none"
        if message.sender is None and not message.is_self:
            # **The name matters as much as the words, and it was missing on every message.** The
            # agent is told "<sender>: <text>" and has no other way to learn who is talking to it;
            # with no sender it received an anonymous line and answered "I don't know your name"
            # to somebody the roster had named all along.
            inferred = self._only_other()
            if inferred is not None:
                message = replace(message, sender=inferred)
                attributed_by = "elimination"

        message = self._addressed(message)
        if message is None:
            return False

        try:
            self._queue.put_nowait(message)
        except asyncio.QueueFull:
            self._dropped += 1
            logger.warning(
                "meet_chat.dropped",
                reason="queue full",
                dropped_total=self._dropped,
                note="chat is arriving faster than the agent can answer",
            )
            return False

        self._received += 1
        logger.info(
            "meet_chat.received",
            sender=message.sender,
            # How the name was arrived at: a selector on the row, the group heading above it,
            # elimination against the roster, or nothing. ``sender=None`` was logged for a whole
            # meeting and said nothing about *why*, which is the only reason it survived.
            attributed_by=attributed_by,
            chars=len(message.text),
            is_self=message.is_self,
            total=self._received,
        )
        return True

    def _addressed(self, message: ChatMessage) -> ChatMessage | None:
        """The message with its mention stripped, or ``None`` if it was not for the avatar.

        The avatar's own messages skip the check entirely — they carry its name by definition,
        so testing them would only ever say yes. ``AvatarClient.send_chat`` drops them on
        ``is_self`` a step later, and that is the one place the decision belongs.
        """
        if not self._require_mention or message.is_self:
            return message

        if not self._mention_names:
            # Nothing to match against means every message would be ignored, and a silently
            # deaf avatar looks exactly like a broken one. Loud, once, with the fix in it.
            if not self._warned_nameless:
                self._warned_nameless = True
                logger.warning(
                    "meet_chat.no_mention_name",
                    note="chat requires an @mention but no name to match is known yet; "
                    "set MC_GOOGLE_MEET__CHAT_MENTION_NAMES, or "
                    "MC_GOOGLE_MEET__CHAT_REQUIRE_MENTION=false to answer every message",
                )
            return None

        question = strip_mention(message.text, self._mention_names)
        if question is None:
            self._ignored += 1
            logger.debug(
                "meet_chat.not_addressed",
                sender=message.sender,
                chars=len(message.text),
                ignored_total=self._ignored,
            )
            return None

        if question == message.text:
            return message
        return replace(message, text=question)

    async def messages(self) -> AsyncIterator[ChatMessage]:
        """Yield chat messages as the page observes them."""
        while True:
            yield await self._queue.get()

    def health(self) -> ComponentHealth:
        """Always healthy when started.

        There is no such thing as a broken chat source that can be distinguished from a meeting
        where nobody has typed anything, so claiming otherwise would be invention. The
        watchdog's job is noticing missing *audio*; a silent chat is the normal case.
        """
        if not self._started:
            return ComponentHealth.unknown(COMPONENT_NAME, "not started")
        return ComponentHealth.healthy(
            COMPONENT_NAME, f"received={self._received} ignored={self._ignored}"
        )
