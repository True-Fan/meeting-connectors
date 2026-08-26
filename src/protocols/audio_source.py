"""Inbound audio port.

Implementations:

* ``connectors.zoom_web.ingest.PageAudioSource`` — Zoom meeting audio, tapped out of
  the page's playout graph
* ``connectors.teams_web.ingest.PageAudioSource`` — the same, for Teams
* ``connectors.google_meet.audio_capture.MeetAudioSource`` — Meet audio over the
  Chromium bridge
* ``tests.fakes.ReplayAudioSource`` — a recorded PCM file, so the whole pipeline
  can be exercised with no live meeting (M2)

The port yields an async iterator rather than pushing to a callback. That gives the
consumer natural backpressure: if the router stops pulling, the source's own bounded
queue fills and its drop policy engages at the point where the drop can be counted
and attributed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from src.domain.health import ComponentHealth
from src.domain.media import AudioFrame


@runtime_checkable
class AudioSource(Protocol):
    """A source of participant audio for one session."""

    async def start(self) -> None:
        """Attach to the source. Returns once audio can begin flowing."""
        ...

    async def stop(self) -> None:
        """Detach and release resources. Must be idempotent."""
        ...

    def frames(self) -> AsyncIterator[AudioFrame]:
        """Yield canonical audio frames until the source is stopped."""
        ...

    def health(self) -> ComponentHealth:
        """Current health. Called by the supervisor; must not block."""
        ...
