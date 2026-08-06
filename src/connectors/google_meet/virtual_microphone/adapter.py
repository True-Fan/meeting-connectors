"""VirtualMicrophoneAdapter — the avatar's PCM into Meet's microphone.

The audio counterpart to ``virtual_camera/adapter.py``, and it rejects the OS-level route
for the same reasons plus one that is specific to audio: a PulseAudio null sink would put
the avatar's voice on the host's sound server, where anything else on the box could record
it and where a second session would need a second sink with its own name. The in-page route
keeps the audio inside the renderer that publishes it.

**Why audio always drains and video does not.** ``virtual_camera`` drops a frame when a send
is already in flight, because a lost video frame costs one frame of smoothness. Audio is
sent unconditionally and waited on, because a lost audio frame is an audible gap in speech —
the same "drop video, keep audio" policy Zoom's and Teams' publishers apply, arrived at
independently for each transport and identical every time.

The playout worklet's ring buffer is what makes that safe: if this adapter runs slightly
ahead of the browser's audio clock, the buffer absorbs it rather than the send blocking.

This is the third of the three places frames are counted, ``ChromiumBridge`` having none.
"""

from __future__ import annotations

from src.connectors.google_meet.bridge.chromium_bridge import ChromiumBridge
from src.connectors.google_meet.websocket.protocol import encode_audio
from src.domain.media import AudioFormat, AudioFrame
from src.infrastructure.logging import get_logger
from src.infrastructure.metrics import MetricName, MetricsCollector
from src.services.media.clock import MediaClock

logger = get_logger(__name__)

COMPONENT_NAME = "google_meet_microphone"


class VirtualMicrophoneAdapter:
    """Publishes PCM to the page's synthetic microphone track."""

    __slots__ = (
        "_bridge",
        "_clock",
        "_dropped",
        "_format",
        "_metrics",
        "_published",
        "_seq",
        "_warned_format",
    )

    def __init__(
        self,
        *,
        bridge: ChromiumBridge,
        audio_format: AudioFormat,
        clock: MediaClock,
        metrics: MetricsCollector | None = None,
    ) -> None:
        self._bridge = bridge
        self._format = audio_format
        self._clock = clock
        self._metrics = metrics
        self._seq = 0
        self._published = 0
        self._dropped = 0
        self._warned_format = False

    @property
    def audio_format(self) -> AudioFormat:
        return self._format

    @property
    def published(self) -> int:
        return self._published

    @property
    def dropped(self) -> int:
        return self._dropped

    async def publish(self, frame: AudioFrame) -> None:
        """Send one PCM frame to the page. Always waits for the write.

        Absorbs a channel failure rather than raising, for the same reason the camera
        adapter does: the Pacer's task group must survive a rejoin.

        A format mismatch is warned about **once** and then sent anyway. That is a
        deliberate difference from the camera's hard drop. The wire header carries the rate
        and channel count, so the playout worklet receives audio it can still render — at
        the wrong pitch, which is audible and diagnosable. Dropping it instead would produce
        total silence, which is neither. And warning once rather than per frame keeps a
        misconfiguration from emitting fifty log lines a second.
        """
        if frame.format != self._format and not self._warned_format:
            self._warned_format = True
            logger.warning(
                "meet_microphone.format_mismatch",
                got=str(frame.format),
                expected=str(self._format),
                note="the avatar will be published at the wrong pitch; check "
                "MC_GOOGLE_MEET__PUBLISH_SAMPLE_RATE_HZ against the decoder's output",
            )

        started = self._clock.now_us()
        payload = encode_audio(frame, seq=self._seq)
        self._seq = (self._seq + 1) % (2**32)

        if not await self._bridge.send_audio(payload):
            self._dropped += 1
            if self._metrics is not None:
                self._metrics.increment(
                    MetricName.FRAMES_DROPPED_TOTAL,
                    ctx=frame.ctx,
                    stage=COMPONENT_NAME,
                    reason="link_down",
                )
            return

        self._published += 1
        if self._metrics is not None:
            self._metrics.increment(
                MetricName.FRAMES_PUBLISHED_TOTAL, ctx=frame.ctx, kind="audio"
            )
            self._metrics.observe(
                MetricName.PUBLISH_US, self._clock.now_us() - started, ctx=frame.ctx, kind="audio"
            )
