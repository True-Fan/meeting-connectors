"""Yielding the floor — to a raised hand, and to a voice talking over the avatar.

**One source for two signals, because they are the same request.** "Stop, I want to speak" is
what a raised hand means and what somebody starting to talk means, and the router already
knows how to answer it: drop the avatar's queued audio and tell the agent, so it stops
mid-sentence and says something like "of course, go ahead" (``MediaRouter._yield_floor``).
Modelling them as two ports would mean a second leg in the router and a second copy of that
answer, for a distinction nothing downstream acts on.

**Both triggers work on this connector, and that is a property of the ingest leg rather than a
preference.** Energy-based barge-in needs the echo gate to be *open*, which needs the avatar's
own voice to be absent from inbound audio. It is: Teams does not play a participant their own
microphone, and the synthetic microphone lives in an ``AudioContext`` that terminates at a
``MediaStreamDestination`` the tap never watches. So the router's speech detector can measure
the inbound mix while the avatar talks, and it fires on the first syllable — sooner than any
DOM observer can, because it does not wait for Teams to redraw a tile.

The DOM speaker observer feeds ``offer_voice`` as well, and the two converge on the same
handover. That is deliberate: the detector is *fast* and anonymous, the observer is *named* and
late, and the per-participant cooldown below de-duplicates them into one interruption that
knows whose it was.

**Only while the avatar is actually speaking**, which is the one place the voice trigger
deliberately differs from the hand. A hand goes up rarely and means "notice me" whether or not
anybody is talking. Somebody starting to speak happens constantly and means nothing at all when
the avatar is silent — that is just the meeting happening, and the audio is already flowing to
the agent. Firing then would send the agent a "stop talking and let them speak" message on
every sentence anybody utters, and an avatar that answers each of those with "go ahead" is
worse than one that never yields.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any

from src.connectors.teams_web.observations import SpeakerEvent
from src.domain.health import ComponentHealth
from src.domain.meeting import HandRaise
from src.infrastructure.logging import get_logger
from src.services.media.clock import MediaClock

logger = get_logger(__name__)

COMPONENT_NAME = "teams_web_interrupt"

TRIGGER_HAND = "hand"
TRIGGER_VOICE = "voice"

DEFAULT_PROMPT = (
    "{name} wants to say something. "
    'Stop talking and let them speak — reply briefly, like "ok, go ahead".'
)
"""What the agent is told when nothing else is configured.

**Written to read like a chat message, because it is delivered as one.** It travels on the same
frame a typed question does (``AvatarClient.send_hand_raise``), so the agent needs no new
concept to act on it — a named participant said something, and what they said is that they want
the floor.

An instruction rather than a script: the bridge contains no AI and does not put words in the
avatar's mouth (doc 003 §0). It says what happened and what to do about it, and the agent
chooses the sentence — which is also why changing the reply is a settings edit rather than a
code change. See ``TeamsWebSettings.hand_raise_prompt``.

Worded for both triggers: the same sentence covers a hand and a voice, and telling the agent
somebody raised a hand when they simply started talking would have it thank them for raising a
hand they never raised."""

ANONYMOUS = "Someone"
"""Stands in for the name when nothing attributed the request — which on this connector is the
common case for a voice barge-in, because the tapped mix carries no attribution and the router
falls back to this wording while the DOM catches up."""

_MAX_NAME_LEN = 120


def render_prompt(template: str, name: str | None) -> str:
    """Fill ``template`` with the interrupter's name, falling back on a broken template.

    Never raises. An operator-supplied template reaches this from settings, and a stray brace or
    an unknown placeholder must not take down the leg that stops the avatar talking over people
    — it should cost the wording, not the feature.
    """
    who = (name or "").strip() or ANONYMOUS
    try:
        return template.format(name=who).strip()
    except (IndexError, KeyError, ValueError):
        logger.warning(
            "teams_interrupt.bad_prompt_template",
            template=template[:120],
            note="only {name} is substituted; falling back to the default wording",
        )
        return DEFAULT_PROMPT.format(name=who)


class TeamsInterruptSource:
    """``HandRaiseSource`` fed by page-observed hands and page-observed active speakers.

    A queue of one, and that number is the design rather than a guess. These are not messages to
    be answered in order — they are a request for the floor *now*, and by the time a second is
    read the first has either been acted on or is stale. Overflow drops the **newest**, which
    looks wrong next to chat and is right here for the same underlying reason: what is already
    queued is the interruption that has not been delivered yet, and replacing it with a newer one
    would delay the floor rather than yield it sooner.

    **Rate limited per participant**, because both inputs repeat. The page re-reads a hand that
    has not moved, and it re-reports the same active speaker through a conversation; without a
    cooldown either produces a stream of interrupts, and an avatar that is interrupted
    continuously never gets as far as saying "go ahead".
    """

    __slots__ = (
        "_clock",
        "_cooldown_us",
        "_dropped",
        "_hands",
        "_ignored",
        "_is_avatar_speaking",
        "_last_seen",
        "_prompt",
        "_queue",
        "_received",
        "_self_names",
        "_started",
        "_voice_enabled",
        "_voices",
    )

    def __init__(
        self,
        *,
        clock: MediaClock,
        prompt: str = DEFAULT_PROMPT,
        cooldown_s: float = 10.0,
        maxsize: int = 1,
        self_names: tuple[str, ...] = (),
        voice_enabled: bool = True,
        is_avatar_speaking: Callable[[], bool] | None = None,
    ) -> None:
        self._clock = clock
        self._prompt = prompt or DEFAULT_PROMPT
        self._cooldown_us = max(int(cooldown_s * 1_000_000), 0)
        self._queue: asyncio.Queue[HandRaise] = asyncio.Queue(maxsize=maxsize)
        self._last_seen: dict[str, int] = {}
        self._self_names: tuple[str, ...] = ()
        self._voice_enabled = voice_enabled
        # ``None`` means "always", which is what a test wants and what an operator gets if the
        # wiring is ever incomplete. Erring towards interrupting is the safer default of the
        # two: an avatar that yields when it did not need to is polite, and one that never
        # yields is the complaint this feature exists to answer.
        self._is_avatar_speaking = is_avatar_speaking
        self._received = 0
        self._dropped = 0
        self._ignored = 0
        self._hands = 0
        self._voices = 0
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
        """Signals seen and not acted on — our own, inside a cooldown, or a voice while the
        avatar was not talking. All three are the expected case, not a fault."""
        return self._ignored

    @property
    def hands(self) -> int:
        """Interruptions that came from a raised hand."""
        return self._hands

    @property
    def voices(self) -> int:
        """Interruptions that came from somebody talking over the avatar."""
        return self._voices

    @property
    def self_names(self) -> tuple[str, ...]:
        return self._self_names

    def observe_self_name(self, name: str | None) -> None:
        """Add a name that means "this is us". Never raises.

        Load-bearing on the voice path rather than merely defensive: the avatar *is* an active
        speaker whenever it talks, so the page draws it taking the floor every time. Without
        this the avatar would interrupt itself, continuously, for as long as it spoke — which is
        the barge-in version of the echo loop.
        """
        cleaned = " ".join(str(name or "").split())[:_MAX_NAME_LEN]
        if not cleaned:
            return
        if any(cleaned.lower() == known.lower() for known in self._self_names):
            return
        self._self_names = (*self._self_names, cleaned)

    async def start(self) -> None:
        self._started = True

    async def stop(self) -> None:
        self._started = False

    # -- inputs ------------------------------------------------------------

    def offer_hand(self, body: dict[str, Any]) -> bool:
        """Accept one ``handRaise`` payload from the page. Never blocks, never raises.

        Called from the page server's read loop, so — like every other observer in this
        connector — a plain method returning a bool rather than a coroutine that could stall it
        or an exception that could tear it down.

        Unconditional on whether the avatar is currently speaking, unlike a voice: it is the
        same request either way, and a silent avatar drops nothing, mutes nothing audible, and
        still tells the agent that somebody wants the floor — which is what makes it say "go
        ahead" rather than sit there.
        """
        try:
            if not isinstance(body, dict):
                return False
            name = " ".join(str(body.get("name") or "").split())[:_MAX_NAME_LEN] or None
            if bool(body.get("isSelf")) or self._is_self(name):
                self._ignored += 1
                return False
            key = str(body.get("id") or "") or (
                f"name:{name.casefold()}" if name else "anonymous"
            )
            return self._raise(name, key=key, trigger=TRIGGER_HAND)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("teams_interrupt.hand_failed", error=str(exc))
            return False

    def offer_voice(self, event: SpeakerEvent) -> bool:
        """Accept one active-speaker observation as a request for the floor. Never raises.

        Gated on the avatar actually speaking — see the module docstring for why this is the
        one place the two triggers behave differently.
        """
        try:
            if not self._voice_enabled:
                return False
            name = " ".join(str(event.display_name or "").split())[:_MAX_NAME_LEN] or None
            if self._is_self(name):
                self._ignored += 1
                return False
            if not self._avatar_is_talking():
                # Somebody started talking and nothing was talking over them. This is the
                # meeting happening; their audio is already on its way to the agent.
                self._ignored += 1
                return False
            key = (
                f"name:{name.casefold()}"
                if name
                else (f"id:{event.user_id}" if event.user_id is not None else "anonymous")
            )
            return self._raise(name, key=key, trigger=TRIGGER_VOICE)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("teams_interrupt.voice_failed", error=str(exc))
            return False

    def _avatar_is_talking(self) -> bool:
        """Whether the avatar is mid-sentence right now.

        Total by construction: this runs on the page read loop, so a predicate that raises must
        cost the interruption rather than the socket. Failing *open* — treating an error as
        "yes, it is talking" — because a missed barge-in is the failure this feature exists to
        fix, and a spurious one is merely polite.
        """
        predicate = self._is_avatar_speaking
        if predicate is None:
            return True
        try:
            return bool(predicate())
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("teams_interrupt.speaking_check_failed", error=str(exc))
            return True

    def _raise(self, name: str | None, *, key: str, trigger: str) -> bool:
        """Queue one request for the floor, subject to the per-participant cooldown."""
        now_us = self._clock.now_us()
        last = self._last_seen.get(key)
        if last is not None and self._cooldown_us and now_us - last < self._cooldown_us:
            self._ignored += 1
            logger.debug(
                "teams_interrupt.cooldown",
                participant=name,
                trigger=trigger,
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
                "teams_interrupt.dropped",
                participant=name,
                trigger=trigger,
                reason="an earlier request for the floor is still waiting to be delivered",
            )
            return False

        self._received += 1
        if trigger == TRIGGER_VOICE:
            self._voices += 1
        else:
            self._hands += 1
        logger.info(
            "teams_interrupt.received",
            participant=name,
            trigger=trigger,
            total=self._received,
            note="the avatar will stop speaking and hand over",
        )
        return True

    def _is_self(self, name: str | None) -> bool:
        if not name:
            return False
        return any(name.lower() == known.lower() for known in self._self_names)

    # -- output ------------------------------------------------------------

    async def events(self) -> AsyncIterator[HandRaise]:
        """Yield requests for the floor as they arrive."""
        while True:
            yield await self._queue.get()

    def health(self) -> ComponentHealth:
        """Always healthy when started.

        A meeting where nobody interrupts is indistinguishable from a broken observer, so
        claiming otherwise would be invention. The counts are in the detail, which is what an
        operator checking whether the feature is live actually needs.
        """
        if not self._started:
            return ComponentHealth.unknown(COMPONENT_NAME, "not started")
        return ComponentHealth.healthy(
            COMPONENT_NAME,
            f"hands={self._hands} voices={self._voices} ignored={self._ignored}",
        )
