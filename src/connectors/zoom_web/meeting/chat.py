"""Answering chat messages that tag the avatar, and ignoring the rest of the room.

Two responsibilities, deliberately separate: deciding whether a message was addressed to
the avatar at all, and handing the survivors to the router as they arrive. Parsing is not
one of them — Zoom delivers a chat message over RTMS with the sender's name on it, so
``connectors/zoom/rtms/mapping.to_chat_message`` has already produced a domain
``ChatMessage`` before anything here runs. The Meet connector's equivalent spends most of
its length reconstructing that from a DOM.

**Why the ``@`` is required by default.** A meeting's chat is a conversation between
people: participants swap links, greet each other, and answer among themselves. An avatar
that speaks up after every line is interrupting a room that was not talking to it.
Requiring the tag makes answering opt-in per message, which is how a person in the room
behaves.

**Why the page cannot be trusted to decide it, and here neither can RTMS.** What deserves
an answer is policy, and policy lives in Python beside the settings that govern it. Zoom
reports what was typed; this decides what it means.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Sequence
from dataclasses import replace

from src.domain.health import ComponentHealth
from src.domain.meeting import ChatMessage
from src.infrastructure.logging import get_logger

logger = get_logger(__name__)

COMPONENT_NAME = "zoom_web_chat"

_MENTION_CACHE: dict[str, re.Pattern[str]] = {}

_LEADING_PUNCTUATION = " \t\n\r,.:;!?-–—@"  # noqa: RUF001 — people type both dashes
"""Trimmed from the front of what is left once a mention is removed, so "@Avatar, what is
the CTC?" reaches the agent as "what is the CTC?" rather than ", what is the CTC?"."""


def _mention_pattern(name: str) -> re.Pattern[str] | None:
    """A compiled matcher for one name the avatar answers to, or ``None`` if unusable.

    **The ``@`` is required.** Zoom's chat box does offer an ``@`` autocomplete, but what
    arrives over RTMS is plain text with no participant token in it — so the ``@`` is the
    only deliberate signal that survives the wire, and it is the difference between talking
    *to* the avatar and talking *about* it. "did the AI avatar join?" is a question for the
    room; "@AI Avatar are you there?" is a question for the avatar.

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
    """Return ``text`` with the ``@mention`` removed, or ``None`` if the avatar was not tagged.

    The mention is stripped rather than left in place because what survives becomes an LLM
    prompt: "@Avatar what is the notice period?" is a question about the notice period, and
    leaving the vocative in invites the agent to answer a question about its own name.

    A message that is *only* a mention keeps its original text. "@Avatar" alone is somebody
    getting the bot's attention, and forwarding an empty string would have it drop the
    message silently — better that the agent sees the greeting and says something.
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
        # Only the gap the removal left, not every run: line structure is left alone
        # because a pasted question can carry meaningful newlines.
        remainder = re.sub(r"[ \t]{2,}", " ", remainder)
        return remainder or text
    return None


class ZoomChatSource:
    """``ChatSource`` fed by RTMS chat messages.

    A bounded queue, and small: chat arrives at human typing speed, so anything that fills
    this is a malfunction rather than load. Overflow drops the *newest* — the opposite of
    the audio policy, and correct for the same reason it is wrong there. Audio must stay in
    real time so stale frames go; a conversation must stay coherent, so an earlier question
    keeps its place ahead of a later one.
    """

    __slots__ = (
        "_dropped",
        "_ignored",
        "_mention_names",
        "_others",
        "_queue",
        "_received",
        "_require_mention",
        "_started",
        "_warned_nameless",
    )

    def __init__(
        self,
        *,
        maxsize: int = 32,
        mention_names: Sequence[str] = (),
        require_mention: bool = True,
    ) -> None:
        self._queue: asyncio.Queue[ChatMessage] = asyncio.Queue(maxsize=maxsize)
        self._received = 0
        self._dropped = 0
        self._ignored = 0
        self._started = False
        self._require_mention = require_mention
        self._mention_names: tuple[str, ...] = ()
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
        """Add a name the avatar answers to. Never raises; called from the RTMS pump."""
        cleaned = " ".join(str(name or "").split())
        if not cleaned:
            return
        if any(cleaned.lower() == known.lower() for known in self._mention_names):
            return
        self._mention_names = (*self._mention_names, cleaned)
        logger.info("zoom_chat.mention_name", name=cleaned, total=len(self._mention_names))

    def observe_participants(self, present: tuple[str, ...]) -> None:
        """Learn who else is in the meeting, for elimination. Never raises.

        Only reachable when Zoom sends a chat message without a sender, which it does not
        normally do — kept because a nameless question is answered with "I don't know your
        name" to somebody the roster knew all along, and that failure is expensive where
        the fix is four lines.
        """
        try:
            names: list[str] = []
            for candidate in present:
                name = " ".join(str(candidate or "").split())
                if not name or self._is_self(name):
                    continue
                if not any(name.casefold() == known.casefold() for known in names):
                    names.append(name)
            self._others = tuple(names)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("zoom_chat.participants_failed", error=str(exc))

    def _is_self(self, name: str | None) -> bool:
        if not name:
            return False
        return any(name.casefold() == known.casefold() for known in self._mention_names)

    def _only_other(self) -> str | None:
        """The single other participant, when there is exactly one and it is not us.

        Fails closed on two or more: naming one of them would be a guess, and a confidently
        wrong "Priya asked…" is worse for every downstream answer than an unattributed
        question.
        """
        if len(self._others) != 1:
            return None
        candidate = self._others[0]
        return None if self._is_self(candidate) else candidate

    async def start(self) -> None:
        self._started = True

    async def stop(self) -> None:
        self._started = False

    def offer(self, message: ChatMessage) -> bool:
        """Accept one chat message from RTMS. Never blocks, never raises.

        Called from the RTMS pump, which is the media channel — so this must not be able to
        stall it or throw into it. Both properties are why it is a plain method returning a
        bool rather than a coroutine that might await.
        """
        try:
            return self._offer(message)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("zoom_chat.offer_failed", error=str(exc))
            return False

    def _offer(self, message: ChatMessage) -> bool:
        if not message.text.strip():
            return False

        # **The avatar's own messages, recognised by name.** Zoom labels a chat message with
        # the sender's display name and nothing that says "this is you", so ``is_self``
        # arrives False on the avatar's own line. Left as it was, an avatar that ever posts
        # to the chat would answer itself — the text-channel version of the echo loop
        # ``SelfAudioFilter`` exists to break.
        if not message.is_self and self._is_self(message.sender):
            message = replace(message, is_self=True)

        attributed_by = "zoom" if message.sender else "none"
        if message.sender is None and not message.is_self:
            inferred = self._only_other()
            if inferred is not None:
                message = replace(message, sender=inferred)
                attributed_by = "elimination"

        addressed = self._addressed(message)
        if addressed is None:
            return False

        try:
            self._queue.put_nowait(addressed)
        except asyncio.QueueFull:
            self._dropped += 1
            logger.warning(
                "zoom_chat.dropped",
                reason="queue full",
                dropped_total=self._dropped,
                note="chat is arriving faster than the agent can answer",
            )
            return False

        self._received += 1
        logger.info(
            "zoom_chat.received",
            sender=addressed.sender,
            attributed_by=attributed_by,
            chars=len(addressed.text),
            is_self=addressed.is_self,
            total=self._received,
        )
        return True

    def _addressed(self, message: ChatMessage) -> ChatMessage | None:
        """The message with its mention stripped, or ``None`` if it was not for the avatar.

        The avatar's own messages skip the check entirely — they carry its name by
        definition, so testing them would only ever say yes. ``AvatarClient.send_chat``
        drops them on ``is_self`` a step later, and that is the one place the decision
        belongs.
        """
        if not self._require_mention or message.is_self:
            return message

        if not self._mention_names:
            # Nothing to match against means every message would be ignored, and a silently
            # deaf avatar looks exactly like a broken one. Loud, once, with the fix in it.
            if not self._warned_nameless:
                self._warned_nameless = True
                logger.warning(
                    "zoom_chat.no_mention_name",
                    note="chat requires an @mention but no name to match is known yet; "
                    "set MC_ZOOM_WEB__CHAT_MENTION_NAMES, or "
                    "MC_ZOOM_WEB__CHAT_REQUIRE_MENTION=false to answer every message",
                )
            return None

        question = strip_mention(message.text, self._mention_names)
        if question is None:
            self._ignored += 1
            logger.debug(
                "zoom_chat.not_addressed",
                sender=message.sender,
                chars=len(message.text),
                ignored_total=self._ignored,
            )
            return None

        if question == message.text:
            return message
        return replace(message, text=question)

    async def messages(self) -> AsyncIterator[ChatMessage]:
        """Yield chat messages as Zoom reports them."""
        while True:
            yield await self._queue.get()

    def health(self) -> ComponentHealth:
        """Always healthy when started.

        There is no such thing as a broken chat source that can be distinguished from a
        meeting where nobody has typed anything, so claiming otherwise would be invention.
        """
        if not self._started:
            return ComponentHealth.unknown(COMPONENT_NAME, "not started")
        return ComponentHealth.healthy(
            COMPONENT_NAME, f"received={self._received} ignored={self._ignored}"
        )
