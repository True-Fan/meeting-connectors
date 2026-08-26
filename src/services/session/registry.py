"""In-memory session registry.

Two indexes over one dict: by session id, which is what every HTTP route resolves, and by
meeting number, which is what a caller who has forgotten the id can still ask for.

**This used to hold a race as well**, and it is worth recording why it is gone. The
Meeting-SDK Zoom connector did not initiate its own ingest: *Zoom* did, through a
``meeting.rtms_started`` webhook that could arrive before or after the session was created.
So the registry also parked unclaimed RTMS bindings with a TTL, indexed sessions by meeting
UUID, and had a rule for which unbound session a late webhook belonged to. Every connector
now joins with a browser and opens its own ingest leg synchronously, so there is no second
initiator, no arrival order to reconcile, and nothing to park.

Not persisted. A browser session cannot resume across a process restart anyway, so a durable
session row would buy no recovery — deferred deliberately (doc 003 §0.1).
"""

from __future__ import annotations

from src.domain.ids import SessionId
from src.domain.session import SessionContext
from src.infrastructure.logging import get_logger

logger = get_logger(__name__)


class SessionRegistry:
    """Tracks live sessions."""

    __slots__ = ("_by_id", "_by_meeting_number")

    def __init__(self) -> None:
        self._by_id: dict[SessionId, SessionContext] = {}
        self._by_meeting_number: dict[str, SessionId] = {}

    def register(self, session: SessionContext) -> None:
        """Add a session. Raises if the id is already present."""
        if session.session_id in self._by_id:
            raise KeyError(f"session {session.session_id} already registered")
        self._by_id[session.session_id] = session
        if session.meeting.meeting_number:
            self._by_meeting_number[session.meeting.meeting_number] = session.session_id

    def by_id(self, session_id: SessionId) -> SessionContext | None:
        return self._by_id.get(session_id)

    def by_meeting_number(self, meeting_number: str) -> SessionContext | None:
        session_id = self._by_meeting_number.get(meeting_number)
        return self._by_id.get(session_id) if session_id else None

    def all_sessions(self) -> tuple[SessionContext, ...]:
        return tuple(self._by_id.values())

    def remove(self, session_id: SessionId) -> SessionContext | None:
        """Evict a session and every index entry pointing at it."""
        session = self._by_id.pop(session_id, None)
        if session is None:
            return None
        self._by_meeting_number = {
            k: v for k, v in self._by_meeting_number.items() if v != session_id
        }
        return session

    def __len__(self) -> int:
        return len(self._by_id)
