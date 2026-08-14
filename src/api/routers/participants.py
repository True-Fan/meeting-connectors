"""Attendance endpoints — who is in the meeting, who was, and who never came.

**Why the agent pulls this rather than being pushed it.** The obvious design is to send the
roster to the avatar agent as it changes, and it is wrong: the only channel this service has to
the agent is the text frame ``AvatarClient.send_chat`` writes, and everything arriving on it is
something the avatar *says out loud* — that is exactly how a raised hand becomes "of course, go
ahead" (``avatar/client.send_hand_raise``). Pushing attendance down it would make the avatar
announce "Priya joined" to the room, unprompted, every time somebody's wifi hiccuped. So the
ledger is served here and read when the question is actually asked, which is also the only
version that can answer *"who **was** here"* — a question that arrives after the fact.

``agent_context`` in the response is a rendered prose brief rather than a second serialisation
of the same fields, because its destination is a context window. See
``connectors/google_meet/meeting/attendance.AttendanceSnapshot.agent_context``.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from src.api.dependencies import MeetingServiceDep
from src.domain.ids import SessionId

router = APIRouter(prefix="/sessions", tags=["participants"])

_NO_LEDGER = (
    "no attendance record for session {session_id}: the session is unknown, or its "
    "connector does not track attendance, or MC_GOOGLE_MEET__ATTENDANCE_ENABLED is false"
)


class ParticipantRecordResponse(BaseModel):
    """One person's time in the meeting."""

    display_name: str | None = Field(default=None, description="Name as Meet rendered it.")
    present: bool = Field(description="In the meeting as of the most recent roster scan.")
    was_invited: bool = Field(
        description="Appeared on the invite list this session was seeded with."
    )
    never_joined: bool = Field(description="Invited, and never once observed in the meeting.")
    rejoins: int = Field(description="Times they came back after leaving.")
    first_seen_at: datetime | None = Field(
        default=None, description="UTC time they were first observed. Null if they never joined."
    )
    last_seen_at: datetime | None = Field(
        default=None, description="UTC time they were last observed."
    )
    seconds_in_meeting: int = Field(
        description=(
            "First-seen to last-seen. A lower bound: the page can only report somebody as "
            "present when it looked, so this under-reports by up to one scan interval at each end."
        )
    )


class ParticipantsResponse(BaseModel):
    """The attendance ledger for one session."""

    session_id: str
    observed_at: datetime
    roster_scans: int = Field(
        description=(
            "Distinct rosters observed. Zero means the page has not reported one yet — which "
            "is 'not known', not 'nobody is here'."
        )
    )
    self_name: str | None = Field(
        default=None, description="The name the avatar's own account appears under."
    )
    has_invite_list: bool = Field(
        description=(
            "Whether an invite list was ever supplied. False makes never_joined unknowable "
            "rather than empty."
        )
    )
    present: tuple[str, ...] = Field(description="Names in the meeting right now.")
    departed: tuple[str, ...] = Field(description="Names that were here and have left.")
    never_joined: tuple[str, ...] = Field(description="Invited names never observed.")
    participants: tuple[ParticipantRecordResponse, ...] = Field(
        description="Full per-person detail, in the order people arrived."
    )
    agent_context: str = Field(
        description=(
            "A prose brief suitable for dropping into the agent's context window, including "
            "what is not known."
        )
    )

    @classmethod
    def from_snapshot(cls, session_id: str, snapshot: object) -> ParticipantsResponse:
        """Build the response from an ``AttendanceSnapshot``.

        Typed as ``object`` because ``MeetingService`` hands this back duck-typed to stay
        platform-blind — see the note above ``MeetingService.attendance_snapshot``.
        """
        return cls(
            session_id=session_id,
            observed_at=snapshot.observed_at,  # type: ignore[attr-defined]
            roster_scans=snapshot.scans,  # type: ignore[attr-defined]
            self_name=snapshot.self_name,  # type: ignore[attr-defined]
            has_invite_list=snapshot.has_invite_list,  # type: ignore[attr-defined]
            present=tuple(r.label for r in snapshot.present),  # type: ignore[attr-defined]
            departed=tuple(r.label for r in snapshot.departed),  # type: ignore[attr-defined]
            never_joined=tuple(r.label for r in snapshot.never_joined),  # type: ignore[attr-defined]
            participants=tuple(
                ParticipantRecordResponse(
                    display_name=r.display_name,
                    present=r.present,
                    was_invited=r.was_invited,
                    never_joined=r.never_joined,
                    rejoins=r.rejoins,
                    first_seen_at=r.first_seen_at,
                    last_seen_at=r.last_seen_at,
                    seconds_in_meeting=r.duration_us() // 1_000_000,
                )
                for r in snapshot.records  # type: ignore[attr-defined]
            ),
            agent_context=snapshot.agent_context(),  # type: ignore[attr-defined]
        )


class SeedInviteesRequest(BaseModel):
    """Who was invited, from the calendar event the meeting came from."""

    invitees: tuple[str, ...] = Field(
        description=(
            "Display names or email addresses of everyone invited. A name Meet also reports is "
            "preferred over the one given here, so 'priya@example.com' becomes 'Priya Menon' "
            "once she joins."
        )
    )


class SeedInviteesResponse(BaseModel):
    session_id: str
    newly_recorded: int = Field(
        description="Names marked invited by this call. Re-posting the same list returns 0."
    )
    total_invited: int


@router.get(
    "/{session_id}/participants",
    response_model=ParticipantsResponse,
    summary="Who is in the meeting, who was, and who never joined",
)
async def get_participants(session_id: str, service: MeetingServiceDep) -> ParticipantsResponse:
    """The attendance ledger. 404 when the session keeps none."""
    snapshot = service.attendance_snapshot(SessionId(session_id))
    if snapshot is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=_NO_LEDGER.format(session_id=session_id),
        )
    return ParticipantsResponse.from_snapshot(session_id, snapshot)


@router.post(
    "/{session_id}/invitees",
    response_model=SeedInviteesResponse,
    summary="Tell a session who was invited",
)
async def seed_invitees(
    session_id: str, request: SeedInviteesRequest, service: MeetingServiceDep
) -> SeedInviteesResponse:
    """Seed the invite list, so "who never showed up" becomes answerable.

    Additive and idempotent: posting twice marks each person once, and posting after people have
    joined marks the entries they already have rather than duplicating them.
    """
    newly = service.seed_invitees(SessionId(session_id), tuple(request.invitees))
    if newly is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=_NO_LEDGER.format(session_id=session_id),
        )
    snapshot = service.attendance_snapshot(SessionId(session_id))
    total = len(snapshot.invited) if snapshot is not None else newly  # type: ignore[attr-defined]
    return SeedInviteesResponse(session_id=session_id, newly_recorded=newly, total_invited=total)
