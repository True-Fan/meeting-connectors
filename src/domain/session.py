"""Session state and context.

The state machine from doc 003 §6. State is *derived* from component health by
``SessionLifecycle`` (M2) rather than assigned ad hoc, so there is exactly one place
where "what does healthy mean" is decided. This module owns the states, the legal
transitions, and the derivation rule; the service layer drives them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType

from src.domain.context import FrameContext
from src.domain.health import ComponentState
from src.domain.ids import CorrelationId, SessionId
from src.domain.meeting import MeetingContext


class SessionState(StrEnum):
    """Lifecycle states of a meeting session."""

    CREATED = "created"
    JOINING = "joining"
    ACTIVE = "active"
    DEGRADED = "degraded"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in (SessionState.STOPPED, SessionState.FAILED)

    @property
    def is_running(self) -> bool:
        """True when media may be flowing in at least one direction."""
        return self in (SessionState.ACTIVE, SessionState.DEGRADED)


_TRANSITIONS: Mapping[SessionState, frozenset[SessionState]] = MappingProxyType(
    {
        SessionState.CREATED: frozenset(
            {SessionState.JOINING, SessionState.STOPPING, SessionState.FAILED}
        ),
        SessionState.JOINING: frozenset(
            {SessionState.ACTIVE, SessionState.STOPPING, SessionState.FAILED}
        ),
        SessionState.ACTIVE: frozenset(
            {SessionState.DEGRADED, SessionState.STOPPING, SessionState.FAILED}
        ),
        SessionState.DEGRADED: frozenset(
            {SessionState.ACTIVE, SessionState.STOPPING, SessionState.FAILED}
        ),
        SessionState.STOPPING: frozenset({SessionState.STOPPED, SessionState.FAILED}),
        SessionState.STOPPED: frozenset(),
        SessionState.FAILED: frozenset(),
    }
)


def can_transition(current: SessionState, requested: SessionState) -> bool:
    """True when ``current -> requested`` is a legal move."""
    return requested in _TRANSITIONS[current]


def allowed_transitions(current: SessionState) -> frozenset[SessionState]:
    """Legal next states from ``current``."""
    return _TRANSITIONS[current]


def derive_state(ingest: ComponentState, publish: ComponentState) -> SessionState:
    """Derive the running state of a session from its two legs' health.

    The Zoom bridge has exactly two independently-recovering legs: RTMS ingest and
    the Meeting SDK publisher (doc 003 §6.3).

    * both healthy                → ``ACTIVE``
    * exactly one unhealthy       → ``DEGRADED`` (recovery in progress)
    * both unhealthy              → ``DEGRADED`` — still not terminal; the
      supervisor decides ``FAILED`` when retries are exhausted, because health
      alone cannot distinguish a blip from an unrecoverable failure.
    * either still starting       → ``JOINING``
    """
    if ComponentState.UNKNOWN in (ingest, publish):
        return SessionState.JOINING
    if ingest is ComponentState.HEALTHY and publish is ComponentState.HEALTHY:
        return SessionState.ACTIVE
    return SessionState.DEGRADED


@dataclass(frozen=True, slots=True)
class SessionError:
    """A recorded failure. Sessions may fail more than once; the sequence matters."""

    component: str
    message: str
    at: datetime
    fatal: bool = False


@dataclass(slots=True)
class SessionContext:
    """Mutable per-session state.

    The one mutable domain object: a session's state, timestamps, and error history
    change over its lifetime by definition. Frames it produces are immutable and
    share its ``frame_context()``.
    """

    session_id: SessionId
    correlation_id: CorrelationId
    meeting: MeetingContext
    state: SessionState = SessionState.CREATED
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    ended_at: datetime | None = None
    last_heartbeat_at: datetime | None = None
    errors: list[SessionError] = field(default_factory=list)

    def frame_context(self) -> FrameContext:
        """The identity every frame from this session carries."""
        return FrameContext(session_id=self.session_id, correlation_id=self.correlation_id)

    def log_fields(self) -> dict[str, str]:
        """Structured-log fields identifying this session."""
        return {
            "session_id": self.session_id,
            "correlation_id": self.correlation_id,
            "meeting_number": self.meeting.meeting_number,
            "session_state": self.state,
        }

    def record_error(self, component: str, message: str, *, fatal: bool = False) -> SessionError:
        """Append a failure to this session's history and return it."""
        error = SessionError(
            component=component, message=message, at=datetime.now(UTC), fatal=fatal
        )
        self.errors.append(error)
        return error
