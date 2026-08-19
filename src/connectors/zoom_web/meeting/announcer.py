"""Pushing the meeting brief to the agent, so it can answer without being asked twice.

**Why this exists at all.** The ledgers alone are not enough. The Google Meet connector
learned this in a live meeting: the bridge knew exactly who was present and the agent
still answered *"I don't have access to your meeting details or a list of participants"* —
because nothing carried the ledger across the avatar socket. Serving it over HTTP makes it
*available*; this makes the agent *hold* it.

**Why a poller and not a callback on the observer.** ``ZoomMeetingObserver`` runs on the
RTMS pump, which is the media channel — the one place in this connector that must not be
given anything that can block. Sending on the avatar socket is I/O with a queue behind it,
so hanging it off that observer would put a network send on the path that also carries
audio. A separate task reading a snapshot is the same information at a cost that cannot
touch media.

**Why it sends on change rather than on a timer.** The brief is standing context, so
resending an identical one is pure noise in the agent's context window; and an agent that
is re-told the same roster every few seconds is being handed a reason to mention it. So a
signature of what actually matters is compared, and only a real change is pushed.

**Why one announcer carries all three briefs.** Because an agent has *one* slot for
standing context, and two pushers competing for it is a bug that looks like an unrelated
regression. The Meet connector observed exactly that: speaker briefs pushed every few
seconds displaced the attendance brief, and the avatar — asked who was in the meeting —
answered "Someone is present in the meeting", having been left holding only the most recent
frame. Who is here, who is talking, and what was said answer one question between them.
One frame, one slot, no eviction.

**Delivered as ``meeting_context``, never as chat**, and that distinction is the whole
safety property. A chat frame is a turn the avatar *says out loud* — it is how a raised
hand becomes "of course, go ahead". Pushing the roster down it would have the avatar
announce "Priya joined" to the room every time somebody's wifi hiccuped. Context is silent:
the agent knows, and mentions it only if asked.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress

from src.avatar.client import AvatarClient
from src.connectors.zoom_web.meeting.active_speaker import ZoomSpeakerTracker
from src.connectors.zoom_web.meeting.attendance import (
    AttendanceSnapshot,
    ZoomAttendanceLedger,
)
from src.connectors.zoom_web.meeting.transcript import ZoomTranscript
from src.infrastructure.logging import get_logger

logger = get_logger(__name__)

COMPONENT_NAME = "zoom_web_announcer"

DEFAULT_INTERVAL_S = 3.0
"""How often the ledgers are read.

Not how often anything is sent — a still meeting sends nothing. Three seconds is the
timescale of a sentence, which is what "who is talking" changes on; who is *here* changes
far more slowly, and folding both into one brief means the faster of the two sets the
cadence."""

SETTLE_DELAY_S = 1.5
"""Grace before the first push.

RTMS attaches a moment after the browser joins, and the first participant events arrive in
a burst. Waiting one interval means the agent's first brief is the meeting as it is, rather
than a snapshot mid-arrival followed by a correction."""


def signature(snapshot: AttendanceSnapshot) -> str:
    """What counts as an attendance change worth telling the agent about.

    Presence, departure and never-joined — not durations or last-seen times, which move on
    every event and would make every poll look like news.
    """
    return "|".join(
        (
            ",".join(sorted(r.label for r in snapshot.present)),
            ",".join(sorted(r.label for r in snapshot.departed)),
            ",".join(sorted(r.label for r in snapshot.never_joined)),
        )
    )


class ZoomMeetingAnnouncer:
    """Watches the ledgers and pushes one combined brief to the agent on change.

    Owns one task and never raises out of it: failing to deliver context must not fail a
    session that is otherwise carrying audio in both directions, so a send error is logged
    and the loop keeps going. The next change tries again, and the HTTP endpoints are
    unaffected either way.
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
        avatar: AvatarClient,
        ledger: ZoomAttendanceLedger | None = None,
        speakers: ZoomSpeakerTracker | None = None,
        transcript: ZoomTranscript | None = None,
        interval_s: float = DEFAULT_INTERVAL_S,
        settle_s: float = SETTLE_DELAY_S,
        require_negotiation: bool = True,
    ) -> None:
        self._avatar = avatar
        self._ledger = ledger
        self._speakers = speakers
        self._transcript = transcript
        self._require_negotiation = require_negotiation
        # A floor against a pathological value only — zero or negative would busy-loop. The
        # operator-facing minimum lives in ``ZoomWebSettings``, where policy belongs;
        # clamping to it here too would override a caller with a legitimate reason to poll
        # faster, which is what tests do.
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
        self._task = asyncio.create_task(self._run(), name="zoom-web-announcer")

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
            attendance = self._ledger.snapshot() if self._ledger is not None else None
            speakers = self._speakers.snapshot() if self._speakers is not None else None
            transcript = self._transcript.snapshot() if self._transcript is not None else None

            current = self._signature(attendance, speakers, transcript)
            if current is None or current == self._last:
                return

            brief = self._brief(attendance, speakers, transcript)
            if not brief:
                return

            delivered = await self._avatar.send_meeting_context(
                brief,
                # ``attendance`` even though the brief carries more than attendance. The
                # topic is part of a contract an agent may route on, and renaming it to suit
                # a wider brief would be a breaking change dressed as a tidy-up — an agent
                # handling ``attendance`` would silently stop being told who is in the
                # meeting. The brief grew; the channel did not.
                topic="attendance",
                require_negotiation=self._require_negotiation,
            )
            # Recorded only on delivery, so an agent that connects late or reconnects gets
            # the current brief on the next tick rather than never — nothing has changed,
            # but *this* agent has not been told.
            if delivered:
                self._last = current
                self._sent += 1
                logger.info(
                    "zoom_web.context_pushed",
                    present=len(attendance.present) if attendance is not None else None,
                    speaking=(speakers.current or None) if speakers is not None else None,
                    transcript_lines=len(transcript.lines) if transcript is not None else None,
                    total=self._sent,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("zoom_web.context_push_failed", error=str(exc))

    def _signature(
        self,
        attendance: AttendanceSnapshot | None,
        speakers: object | None,
        transcript: object | None,
    ) -> str | None:
        """What this tick would say, reduced to a comparable string.

        ``None`` means there is nothing worth saying yet — no events at all — which is
        distinct from "nothing changed": sending "attendance is unknown" would be true,
        useless, and would have to be corrected a second later.
        """
        parts: list[str] = []
        interesting = False

        if attendance is not None and attendance.scans:
            parts.append(signature(attendance))
            interesting = True
        if speakers is not None:
            # The candidate list as well as the current speaker, because it is *in* the
            # brief: somebody joining narrows "it could be either of them" down to a name,
            # and a change the frame carries but the signature ignores is a change the agent
            # never hears about.
            parts.append(",".join(speakers.current))  # type: ignore[attr-defined]
            parts.append(",".join(sorted(speakers.candidates)))  # type: ignore[attr-defined]
            interesting = interesting or bool(speakers.events)  # type: ignore[attr-defined]
        if transcript is not None:
            # **The *chat* line count, not every line, and this one is about reply latency.**
            # Re-sending standing context makes the agent rebuild its frame and throws away
            # the reply it had begun preparing; a transcribed line arrives every couple of
            # seconds *while somebody is talking*, so keying on the total meant a push
            # landing on almost every sentence, at the exact moment the answer was being
            # formed. It also bought nothing: the agent transcribes the meeting's audio
            # itself, so a transcribed line is the one thing in this brief it already knows.
            # A typed line it cannot know, and arrives at typing speed.
            #
            # Spoken lines still reach the agent — the next push carries every line the
            # ledger holds. They no longer *cause* one.
            parts.append(str(transcript.chat_lines))  # type: ignore[attr-defined]
            interesting = interesting or bool(transcript.chat_lines)  # type: ignore[attr-defined]

        return "#".join(parts) if interesting else None

    def _brief(
        self,
        attendance: AttendanceSnapshot | None,
        speakers: object | None,
        transcript: object | None,
    ) -> str:
        """The three briefs, joined into the one frame the agent has a slot for."""
        parts: list[str] = []
        if attendance is not None and attendance.scans:
            parts.append(attendance.agent_context())
        if speakers is not None and (
            speakers.events or speakers.candidates  # type: ignore[attr-defined]
        ):
            # Included before the first speaking event, not after it: the speaker brief says
            # something useful with zero events — who the voice could be, and that it must
            # not be resolved from the chat history — and that is exactly the moment it is
            # needed, when somebody joins and speaks straight away.
            parts.append(speakers.agent_context())  # type: ignore[attr-defined]

        brief = " ".join(part for part in parts if part)
        if transcript is not None:
            said = transcript.agent_context()  # type: ignore[attr-defined]
            if said:
                # On its own lines, because it is dialogue and reads as such — and because
                # the sentences above it are *about* the meeting while this is the meeting.
                brief = f"{brief}\n\n{said}" if brief else said
        return brief
