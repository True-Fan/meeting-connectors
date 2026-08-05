"""Meeting and participant models.

Deliberately Zoom-shaped-but-not-Zoom-typed: ``meeting_number`` and ``passcode`` are
what a Zoom join needs, and no RTMS or SDK type appears here. ``platform_data`` holds
the connector-private payload (RTMS stream id, server urls) as opaque data — only
``connectors/zoom`` may interpret it, and it validates it into a typed model at its
own boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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
        )
