"""In-memory session registry, and the join/RTMS race.

**The race** (doc 003 §3.1): we initiate the bot join, but *Zoom* initiates RTMS via
the ``meeting.rtms_started`` webhook. Those two events can arrive in either order:

* **Session first** — the webhook finds the session by meeting number and binds.
* **Webhook first** — no session exists yet, so the payload is *parked* with a TTL and
  bound when the session is created.

Whichever lands second completes the binding. Handling only one order would make
attachment depend on timing we do not control.

Parked bindings expire because an RTMS stream started for a meeting we were never
asked to join is not ours, and holding it forever is a leak.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from src.domain.ids import SessionId
from src.domain.meeting import MeetingPlatform
from src.domain.session import SessionContext
from src.infrastructure.logging import get_logger

logger = get_logger(__name__)

DEFAULT_PENDING_TTL_S = 300.0


@dataclass(frozen=True, slots=True)
class PendingRtmsBinding:
    """An ``rtms_started`` payload awaiting a session to belong to."""

    meeting_uuid: str
    rtms_stream_id: str
    signaling_url: str
    received_at: float = field(default_factory=time.monotonic)

    def is_expired(self, ttl_s: float, *, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        return current - self.received_at > ttl_s


class SessionRegistry:
    """Tracks live sessions and parked RTMS bindings.

    Not persisted. RTMS cannot resume across a process restart anyway (doc 001 §10),
    so a durable session row would buy no recovery — deferred deliberately
    (doc 003 §0.1).
    """

    __slots__ = ("_by_id", "_by_meeting_number", "_by_uuid", "_pending", "_ttl_s")

    def __init__(self, *, pending_ttl_s: float = DEFAULT_PENDING_TTL_S) -> None:
        self._by_id: dict[SessionId, SessionContext] = {}
        self._by_uuid: dict[str, SessionId] = {}
        self._by_meeting_number: dict[str, SessionId] = {}
        self._pending: dict[str, PendingRtmsBinding] = {}
        self._ttl_s = pending_ttl_s

    # -- sessions ----------------------------------------------------------

    def register(self, session: SessionContext) -> None:
        """Add a session. Raises if the id is already present."""
        if session.session_id in self._by_id:
            raise KeyError(f"session {session.session_id} already registered")
        self._by_id[session.session_id] = session
        if session.meeting.meeting_number:
            self._by_meeting_number[session.meeting.meeting_number] = session.session_id
        if session.meeting.meeting_uuid:
            self._by_uuid[session.meeting.meeting_uuid] = session.session_id

    def bind_uuid(self, session_id: SessionId, meeting_uuid: str) -> None:
        """Index a session by the meeting UUID learned from a webhook."""
        self._by_uuid[meeting_uuid] = session_id

    def by_id(self, session_id: SessionId) -> SessionContext | None:
        return self._by_id.get(session_id)

    def by_meeting_uuid(self, meeting_uuid: str) -> SessionContext | None:
        session_id = self._by_uuid.get(meeting_uuid)
        return self._by_id.get(session_id) if session_id else None

    def by_meeting_number(self, meeting_number: str) -> SessionContext | None:
        session_id = self._by_meeting_number.get(meeting_number)
        return self._by_id.get(session_id) if session_id else None

    def sole_session_awaiting_rtms(self) -> SessionContext | None:
        """The one Zoom session still waiting to be bound, if there is exactly one.

        The mirror of ``take_any_pending_rtms``, for the opposite arrival order. An
        operator creates a session by **meeting number**; ``meeting.rtms_started``
        identifies the meeting only by **UUID** — it carries no meeting number at
        all — so a session created before the webhook cannot be found by
        ``by_meeting_uuid``: its UUID is still ``None``. Without this fallback the
        payload parks with nobody left to claim it, and ingest waits out its full
        timeout for a webhook that already arrived.

        **The browser connector makes this the normal case rather than a race.** It
        joins the meeting first and RTMS starts afterwards, so the session always
        exists before the webhook.

        Returns ``None`` unless exactly one candidate exists: with two unbound Zoom
        sessions the correct target is genuinely unknowable, and binding the wrong
        one is worse than waiting.
        """
        candidates = [
            session
            for session in self._by_id.values()
            if session.meeting.platform
            in (MeetingPlatform.ZOOM, MeetingPlatform.ZOOM_WEB)
            and session.meeting.meeting_uuid is None
        ]
        if len(candidates) == 1:
            return candidates[0]
        return None

    def all_sessions(self) -> tuple[SessionContext, ...]:
        return tuple(self._by_id.values())

    def remove(self, session_id: SessionId) -> SessionContext | None:
        """Evict a session and every index entry pointing at it."""
        session = self._by_id.pop(session_id, None)
        if session is None:
            return None
        self._by_uuid = {k: v for k, v in self._by_uuid.items() if v != session_id}
        self._by_meeting_number = {
            k: v for k, v in self._by_meeting_number.items() if v != session_id
        }
        return session

    def __len__(self) -> int:
        return len(self._by_id)

    # -- parked bindings ---------------------------------------------------

    def park_pending_rtms(self, binding: PendingRtmsBinding) -> None:
        """Hold an RTMS binding until its session is created."""
        self._expire_pending()
        self._pending[binding.meeting_uuid] = binding
        logger.info(
            "session.rtms_binding_parked",
            meeting_uuid=binding.meeting_uuid,
            parked=len(self._pending),
        )

    def take_pending_rtms(self, meeting_uuid: str) -> PendingRtmsBinding | None:
        """Claim a parked binding, if one is still valid."""
        self._expire_pending()
        binding = self._pending.pop(meeting_uuid, None)
        if binding is None:
            return None
        if binding.is_expired(self._ttl_s):
            return None
        return binding

    def take_any_pending_rtms(self) -> PendingRtmsBinding | None:
        """Claim any valid parked binding.

        Needed because an operator creates a session by **meeting number** while the
        webhook identifies the meeting by **UUID**, and the two cannot be correlated
        before attaching. Safe for the PoC's one-meeting-at-a-time scope; with
        concurrent meetings the operator must supply the UUID, which
        ``take_pending_rtms`` then matches exactly.
        """
        self._expire_pending()
        if not self._pending:
            return None
        oldest_uuid = min(self._pending, key=lambda k: self._pending[k].received_at)
        return self._pending.pop(oldest_uuid)

    def pending_count(self) -> int:
        self._expire_pending()
        return len(self._pending)

    def _expire_pending(self) -> None:
        expired = [uuid for uuid, b in self._pending.items() if b.is_expired(self._ttl_s)]
        for uuid in expired:
            self._pending.pop(uuid, None)
            logger.warning("session.rtms_binding_expired", meeting_uuid=uuid, ttl_s=self._ttl_s)
