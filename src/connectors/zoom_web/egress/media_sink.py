"""The ``MediaSink`` for a browser-driven Zoom session.

Audio goes to the page's synthetic microphone — the same mechanism the Google Meet
connector publishes through, and for the same reason: it needs nothing from the host.

**Video is accepted and counted, not published.** Publishing the avatar's face needs
a canvas track and Zoom's video controls driven as well; the audio path is what makes
the avatar a participant rather than a picture. Counting the discards keeps it honest
— an operator sees ``video_dropped`` climbing rather than assuming a camera that was
never wired.
"""

from __future__ import annotations

from src.connectors.zoom_web.page.protocol import encode_audio
from src.connectors.zoom_web.page.server import PageAudioServer
from src.domain.health import ComponentHealth, ComponentState
from src.domain.media import AudioFrame, VideoFrame
from src.domain.meeting import MeetingContext, ParticipantRef
from src.infrastructure.logging import get_logger

logger = get_logger(__name__)

COMPONENT_NAME = "zoom_web_publish"


class ZoomWebMediaSink:
    """Publishes the avatar's audio through the page's synthetic microphone."""

    __slots__ = ("_server", "published", "video_dropped")

    def __init__(self, *, server: PageAudioServer) -> None:
        self._server = server
        self.published = 0
        self.video_dropped = 0

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

    async def publish_video(self, frame: VideoFrame) -> None:  # noqa: ARG002
        self.video_dropped += 1

    def health(self) -> ComponentHealth:
        """Unhealthy while no page is attached.

        That is the failure worth surfacing: the session keeps hearing the meeting
        and keeps looking fine everywhere else, while nothing it says is audible.
        A page that never connected usually means the loopback socket was blocked.
        """
        detail = (
            f"audio={self.published} video_dropped={self.video_dropped} "
            f"pages={self._server.attached_pages}"
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

    def own_participant(self) -> ParticipantRef | None:
        """Unknown, and structurally so.

        The browser does not tell us our own Zoom participant id, so ``EchoGuard``
        falls back to its speaking gate — the case that gate exists for.
        """
        return None
