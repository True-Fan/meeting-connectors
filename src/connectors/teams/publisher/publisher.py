"""TeamsMediaSink — the ``MediaSink`` port over the media sidecar link.

The egress counterpart to ``ingest/audio_source.py``, and equally thin for the same
reason: ``TeamsSidecarLink`` owns the one media session that carries both directions.

**This deliberately shares nothing with Zoom's ``MeetingPublisher``.** The underlying
SDKs are different (``Microsoft.Graph.Communications.Calls.Media`` on Windows/.NET
versus the Zoom Meeting SDK on Linux/C++), the transports are different (TLS across a
host boundary versus a Unix socket), the credentials are different (an Azure AD client
secret versus a Zoom SDK JWT), and the pixel format the platform wants is different
(NV12 versus I420). What the two genuinely have in common is the ``MediaSink`` port —
which is precisely the thing that is shared.
"""

from __future__ import annotations

from src.connectors.teams.sidecar.link import TeamsSidecarLink
from src.domain.health import ComponentHealth
from src.domain.media import AudioFrame, VideoFrame
from src.domain.meeting import MeetingContext, ParticipantRef
from src.infrastructure.logging import get_logger

logger = get_logger(__name__)

COMPONENT_NAME = "teams_publisher"


class TeamsMediaSink:
    """Publishes the avatar's audio and video into a Teams meeting."""

    __slots__ = ("_link",)

    def __init__(self, *, link: TeamsSidecarLink) -> None:
        self._link = link

    async def start(self, meeting: MeetingContext) -> None:
        """Ensure the link is joined and ready to carry media.

        ``TeamsMeetingSession`` normally starts the link before this is reached, so
        this is usually a no-op; it stays idempotent so that a sink started on its own
        — a ``FileSink``-style verification run, or a test — still works.

        Raises:
            TeamsSidecarError: the join failed. Propagated so session creation fails
                with the real reason rather than reporting success and publishing
                nothing.
        """
        await self._link.start(meeting)

    async def stop(self) -> None:
        """No-op: ``TeamsMeetingSession`` stops the link once, for both legs."""

    async def publish_audio(self, frame: AudioFrame) -> None:
        """Publish one PCM frame."""
        await self._link.publish_audio(frame)

    async def publish_video(self, frame: VideoFrame) -> None:
        """Publish one I420 frame; the sidecar converts it to NV12."""
        await self._link.publish_video(frame)

    def health(self) -> ComponentHealth:
        """The link's health, renamed for this leg."""
        link_health = self._link.health()
        return ComponentHealth(
            name=COMPONENT_NAME, state=link_health.state, detail=link_health.detail
        )

    def own_participant(self) -> ParticipantRef | None:
        """The identity the bot publishes as, once the roster reports it.

        ``None`` until the Graph roster arrives, which is *after* the call is
        established — where Zoom learns its participant id during the join handshake.
        ``EchoGuard``'s speaking gate covers that window, and
        ``TeamsSidecarLink.add_participant_listener`` closes the loop the moment the
        identity is known (doc 005 §4.3).
        """
        return self._link.own_participant()

    @property
    def dropped_video(self) -> int:
        return self._link.stats["dropped_video"]
