"""Turning an observed raised hand into a request for the avatar to stop talking.

The same split as ``meeting/chat.py``, for the same reason: ``bridge.js`` reports that a hand
went up in the DOM, and every judgement about what that means — whether it was ours, whether we
have already reacted to this person, and what the agent is told — is made here.

**What the page cannot decide.** It cannot know the avatar's own name reliably (the roster
does), it cannot hold a rate limit that survives a rejoin, and it must not carry the wording
sent to an LLM. Those are policy, and policy lives in Python.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from src.domain.health import ComponentHealth
from src.domain.meeting import HandRaise
from src.infrastructure.logging import get_logger
from src.services.media.clock import MediaClock

logger = get_logger(__name__)

COMPONENT_NAME = "google_meet_hand_raise"

DEFAULT_PROMPT = (
    "{name} raised their hand and wants to say something. "
    "Stop talking and let them speak — reply briefly, like \"ok, go ahead\"."
)
"""What the agent is told when nothing else is configured.

**Written to read like a chat message, because it is delivered as one.** It travels on the
same frame a typed question does (see ``AvatarClient.send_hand_raise``), so the agent needs no
new concept to act on it — a named participant said something, and what they said is that they
want the floor.

An instruction rather than a script: the bridge contains no AI and does not put words in the
avatar's mouth (doc 003 §0). It says what happened and what to do about it, and the agent
chooses the sentence — which is also why changing the avatar's reply is a settings edit and
not a code change. See ``GoogleMeetSettings.hand_raise_prompt``."""

ANONYMOUS = "Someone"
"""Stands in for the name when Meet renders an indicator it does not attribute. The prompt has
to name somebody, and "Someone raised their hand" is true where an empty gap is unreadable."""


def render_prompt(template: str, name: str | None) -> str:
    """Fill ``template`` with the raiser's name, falling back on a broken template.

    Never raises. An operator-supplied template reaches this from settings, and a stray brace
    or an unknown placeholder in it must not take down the leg that stops the avatar talking
    over people — it should cost the wording, not the feature.
    """
    who = (name or "").strip() or ANONYMOUS
    try:
        return template.format(name=who).strip()
    except (IndexError, KeyError, ValueError):
        logger.warning(
            "meet_hand_raise.bad_prompt_template",
            template=template[:120],
            note="only {name} is substituted; falling back to the default wording",
        )
        return DEFAULT_PROMPT.format(name=who)


class MeetHandRaiseSource:
    """``HandRaiseSource`` fed by the page's hand observer.

    A queue of one, and that number is the design rather than a guess. Raised hands are not
    messages to be answered in order — they are a request for the floor *now*, and by the time
    a second one is read the first has either been acted on or is stale. Overflow drops the
    **newest**, which looks wrong next to chat and is right here for the same underlying
    reason: what is already queued is the interruption that has not been delivered yet, and
    replacing it with a newer one would delay the floor rather than yield it sooner.

    **Rate limited per participant.** Meet's hand indicator lives in a DOM that re-renders
    constantly, and a hand can also be lowered and re-raised by someone impatient. Without a
    cooldown either produces a stream of interrupts, and an avatar that is interrupted
    continuously never gets as far as saying "go ahead".
    """

    __slots__ = (
        "_clock",
        "_cooldown_us",
        "_dropped",
        "_ignored",
        "_last_seen",
        "_prompt",
        "_queue",
        "_received",
        "_self_names",
        "_started",
    )

    def __init__(
        self,
        *,
        clock: MediaClock,
        prompt: str = DEFAULT_PROMPT,
        cooldown_s: float = 10.0,
        maxsize: int = 1,
        self_names: tuple[str, ...] = (),
    ) -> None:
        self._clock = clock
        self._prompt = prompt or DEFAULT_PROMPT
        self._cooldown_us = max(int(cooldown_s * 1_000_000), 0)
        self._queue: asyncio.Queue[HandRaise] = asyncio.Queue(maxsize=maxsize)
        self._last_seen: dict[str, int] = {}
        self._self_names: tuple[str, ...] = ()
        self._received = 0
        self._dropped = 0
        self._ignored = 0
        self._started = False
        for name in self_names:
            self.observe_self_name(name)

    @property
    def received(self) -> int:
        return self._received

    @property
    def dropped(self) -> int:
        return self._dropped

    @property
    def ignored(self) -> int:
        """Hands seen but not acted on — our own, or inside a participant's cooldown."""
        return self._ignored

    @property
    def self_names(self) -> tuple[str, ...]:
        return self._self_names

    def observe_self_name(self, name: str | None) -> None:
        """Add a name that means "this is us". Never raises; called from the read loop.

        The avatar has a hand-raise button like anybody else, and a future feature that clicks
        it must not make the avatar interrupt itself. The page's own ``isSelf`` is honoured
        too; this is the half of the check that knows the account's real rendered name, which
        only the roster reveals — the same reasoning as ``MeetChatSource.observe_self_name``.
        """
        cleaned = " ".join(str(name or "").split())
        if not cleaned:
            return
        if any(cleaned.lower() == known.lower() for known in self._self_names):
            return
        self._self_names = (*self._self_names, cleaned)

    async def start(self) -> None:
        self._started = True

    async def stop(self) -> None:
        self._started = False

    def offer(self, body: dict[str, Any], *, event_id: str | None = None) -> bool:
        """Accept one ``HAND_RAISE`` payload from the page. Never blocks, never raises.

        Called from the bridge's read loop, which is the media channel — so, like
        ``MeetChatSource.offer``, this is a plain method returning a bool rather than a
        coroutine that could stall it or an exception that could tear it down.
        """
        if not isinstance(body, dict):
            return False

        name = str(body.get("name") or "").strip() or None
        key = str(event_id or body.get("id") or "") or (f"name:{name}" if name else "anonymous")
        now_us = self._clock.now_us()

        if self._is_self(body, name):
            self._ignored += 1
            return False

        last = self._last_seen.get(key)
        if last is not None and self._cooldown_us and now_us - last < self._cooldown_us:
            self._ignored += 1
            logger.debug(
                "meet_hand_raise.cooldown",
                participant=name,
                since_ms=(now_us - last) // 1_000,
                ignored_total=self._ignored,
            )
            return False
        self._last_seen[key] = now_us

        event = HandRaise(
            participant=name,
            prompt=render_prompt(self._prompt, name),
            raised_at_us=now_us,
        )
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            # The one already queued has not been delivered yet, so it is the interruption
            # that still needs to happen. Keeping it is what makes the floor change hands
            # sooner rather than later.
            self._dropped += 1
            logger.info(
                "meet_hand_raise.dropped",
                participant=name,
                reason="an earlier raised hand is still waiting to be delivered",
            )
            return False

        self._received += 1
        logger.info(
            "meet_hand_raise.received",
            participant=name,
            total=self._received,
            note="the avatar will stop speaking and hand over",
        )
        return True

    def _is_self(self, body: dict[str, Any], name: str | None) -> bool:
        """Whether this hand is the avatar's own."""
        if bool(body.get("isSelf")):
            return True
        if name is None:
            return False
        return any(name.lower() == known.lower() for known in self._self_names)

    async def events(self) -> AsyncIterator[HandRaise]:
        """Yield raised hands as the page observes them."""
        while True:
            yield await self._queue.get()

    def health(self) -> ComponentHealth:
        """Always healthy when started.

        A meeting where nobody raises a hand is indistinguishable from a broken observer, and
        the same is true of chat — claiming otherwise would be invention. The watchdog's job is
        noticing missing audio.
        """
        if not self._started:
            return ComponentHealth.unknown(COMPONENT_NAME, "not started")
        return ComponentHealth.healthy(
            COMPONENT_NAME, f"received={self._received} ignored={self._ignored}"
        )
