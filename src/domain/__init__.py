"""Canonical domain model.

Depends on nothing else in ``src``. Enforced by ``tests/architecture/test_layering.py``.
"""

from src.domain.avatar import (
    AVATAR_INPUT_FORMAT,
    AVATAR_OUTPUT_CONTAINER,
    AVATAR_PROTOCOL_VERSION,
    AvatarClientHello,
    AvatarProtocolVersion,
    AvatarServerHello,
    check_handshake,
)
from src.domain.context import FrameContext
from src.domain.exceptions import (
    AvatarProtocolMismatchError,
    DomainError,
    IllegalStateTransitionError,
    InvalidFrameError,
)
from src.domain.health import ComponentHealth, ComponentState, HealthReport
from src.domain.ids import CorrelationId, SessionId, new_correlation_id, new_session_id
from src.domain.media import (
    AudioFormat,
    AudioFrame,
    ContainerFormat,
    MediaChunk,
    PixelFormat,
    SampleFormat,
    VideoFormat,
    VideoFrame,
)
from src.domain.meeting import MeetingContext, ParticipantRef
from src.domain.session import (
    SessionContext,
    SessionError,
    SessionState,
    allowed_transitions,
    can_transition,
    derive_state,
)

__all__ = [
    "AVATAR_INPUT_FORMAT",
    "AVATAR_OUTPUT_CONTAINER",
    "AVATAR_PROTOCOL_VERSION",
    "AudioFormat",
    "AudioFrame",
    "AvatarClientHello",
    "AvatarProtocolMismatchError",
    "AvatarProtocolVersion",
    "AvatarServerHello",
    "ComponentHealth",
    "ComponentState",
    "ContainerFormat",
    "CorrelationId",
    "DomainError",
    "FrameContext",
    "HealthReport",
    "IllegalStateTransitionError",
    "InvalidFrameError",
    "MediaChunk",
    "MeetingContext",
    "ParticipantRef",
    "PixelFormat",
    "SampleFormat",
    "SessionContext",
    "SessionError",
    "SessionId",
    "SessionState",
    "VideoFormat",
    "VideoFrame",
    "allowed_transitions",
    "can_transition",
    "check_handshake",
    "derive_state",
    "new_correlation_id",
    "new_session_id",
]
