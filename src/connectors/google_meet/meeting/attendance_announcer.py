"""Pushing the attendance brief to the agent, so it can answer without being asked twice.

**Why this exists at all.** The ledger alone was not enough. In a live meeting the bridge knew
exactly who was present and the agent still answered *"I don't have access to your meeting
details or a list of participants"* — because nothing carried the ledger across the avatar
socket. Serving it over HTTP makes it *available*; this makes the agent *hold* it.

**Why a poller and not a callback on the roster listener.** ``AttendanceLedger.observe_roster``
runs on the bridge's read loop, which is the media channel — the one place in this connector
that must not be given anything that can block. Sending on the avatar socket is I/O with a
queue behind it, so hanging it off that listener would put a network send on the path that also
carries audio. A separate task reading a snapshot is the same information at a cost that cannot
touch media.

**Why it sends on change rather than on a timer.** The brief is standing context, so resending
an identical one is pure noise in the agent's context window; and an agent that is re-told the
same roster every five seconds is being handed a reason to mention it. So the signature of
*who is present, who left and who is missing* is compared, and only a real change is pushed.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress

from src.avatar.client import AvatarClient
from src.connectors.google_meet.meeting.attendance import AttendanceLedger, AttendanceSnapshot
from src.infrastructure.logging import get_logger

logger = get_logger(__name__)

COMPONENT_NAME = "google_meet_attendance_announcer"

DEFAULT_INTERVAL_S = 5.0
"""How often the ledger is read.

Not how often anything is sent — a still meeting sends nothing. Five seconds is chosen against
the page's own 250 ms scan floor: it is slow enough that a burst of roster churn (somebody
reconnecting, Meet re-rendering a tile) collapses into one push, and fast enough that the agent
knows about a new arrival before that person has finished saying hello."""

SETTLE_DELAY_S = 1.5
"""Grace before the first push.

The roster is read from a DOM that is still assembling itself in the first moment after joining
— the run this was written against reported a garbage tile, then the real name, then a
departure, all within two seconds. Waiting one settle interval means the agent's first brief is
the meeting as it actually is, rather than a snapshot of Meet mid-render followed by a
correction.
"""


def signature(snapshot: AttendanceSnapshot) -> str:
    """What counts as a change worth telling the agent about.

    Presence, departure and never-joined — not durations or last-seen times, which move on every
    scan and would make every poll look like news.
    """
    return "|".join(
        (
            ",".join(sorted(r.label for r in snapshot.present)),
            ",".join(sorted(r.label for r in snapshot.departed)),
            ",".join(sorted(r.label for r in snapshot.never_joined)),
        )
    )


class AttendanceAnnouncer:
    """Watches an ``AttendanceLedger`` and pushes its brief to the agent on change.

    Owns one task. Never raises out of it: a failure to deliver context must not fail a session
    that is otherwise carrying audio in both directions, so a send error is logged and the loop
    keeps going — the next change tries again, and the HTTP endpoint is unaffected either way.
    """

    __slots__ = (
        "_avatar",
        "_interval_s",
        "_last",
        "_ledger",
        "_require_negotiation",
        "_sent",
        "_settle_s",
        "_task",
    )

    def __init__(
        self,
        *,
        ledger: AttendanceLedger,
        avatar: AvatarClient,
        interval_s: float = DEFAULT_INTERVAL_S,
        settle_s: float = SETTLE_DELAY_S,
        require_negotiation: bool = True,
    ) -> None:
        self._ledger = ledger
        self._avatar = avatar
        self._require_negotiation = require_negotiation
        # A floor against a pathological value only — zero or negative would busy-loop. The
        # operator-facing minimum is 0.5 s and lives in ``GoogleMeetSettings``, where policy
        # belongs; clamping to it here as well would silently override a caller that has a
        # legitimate reason to poll faster, which is what tests do.
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
        self._task = asyncio.create_task(self._run(), name="meet-attendance-announcer")

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
        """One comparison, and a push if anything changed. Never raises."""
        try:
            snapshot = self._ledger.snapshot()
            if not snapshot.scans:
                # No roster observed yet. Sending "attendance is unknown" would be true and
                # useless, and would have to be corrected a second later.
                return

            current = signature(snapshot)
            if current == self._last:
                return

            delivered = await self._avatar.send_meeting_context(
                snapshot.agent_context(),
                topic="attendance",
                require_negotiation=self._require_negotiation,
            )
            # Recorded only on delivery, so an agent that connects late or reconnects gets the
            # current brief on the next tick rather than never — the roster has not changed, but
            # this agent has not been told.
            if delivered:
                self._last = current
                self._sent += 1
                logger.info(
                    "meet_attendance.context_pushed",
                    present=len(snapshot.present),
                    departed=len(snapshot.departed),
                    never_joined=len(snapshot.never_joined),
                    total=self._sent,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("meet_attendance.context_push_failed", error=str(exc))
