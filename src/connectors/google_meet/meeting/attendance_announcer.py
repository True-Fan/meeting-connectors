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

**Why it carries the speaker brief too, rather than a second announcer doing it.** Because an
agent has *one* place to put standing context, and two pushers competing for it is a bug that
looks like an unrelated regression. Observed live: speaker briefs pushed every few seconds
displaced the attendance brief, and the avatar — asked who was in the meeting — answered
"Someone is present in the meeting", having been left holding only the most recent frame. The
two facts belong in one brief because they answer one question between them: *who is here, and
who is talking*. One frame, one slot, no eviction.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress

from src.avatar.client import AvatarClient
from src.connectors.google_meet.meeting.active_speaker import SpeakerTracker
from src.connectors.google_meet.meeting.attendance import AttendanceLedger, AttendanceSnapshot
from src.connectors.google_meet.meeting.transcript import MeetTranscript
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
        "_speakers",
        "_task",
        "_transcript",
    )

    def __init__(
        self,
        *,
        ledger: AttendanceLedger,
        avatar: AvatarClient,
        interval_s: float = DEFAULT_INTERVAL_S,
        settle_s: float = SETTLE_DELAY_S,
        require_negotiation: bool = True,
        speakers: SpeakerTracker | None = None,
        transcript: MeetTranscript | None = None,
    ) -> None:
        self._ledger = ledger
        self._avatar = avatar
        # Optional, and folded into this brief rather than pushed alongside it — see the module
        # docstring for the eviction that made a second announcer the wrong shape. ``None`` when
        # speaker tracking is off, in which case every byte on this wire is what it always was.
        self._speakers = speakers
        # The conversation itself, on the same terms and for the same reason: it is the third thing
        # an agent needs a slot for, and there is one slot. Who is here, who is talking, and what
        # they said are one brief.
        self._transcript = transcript
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

            speakers = self._speakers.snapshot() if self._speakers is not None else None
            transcript = self._transcript.snapshot() if self._transcript is not None else None
            current = signature(snapshot)
            if speakers is not None:
                # The candidate list too, because it is now *in* the brief: somebody unmuting
                # narrows "it could be either of them" down to a name, and a change the frame
                # carries but the signature ignores is a change the agent never hears about.
                current = f"{current}#{','.join(speakers.current)}"
                current = f"{current}#{','.join(sorted(speakers.candidates))}"
            if transcript is not None:
                # The line count, not the text: a new line is news, and re-rendering the same ones
                # is not.
                current = f"{current}#{len(transcript.lines)}"
            if current == self._last:
                return

            brief = snapshot.agent_context()
            # **Included before the first speaking edge, not after it — the gate here was the last
            # thing holding the fix out of the frame.** ``SpeakerSnapshot.agent_context`` now says
            # something useful with zero events: who the voice could be, and that it must not be
            # resolved from the chat history. Requiring ``events`` withheld exactly that sentence
            # during the one moment it was needed — somebody joining and speaking straight away,
            # which is when the avatar answered "what is my name?" with the name of whoever had
            # been typing. Still nothing at all when there is nobody to talk about.
            if speakers is not None and (speakers.events or speakers.candidates):
                brief = f"{brief} {speakers.agent_context()}"
            if transcript is not None:
                said = transcript.agent_context()
                if said:
                    # On its own lines, because it is dialogue and reads as such — and because the
                    # sentences above it are *about* the meeting while this is the meeting.
                    brief = f"{brief}\n\n{said}"

            delivered = await self._avatar.send_meeting_context(
                brief,
                # **``attendance`` even when the brief also names the speaker.** The topic is part
                # of a contract an agent may route on, and renaming it to suit a wider brief would
                # be a breaking change dressed as a tidy-up — an agent handling ``attendance``
                # would silently stop being told who is in the meeting. The brief grew; the
                # channel did not.
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
                    speaking=(speakers.current or None) if speakers is not None else None,
                    transcript_lines=len(transcript.lines) if transcript is not None else None,
                    total=self._sent,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("meet_attendance.context_push_failed", error=str(exc))
