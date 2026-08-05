"""Session endpoints.

Platform-blind: the DTOs and this router mention meetings and sessions, never RTMS,
the Meeting SDK, or a sidecar. All of that is behind ``MeetingService``.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from src.api.dependencies import MeetingServiceDep
from src.api.dto import (
    CreateSessionRequest,
    SessionDetailResponse,
    SessionListResponse,
    SessionResponse,
)
from src.domain.ids import SessionId
from src.infrastructure.context import current_correlation_id
from src.services.meeting.service import (
    CreateSessionCommand,
    MeetingServiceError,
    SessionNotFoundError,
)

router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.post(
    "",
    response_model=SessionResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Put an avatar into a meeting",
)
async def create_session(
    request: CreateSessionRequest, service: MeetingServiceDep
) -> SessionResponse:
    """Start a session.

    Returns 202: the avatar joins and begins publishing idle media immediately, while
    audio ingest may still be waiting on Zoom's ``rtms_started`` webhook.
    """
    command = CreateSessionCommand(
        meeting_number=request.meeting_number,
        passcode=request.passcode,
        display_name=request.display_name,
        meeting_uuid=request.meeting_uuid,
        # Carry the request's correlation id into the session so one HTTP call can be
        # traced through every frame it causes.
        correlation_id=current_correlation_id(),
    )
    try:
        session = await service.create_session(command)
    except MeetingServiceError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return SessionResponse.from_domain(session)


@router.get("", response_model=SessionListResponse, summary="List sessions")
async def list_sessions(service: MeetingServiceDep) -> SessionListResponse:
    sessions = service.list_sessions()
    return SessionListResponse(
        sessions=tuple(SessionResponse.from_domain(s) for s in sessions), total=len(sessions)
    )


@router.get(
    "/{session_id}", response_model=SessionDetailResponse, summary="Session detail and health"
)
async def get_session(session_id: str, service: MeetingServiceDep) -> SessionDetailResponse:
    session = service.get_session(SessionId(session_id))
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"session {session_id} not found"
        )
    report = service.health_report(SessionId(session_id))
    return SessionDetailResponse.from_domain(session, report)


@router.delete(
    "/{session_id}",
    response_model=SessionResponse,
    summary="Remove the avatar from its meeting",
)
async def stop_session(session_id: str, service: MeetingServiceDep) -> SessionResponse:
    try:
        session = await service.stop_session(SessionId(session_id))
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except MeetingServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc
    return SessionResponse.from_domain(session)
