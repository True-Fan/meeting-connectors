"""Outbound media port.

Implementations:

* ``connectors.zoom_web.egress.ZoomWebMediaSink`` — a synthetic microphone and camera
  injected into the page
* ``connectors.teams_web.egress.TeamsWebMediaSink`` — the same, for Teams
* ``connectors.google_meet.egress.ChromiumMediaSink`` — the same, over the Meet bridge

Three implementations, one per connector, which is what keeps this port earned. There were
once two more — a ``FileSink`` that muxed the pipeline's output into a playable file and a
``NullSink`` that counted and discarded — and they were the original justification for the
port: they let M4 prove decoding, the media clock and A/V sync were correct, with watchable
output, before any live meeting was involved. Neither was ever wired into a running path, so
both have been removed; a verification sink is worth writing again the day it is needed.
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
