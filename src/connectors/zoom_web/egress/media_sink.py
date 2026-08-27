"""The ``MediaSink`` for a browser-driven Zoom session.

Audio goes to the page's synthetic microphone — the same mechanism the Google Meet
connector publishes through, and for the same reason: it needs nothing from the host.

**Video goes to the page's synthetic camera, the same way.** ``js/inject.js`` mints a
canvas-backed track behind a patched ``getUserMedia``, exactly as ``google_meet/js/bridge.js``
does for Meet; this sink's job is only to frame each ``VideoFrame`` for the wire
(``page/protocol.py``'s ``encode_video``) and hand it to the same page socket ``publish_audio``
already uses. Getting the avatar's face into the meeting also needs Zoom's own "camera on"
control clicked — see ``ZoomWebJoiner._ensure_camera_on`` in ``meeting/join.py`` — which is a
one-time UI action taken during join, not something this sink repeats per frame.
"""

from __future__ import annotations

from src.connectors.zoom_web.page.protocol import encode_audio, encode_video
from src.connectors.zoom_web.page.server import PageAudioServer
from src.domain.health import ComponentHealth, ComponentState
from src.domain.media import AudioFrame, VideoFormat, VideoFrame
from src.domain.meeting import MeetingContext
from src.infrastructure.logging import get_logger

logger = get_logger(__name__)

COMPONENT_NAME = "zoom_web_publish"


class ZoomWebMediaSink:
    """Publishes the avatar's audio and video through the page's synthetic devices."""

    __slots__ = ("_server", "_video_format", "published", "video_dropped", "video_published")

    def __init__(self, *, server: PageAudioServer, video_format: VideoFormat) -> None:
        self._server = server
        self._video_format = video_format
        self.published = 0
        self.video_dropped = 0
        self.video_published = 0

    async def start(self, meeting: MeetingContext) -> None:
        """The session owns the page's lifecycle; the socket is already bound."""

    async def stop(self) -> None:
        """No-op: the session stops the page server once, for the whole session."""

    async def publish_audio(self, frame: AudioFrame) -> None:
        if self.published == 0:
            # The counterpart of ``router.first_inbound_frame``: the one line that
            # separates "the agent never spoke" from "it spoke and the page did not
            # carry it". Without it both look identical — silence in the meeting.
            logger.info(
                "zoom_web.first_audio_published",
                attached_pages=self._server.attached_pages,
                samples=len(frame.pcm),
            )
        await self._server.send(encode_audio(frame.pcm, pts_us=frame.pts_us))
        self.published += 1

    async def publish_video(self, frame: VideoFrame) -> None:
        """Publish one I420 frame through the page's synthetic camera.

        Geometry is checked before encoding, the same guard
        ``google_meet/virtual_camera/adapter.py`` applies: the pipeline is built for one
        geometry from session start, so a mismatch means the decoder produced something the
        page's canvas was never sized for, and drawing it anyway would shear the image rather
        than fail loudly.
        """
        if (frame.format.width, frame.format.height) != (
            self._video_format.width,
            self._video_format.height,
        ):
            self.video_dropped += 1
            logger.warning(
                "zoom_web.camera_geometry_mismatch",
                got=str(frame.format),
                expected=str(self._video_format),
            )
            return

        if self.video_published == 0:
            logger.info(
                "zoom_web.first_video_published",
                attached_pages=self._server.attached_pages,
            )
        await self._server.send(encode_video(frame))
        self.video_published += 1

    def health(self) -> ComponentHealth:
        """Unhealthy while no page is attached.

        That is the failure worth surfacing: the session keeps hearing the meeting and keeps
        looking fine everywhere else, while nothing it says is audible. A page that never
        connected usually means the loopback socket was blocked.
        """
        detail = (
            f"audio={self.published} video={self.video_published} "
            f"video_dropped={self.video_dropped} pages={self._server.attached_pages}"
        )
        if not self._server.connected:
            return ComponentHealth(
                name=COMPONENT_NAME,
                state=ComponentState.UNHEALTHY,
                detail=f"the page is not attached to the audio channel; {detail}",
            )
        return ComponentHealth(
            name=COMPONENT_NAME, state=ComponentState.HEALTHY, detail=detail
        )

