"""Media decoding port.

Implementations:

* ``services.media.decoders.FfmpegDecoder`` — subprocess isolation, so a malformed
  fragment cannot crash the bridge (M4)
* ``tests.fakes.FakeDecoder`` — deterministic frames, no ffmpeg binary needed

Named ``MediaDecoder`` rather than ``Mp4Decoder``: the container is a detail of the
current avatar contract, and a port should not be renamed if that contract's
encoding ever changes. A PyAV implementation is documented in doc 003 §0.1 as
deferred — the port keeps the door open without paying for a second decoder now.

``start()`` takes the init segment explicitly because a fragmented-MP4 decoder
cannot resume from a mid-stream ``moof``. Putting it in the signature makes the
replay-on-restart requirement impossible to forget.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from src.domain.health import ComponentHealth
from src.domain.media import AudioFrame, MediaChunk, VideoFrame


@runtime_checkable
class MediaDecoder(Protocol):
    """Decodes a streamed container into raw audio and video frames."""

    async def start(self, init_segment: MediaChunk | None = None) -> None:
        """Start decoding, replaying ``init_segment`` first when supplied."""
        ...

    async def stop(self) -> None:
        """Stop decoding and release resources. Must be idempotent."""
        ...

    async def feed(self, chunk: MediaChunk) -> None:
        """Push one container chunk into the decoder."""
        ...

    def video(self) -> AsyncIterator[VideoFrame]:
        """Yield decoded video frames with PTS on the session media clock."""
        ...

    def audio(self) -> AsyncIterator[AudioFrame]:
        """Yield decoded audio frames with PTS on the session media clock."""
        ...

    def health(self) -> ComponentHealth:
        """Current health. Called by the supervisor; must not block."""
        ...
