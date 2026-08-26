"""API request and response models.

Separate from domain models on purpose: an HTTP contract and an internal model change
for different reasons, and coupling them means an internal refactor becomes a breaking
API change.

Nothing here names a *specific* platform's mechanics. ``platform`` is a domain enum, so
the contract says which connector to use without exposing RTMS, Graph, or a sidecar —
a caller still cannot tell from this contract how either platform is implemented.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from src.domain.health import ComponentState, HealthReport
from src.domain.meeting import MeetingPlatform
from src.domain.session import SessionContext, SessionState


class ComponentHealthResponse(BaseModel):
    """Health of one component."""

    model_config = ConfigDict(frozen=True)

    name: str
    state: ComponentState
    detail: str | None = None


class HealthResponse(BaseModel):
    """Service health."""

    model_config = ConfigDict(frozen=True)

    status: ComponentState
    app: str
    version: str
    env: str
    uptime_seconds: float = Field(ge=0)
    active_sessions: int = 0
    components: tuple[ComponentHealthResponse, ...] = ()


class CreateSessionRequest(BaseModel):
    """Request to put an avatar into a meeting."""

    model_config = ConfigDict(frozen=True)

    meeting_number: str = Field(min_length=1, max_length=512, examples=["1234567890"])
    """The meeting's identifier. On Zoom, the meeting number; on Teams, the "Meeting ID"
    printed in the invite; on Google Meet, the meeting code. A join URL is also accepted
    here, which is why the length allows for one."""
    passcode: str | None = Field(default=None, max_length=128)
    display_name: str | None = Field(
        default=None,
        max_length=128,
        description="Name other participants see. Defaults to the configured value.",
    )
    platform: MeetingPlatform = Field(
        default=MeetingPlatform.ZOOM_WEB,
        description=(
            "Which connector serves this session. Defaults to zoom_web, so a Zoom "
            "join with no platform stated still joins Zoom."
        ),
    )
    meeting_url: str | None = Field(
        default=None,
        max_length=2048,
        description=(
            "Optional platform join URL, for where a numeric meeting id is not to "
            "hand. Used by teams_web; zoom_web and google_meet join by number and code."
        ),
    )


class SessionErrorResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    component: str
    message: str
    at: datetime
    fatal: bool


class SessionResponse(BaseModel):
    """A session's summary state."""

    model_config = ConfigDict(frozen=True)

    session_id: str
    correlation_id: str
    platform: MeetingPlatform
    meeting_number: str
    display_name: str
    state: SessionState
    audio_attached: bool = Field(
        description=(
            "True once audio ingest is bound. On Zoom this waits for the RTMS stream, "
            "and the avatar publishes before it; on Teams one join covers both "
            "directions, so it is true from the moment the session is active."
        )
    )
    created_at: datetime
    started_at: datetime | None = None
    ended_at: datetime | None = None
    last_heartbeat_at: datetime | None = None

    @classmethod
    def from_domain(cls, session: SessionContext) -> SessionResponse:
        return cls(
            session_id=session.session_id,
            correlation_id=session.correlation_id,
            platform=session.meeting.platform,
            meeting_number=session.meeting.meeting_number,
            display_name=session.meeting.display_name,
            state=session.state,
            audio_attached=_audio_attached(session),
            created_at=session.created_at,
            started_at=session.started_at,
            ended_at=session.ended_at,
            last_heartbeat_at=session.last_heartbeat_at,
        )


class SessionDetailResponse(SessionResponse):
    """A session plus component health and its error history."""

    model_config = ConfigDict(frozen=True)

    components: tuple[ComponentHealthResponse, ...] = ()
    errors: tuple[SessionErrorResponse, ...] = ()

    @classmethod
    def from_domain(  # type: ignore[override]
        cls, session: SessionContext, report: object | None = None
    ) -> SessionDetailResponse:
        components: tuple[ComponentHealthResponse, ...] = ()
        if isinstance(report, HealthReport):
            components = tuple(
                ComponentHealthResponse(name=c.name, state=c.state, detail=c.detail)
                for c in report.components
            )
        return cls(
            **SessionResponse.from_domain(session).model_dump(),
            components=components,
            errors=tuple(
                SessionErrorResponse(
                    component=e.component, message=e.message, at=e.at, fatal=e.fatal
                )
                for e in session.errors
            ),
        )


class SessionListResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    sessions: tuple[SessionResponse, ...] = ()
    total: int = 0


def _audio_attached(session: SessionContext) -> bool:
    """Whether audio ingest is bound.

    **One answer for every platform now, and it used to be two.** The removed Meeting-SDK
    Zoom connector attached ingest only once a ``meeting.rtms_started`` webhook supplied a
    meeting UUID, so for that connector the UUID's presence *was* the answer. Every
    remaining connector opens ingest inside its own join, which covers both directions — so
    a running session is by definition attached.
    """
    return session.state.is_running
