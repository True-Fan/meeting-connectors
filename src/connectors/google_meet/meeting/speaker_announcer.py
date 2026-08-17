"""Telling the agent who is speaking, without telling it out loud.

Same shape and same reasoning as ``attendance_announcer.py``, which is why this is a second
small class rather than a branch in that one: a poller reading a snapshot cannot touch the
media path, where a callback hung off ``SpeakerTracker.offer`` would put a network send on the
bridge's read loop — the loop that also carries the meeting's audio.

**Delivered as ``meeting_context``, never as chat.** That distinction is the whole safety
property. A chat frame is a turn the avatar *says out loud* — it is how a raised hand becomes
"of course, go ahead" — so pushing speaker changes down it would have the avatar narrate the
meeting: "Priya is speaking now", out loud, every time somebody took a breath. Context is
silent: the agent knows, and mentions it only if asked.

**On change, and only on change.** A speaker is standing context, so re-sending an identical
brief is noise in a context window and, worse, an invitation for the agent to comment on it.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress

from src.avatar.client import AvatarClient
from src.connectors.google_meet.meeting.active_speaker import SpeakerSnapshot, SpeakerTracker
from src.infrastructure.logging import get_logger

logger = get_logger(__name__)

COMPONENT_NAME = "google_meet_speaker_announcer"

DEFAULT_INTERVAL_S = 3.0
"""How often the tracker is read.

Faster than attendance's five seconds and for an obvious reason — who is talking changes on the
timescale of a sentence, not of somebody joining — and still far slower than the 200 ms energy
sampler that produces the edges. The gap between the two is deliberate: the *history* is exact
to a fifth of a second, and what the agent is told about it collapses a burst of turn-taking
into one brief."""

SETTLE_DELAY_S = 1.5
"""Grace before the first push, matching the attendance announcer's.

The roster is still assembling in the first moment after joining, and the first turns of a
meeting are exactly the ones whose names arrive late. One settle interval means the agent's
first brief names people rather than track ids."""


def signature(snapshot: SpeakerSnapshot) -> str:
    """What counts as a change worth telling the agent about.

    Who is speaking, and nothing else. Talk times move on every poll and turn counts move on
    every clause, so including either would make every tick look like news.
    """
    return ",".join(snapshot.current)


class SpeakerAnnouncer:
    """Watches a ``SpeakerTracker`` and pushes who is speaking to the agent on change.

    Owns one task and never raises out of it: failing to deliver context must not fail a session
    that is carrying audio in both directions, so a send error is logged and the next change
    tries again. The HTTP endpoint is unaffected either way.
    """

    __slots__ = (
        "_avatar",
        "_interval_s",
        "_last",
        "_require_negotiation",
        "_sent",
        "_settle_s",
        "_task",
        "_tracker",
    )

    def __init__(
        self,
        *,
        tracker: SpeakerTracker,
        avatar: AvatarClient,
        interval_s: float = DEFAULT_INTERVAL_S,
        settle_s: float = SETTLE_DELAY_S,
        require_negotiation: bool = True,
    ) -> None:
        self._tracker = tracker
        self._avatar = avatar
        self._require_negotiation = require_negotiation
        # A floor against a pathological value only — zero would busy-loop. The operator-facing
        # minimum lives in ``GoogleMeetSettings``, where policy belongs.
        self._interval_s = max(float(interval_s), 0.01)
        self._settle_s = max(float(settle_s), 0.0)
        self._last: str | None = None
        self._sent = 0
        self._task: asyncio.Task[None] | None = None

    @property
    def sent(self) -> int:
        return self._sent

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="meet-speaker-announcer")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _run(self) -> None:
        await asyncio.sleep(self._settle_s)
        while True:
            await self._tick()
            await asyncio.sleep(self._interval_s)

    async def _tick(self) -> None:
        """One comparison, and a push if the floor changed hands. Never raises."""
        try:
            snapshot = self._tracker.snapshot()
            if not snapshot.events:
                # Nothing has been heard yet. "Nobody has spoken" is true, useless, and would
                # have to be corrected the moment anybody does.
                return

            current = signature(snapshot)
            if current == self._last:
                return

            delivered = await self._avatar.send_meeting_context(
                snapshot.agent_context(),
                topic="speaker",
                require_negotiation=self._require_negotiation,
            )
            # Recorded only on delivery, so an agent that connects late or reconnects is caught
            # up on the next tick rather than never — the speaker has not changed, but this
            # agent has not been told.
            if delivered:
                self._last = current
                self._sent += 1
                logger.info(
                    "meet_speaker.context_pushed",
                    speaking=snapshot.current or None,
                    total=self._sent,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("meet_speaker.context_push_failed", error=str(exc))
