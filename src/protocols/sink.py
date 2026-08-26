"""Outbound media port.

Implementations:

* ``connectors.zoom_web.egress.ZoomWebMediaSink`` — a synthetic microphone and camera
  injected into the page
* ``connectors.teams_web.egress.TeamsWebMediaSink`` — the same, for Teams
* ``connectors.google_meet.egress.ChromiumMediaSink`` — the same, over the Meet bridge
* ``services.media.sinks.FileSink`` — writes a playable file (M4)
* ``services.media.sinks.NullSink`` — counts and timestamps, discards payload (M4)

``FileSink`` is why this port exists *today*: it lets M4 prove that decoding, the
media clock, and A/V sync are correct — with watchable output — before a browser,
a profile, or a live meeting is involved.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from src.domain.health import ComponentHealth
from src.domain.media import AudioFrame, VideoFrame
from src.domain.meeting import MeetingContext, ParticipantRef


@runtime_checkable
class MediaSink(Protocol):
    """A destination for the avatar's audio and video."""

    async def start(self, meeting: MeetingContext) -> None:
        """Begin publishing. Returns once the sink can accept frames."""
        ...

    async def stop(self) -> None:
        """Stop publishing and release resources. Must be idempotent."""
        ...

    async def publish_audio(self, frame: AudioFrame) -> None:
        """Publish one audio frame. Must not block indefinitely."""
        ...

    async def publish_video(self, frame: VideoFrame) -> None:
        """Publish one video frame. Must not block indefinitely."""
        ...

    def health(self) -> ComponentHealth:
        """Current health. Called by the supervisor; must not block."""
        ...

    def own_participant(self) -> ParticipantRef | None:
        """The identity this sink publishes as, once known.

        ``EchoGuard`` needs it to recognise the avatar's own audio arriving back
        through ingest. ``None`` until the sink has joined and learned it — which
        is exactly why the echo gate exists as a second defence layer rather than
        relying on identity alone (doc 003 §3.3).
        """
        ...
