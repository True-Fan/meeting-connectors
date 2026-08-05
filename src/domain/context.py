"""Per-session identity carried by every media frame.

Requirement: every log, metric, and frame carries the session and correlation id.

Rather than duplicating two fields across ``AudioFrame``, ``VideoFrame`` and
``MediaChunk``, each frame holds a single reference to one immutable
``FrameContext`` created once per session. Two consequences:

* the ids cannot drift apart — there is one object, not six fields;
* the per-frame cost is one pointer, not two strings, which matters at
  50 audio frames/s plus 25 video frames/s per session.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.domain.ids import CorrelationId, SessionId


@dataclass(frozen=True, slots=True)
class FrameContext:
    """Immutable identity shared by every frame belonging to one session."""

    session_id: SessionId
    correlation_id: CorrelationId

    def as_log_fields(self) -> dict[str, str]:
        """Return the context as structured-log fields."""
        return {"session_id": self.session_id, "correlation_id": self.correlation_id}
