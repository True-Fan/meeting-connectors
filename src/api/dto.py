"""API request and response models.

Separate from domain models on purpose: an HTTP contract and an internal model change
for different reasons, and coupling them means an internal refactor becomes a breaking
API change.

Nothing here names a platform. A caller cannot tell from this contract that the
implementation is Zoom.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from src.domain.health import ComponentState, HealthReport
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

    meeting_number: str = Field(min_length=1, max_length=64, examples=["1234567890"])
    passcode: str | None = Field(default=None, max_length=128)
    display_name: str | None = Field(
        default=None,
        max_length=128,
        description="Name other participants see. Defaults to the configured value.",
    )
    meeting_uuid: str | None = Field(
        default=None,
        description=(
            "Optional. Supplying it lets an inbound RTMS stream be matched to this "
            "session exactly rather than by arrival order."
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
    meeting_number: str
    display_name: str
    state: SessionState
    audio_attached: bool = Field(
        description="True once an RTMS stream is bound. The avatar publishes before this."
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
            meeting_number=session.meeting.meeting_number,
            display_name=session.meeting.display_name,
            state=session.state,
            audio_attached=session.meeting.meeting_uuid is not None,
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
