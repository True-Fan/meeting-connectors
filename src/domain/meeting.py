"""Meeting and participant models.

Deliberately platform-shaped-but-not-platform-typed: ``meeting_number`` and
``passcode`` are what a join needs on every platform we support, and no RTMS, Graph,
or SDK type appears here. ``platform_data`` holds the connector-private payload (Zoom:
RTMS stream id and server urls; Teams: the resolved Graph join descriptor) as opaque
data — only the owning connector may interpret it, and it validates it into a typed
model at its own boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class MeetingPlatform(StrEnum):
    """The meeting platform a session runs against.

    Added when Teams arrived: with one connector the platform was a constant folded
    into the code (doc 003 §0.1), and with two it is data. It is a *domain* enum
    rather than a connector concept precisely so that ``api/`` and ``services/`` can
    route on it without importing a connector.
    """

    ZOOM = "zoom"
    TEAMS = "teams"


@dataclass(frozen=True, slots=True)
class ParticipantRef:
    """A reference to a meeting participant.

    ``user_id`` is the platform's numeric participant identifier. It is the key
    ``EchoGuard`` uses to recognise the avatar's own audio arriving back through
    ingest (doc 003 §5.3).
    """

    user_id: int
    display_name: str | None = None


@dataclass(frozen=True, slots=True)
class MeetingContext:
    """Everything needed to attach to one meeting."""

    meeting_number: str
    display_name: str
    passcode: str | None = None
    meeting_uuid: str | None = None
    platform_data: dict[str, Any] = field(default_factory=dict)
    platform: MeetingPlatform = MeetingPlatform.ZOOM
    """Which connector owns this meeting. Defaults to ``ZOOM`` so every existing
    construction site — and every Zoom code path — behaves exactly as before."""

    def with_uuid(self, meeting_uuid: str) -> MeetingContext:
        """Return a copy carrying the meeting UUID learned from a webhook.

        The UUID is not known when an operator creates a session by meeting number;
        it arrives with ``meeting.rtms_started``. It is the key used to bind an
        inbound RTMS stream to an existing session (doc 003 §3.1).
        """
        return MeetingContext(
            meeting_number=self.meeting_number,
            display_name=self.display_name,
            passcode=self.passcode,
            meeting_uuid=meeting_uuid,
            platform_data=dict(self.platform_data),
            platform=self.platform,
        )
