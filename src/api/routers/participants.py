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


_NO_SPEAKERS = (
    "no speaker record for session {session_id}: the session is unknown, or its connector "
    "cannot identify a speaker, or MC_GOOGLE_MEET__SPEAKER_TRACKING_ENABLED is false"
)


class SpeakerTurnResponse(BaseModel):
    """One stretch of one participant talking."""

    display_name: str | None = Field(
        default=None,
        description=(
            "Name as Meet rendered it. Null when the speech was heard but could not be "
            "attributed — the level was measured and nothing on the page said whose it was."
        ),
    )
    speaker: str = Field(
        description="What to call this speaker in an answer. Never empty; 'Someone' when unknown."
    )
    source: str = Field(
        description=(
            "How the turn was first observed. 'audio' is per-track level measured beside the "
            "capture graph; 'dom' is Meet's own speaking indicator."
        )
    )
    speaking: bool = Field(description="Whether they were still talking as of this response.")
    started_at: datetime
    ended_at: datetime | None = Field(
        default=None, description="Null while the turn is still open."
    )
    seconds: int = Field(description="Length of the turn, extended to now while it is open.")


class SpeakersResponse(BaseModel):
    """Who is speaking in one session's meeting, and who has."""

    session_id: str
    observed_at: datetime
    events: int = Field(
        description=(
            "Speaking edges observed. Zero means nothing has been heard yet — which is 'not "
            "known', not 'nobody has spoken'."
        )
    )
    speaking_now: tuple[str, ...] = Field(
        description=(
            "Everyone holding the floor right now, most recently started first. Plural because "
            "people talk over each other."
        )
    )
    current_speaker: str | None = Field(
        default=None,
        description="The single best answer to 'who is speaking'. Null when nobody is.",
    )
    self_name: str | None = Field(
        default=None,
        description=(
            "The name the avatar's own account appears under. Never counted as a speaker: its "
            "audio never reaches the microphone tap."
        ),
    )
    talk_time_seconds: tuple[tuple[str, int], ...] = Field(
        description="Seconds each participant has held the floor, longest first."
    )
    turns: tuple[SpeakerTurnResponse, ...] = Field(
        description="Turns in the order they began — the order the conversation happened in."
    )
    agent_context: str = Field(
        description=(
            "A prose brief suitable for dropping into the agent's context window, including "
            "what is not known."
        )
    )

    @classmethod
    def from_snapshot(cls, session_id: str, snapshot: object) -> SpeakersResponse:
        """Build the response from a ``SpeakerSnapshot``.

        Typed as ``object`` for the reason ``ParticipantsResponse.from_snapshot`` is:
        ``MeetingService`` hands this back duck-typed so it stays platform-blind.
        """
        now_us = snapshot.now_us or None  # type: ignore[attr-defined]
        return cls(
            session_id=session_id,
            observed_at=snapshot.observed_at,  # type: ignore[attr-defined]
            events=snapshot.events,  # type: ignore[attr-defined]
            speaking_now=snapshot.current,  # type: ignore[attr-defined]
            current_speaker=snapshot.current_speaker,  # type: ignore[attr-defined]
            self_name=snapshot.self_name,  # type: ignore[attr-defined]
            talk_time_seconds=snapshot.talk_time(),  # type: ignore[attr-defined]
            turns=tuple(
                SpeakerTurnResponse(
                    display_name=turn.display_name,
                    speaker=turn.label,
                    source=turn.source,
                    speaking=turn.is_open,
                    started_at=turn.started_at,
                    ended_at=turn.ended_at,
                    seconds=turn.duration_us(now_us=now_us) // 1_000_000,
                )
                for turn in snapshot.turns  # type: ignore[attr-defined]
            ),
            agent_context=snapshot.agent_context(),  # type: ignore[attr-defined]
        )


_NO_TRANSCRIPT = (
    "no transcript for session {session_id}: the session is unknown, or its connector cannot "
    "transcribe, or both MC_GOOGLE_MEET__CAPTIONS_ENABLED and MC_GOOGLE_MEET__CHAT_ENABLED "
    "are false"
)


class TranscriptLineResponse(BaseModel):
    """One thing one person said — aloud, or in the meeting's chat."""

    speaker: str = Field(description="Who said it. 'Someone' when nobody could be named.")
    text: str = Field(
        description=(
            "What they said. Approximate for a spoken line, which is Meet's own transcription; "
            "exact for a typed one."
        )
    )
    at: datetime = Field(description="UTC time the line was captured.")
    is_self: bool = Field(description="True when it was the avatar's own turn.")
    in_chat: bool = Field(
        default=False,
        description=(
            "True when this was typed into the meeting chat rather than spoken. Distinguished "
            "because somebody who only types has not been heard speaking."
        ),
    )
    inferred: bool = Field(
        default=False,
        description=(
            "True when the name came from elimination — exactly one other person was in the "
            "meeting — rather than from the line itself."
        ),
    )


class TranscriptResponse(BaseModel):
    """The meeting's conversation, attributed."""

    session_id: str
    observed_at: datetime
    self_name: str | None = Field(
        default=None, description="The name the avatar's own account appears under."
    )
    speakers: tuple[str, ...] = Field(
        description="Everyone who has said something, in the order they first did."
    )
    lines: tuple[TranscriptLineResponse, ...] = Field(
        description="Every captured line, oldest first."
    )
    agent_context: str = Field(
        description=(
            "The recent conversation as dialogue, for an agent's context window — including the "
            "caveat that Meet's captions are a transcription and the wording is approximate."
        )
    )

    @classmethod
    def from_snapshot(cls, session_id: str, snapshot: object) -> TranscriptResponse:
        """Build the response from a ``TranscriptSnapshot`` (duck-typed, as above)."""
        return cls(
            session_id=session_id,
            observed_at=snapshot.observed_at,  # type: ignore[attr-defined]
            self_name=snapshot.self_name,  # type: ignore[attr-defined]
            speakers=snapshot.speakers,  # type: ignore[attr-defined]
            lines=tuple(
                TranscriptLineResponse(
                    speaker=line.label,
                    text=line.text,
                    at=line.at,
                    is_self=line.is_self,
                    in_chat=line.in_chat,
                    inferred=line.inferred,
                )
                for line in snapshot.lines  # type: ignore[attr-defined]
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


@router.get(
    "/{session_id}/speakers",
    response_model=SpeakersResponse,
    summary="Who is speaking now, and who has spoken",
)
async def get_speakers(session_id: str, service: MeetingServiceDep) -> SpeakersResponse:
    """Current speaker and turn history. 404 when the session keeps none.

    Pulled rather than pushed for the same reason attendance is — the channel the avatar speaks
    from is not a place to put a running commentary — with one difference worth stating: who is
    speaking *is* also pushed, as silent ``meeting_context``, when
    ``MC_GOOGLE_MEET__SPEAKER_PUSH_ENABLED`` is on. That covers "the agent should know"; this
    covers "the agent, or an operator, should be able to ask", including after the fact.
    """
    snapshot = service.speaker_snapshot(SessionId(session_id))
    if snapshot is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=_NO_SPEAKERS.format(session_id=session_id),
        )
    return SpeakersResponse.from_snapshot(session_id, snapshot)


@router.get(
    "/{session_id}/transcript",
    response_model=TranscriptResponse,
    summary="What each participant said",
)
async def get_transcript(session_id: str, service: MeetingServiceDep) -> TranscriptResponse:
    """The attributed conversation. 404 when the session records none.

    Read from Meet's own captions, which is the only place in the meeting where a name and the
    words that person said appear together — the avatar's own transcription hears one mixed stream
    and cannot attribute it. The recent lines are also pushed to the agent inside the meeting
    brief; this is the whole of it, and the version an operator or a tool-calling agent can read
    after the fact.
    """
    snapshot = service.transcript_snapshot(SessionId(session_id))
    if snapshot is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=_NO_TRANSCRIPT.format(session_id=session_id),
        )
    return TranscriptResponse.from_snapshot(session_id, snapshot)


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
