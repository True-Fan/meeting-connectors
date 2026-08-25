"""VirtualCameraAdapter — decoded video frames into Meet's camera.

**Why there is no OS-level virtual camera here.** The obvious way to give Chromium a camera
is a kernel loopback device: ``v4l2loopback`` on Linux, feeding it I420 and letting Chromium
enumerate it as ``/dev/video0``. It was rejected, and the reasons compound:

* It needs a kernel module, so it needs a privileged container and a host whose kernel
  headers match. That makes the connector undeployable on most managed runtimes, and
  untestable in CI.
* It is Linux-only, where everything else in this connector runs anywhere Chromium does.
* Its failure mode is silent. A loopback device that stops being written to keeps
  presenting as a healthy camera and Chromium happily publishes its last frame forever, so
  the avatar freezes and every health check stays green.
* It is a second process boundary carrying 78 MB/s, on top of the one we already have.

The in-page route avoids all four. ``js/bridge.js`` intercepts ``getUserMedia`` and hands
Meet a track backed by a canvas, so the "device" exists only inside the renderer that
consumes it. Nothing to install, nothing to privilege, and the same code path on every
platform.

**What this adapter is, then.** The Python half: it encodes an ``I420`` ``VideoFrame`` into
one wire message and hands it to the bridge. It is thin on purpose — the interesting work is
``WebCodecs.VideoFrame`` construction on the far side, which happens where the pixels are
already going.

It is also where **published video frames are counted**, because ``ChromiumBridge`` records
no metrics and this is one of the three points where frames cross a boundary.
"""

from __future__ import annotations

from src.connectors.google_meet.bridge.chromium_bridge import ChromiumBridge
from src.connectors.google_meet.websocket.protocol import encode_video
from src.domain.media import VideoFormat, VideoFrame
from src.infrastructure.logging import get_logger
from src.infrastructure.metrics import MetricName, MetricsCollector
from src.services.media.clock import MediaClock

logger = get_logger(__name__)

COMPONENT_NAME = "google_meet_camera"


class VirtualCameraAdapter:
    """Publishes I420 frames to the page's synthetic camera track."""

    __slots__ = ("_bridge", "_clock", "_dropped", "_format", "_metrics", "_published", "_seq")

    def __init__(
        self,
        *,
        bridge: ChromiumBridge,
        video_format: VideoFormat,
        clock: MediaClock,
        metrics: MetricsCollector | None = None,
    ) -> None:
        self._bridge = bridge
        self._format = video_format
        self._clock = clock
        self._metrics = metrics
        self._seq = 0
        self._published = 0
        self._dropped = 0

    @property
    def video_format(self) -> VideoFormat:
        return self._format

    @property
    def published(self) -> int:
        return self._published

    @property
    def dropped(self) -> int:
        return self._dropped

    async def publish(self, frame: VideoFrame) -> None:
        """Send one frame to the page.

        Absorbs a channel failure rather than raising, and that is a requirement rather
        than a convenience: the Pacer publishes on a continuous cadence and is the same
        shared component all three connectors use. An exception escaping here would tear
        down its task group — and with it the audio loop — at the exact moment the bridge
        is a few seconds from relaunching a browser. A dropped frame is counted and the leg
        is marked degraded, which is what the supervisor acts on.

        Geometry is checked before encoding. The pipeline is built for one geometry from
        session start, so a mismatch means the decoder produced something unexpected; the
        page would build a ``VideoFrame`` whose layout disagrees with its canvas and render
        a sheared image, which is a slow fault to identify from outside a headless browser.
        """
        if (frame.format.width, frame.format.height) != (self._format.width, self._format.height):
            self._count_drop(frame, reason="geometry_mismatch")
            logger.warning(
                "meet_camera.geometry_mismatch",
                got=str(frame.format),
                expected=str(self._format),
            )
            return

        started = self._clock.now_us()
        payload = encode_video(frame, seq=self._seq)
        self._seq = (self._seq + 1) % (2**32)

        if not await self._bridge.send_video(payload):
            self._count_drop(frame, reason="backpressure_or_link_down")
            return

        self._published += 1
        if self._metrics is not None:
            self._metrics.increment(
                MetricName.FRAMES_PUBLISHED_TOTAL, ctx=frame.ctx, kind="video"
            )
            self._metrics.observe(
                MetricName.PUBLISH_US, self._clock.now_us() - started, ctx=frame.ctx, kind="video"
            )

    def _count_drop(self, frame: VideoFrame, *, reason: str) -> None:
        self._dropped += 1
        if self._metrics is not None:
            self._metrics.increment(
                MetricName.FRAMES_DROPPED_TOTAL,
                ctx=frame.ctx,
                stage=COMPONENT_NAME,
                reason=reason,
            )
