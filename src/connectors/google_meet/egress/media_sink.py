"""ChromiumMediaSink — the ``MediaSink`` port over the page bridge.

The egress counterpart to ``audio_capture/audio_source.py``, and equally thin for the same
reason: ``ChromiumBridge`` owns the one browser tab that carries both directions. What this
class adds is composition — it fans a paced stream out to the two device adapters and
presents them to the shared ``Pacer`` as a single sink.

**This deliberately shares nothing with the other connectors' sinks or the removed
``TeamsMediaSink``.** The runtimes differ (Chromium via Playwright, versus a C++ Meeting SDK
sidecar, versus .NET app-hosted media on Windows), the transports differ (a loopback
WebSocket, versus a Unix socket, versus TLS across a host boundary), the credentials differ
(a browser profile cookie, versus a Zoom SDK JWT, versus an Azure AD client secret), and what
the far end wants differs (``WebCodecs.VideoFrame`` I420, versus SDK-native I420, versus
NV12). What all three genuinely have in common is the ``MediaSink`` port — which is precisely
the thing that is shared, and the whole point of the boundary.

**No participant identity is published.** A browser is never told its own participant id,
so there is nothing for ``EchoGuard`` to filter against — and nothing is needed: echo is
structurally impossible on this connector, because the WebRTC tap is inbound-only.
"""

from __future__ import annotations

from src.connectors.google_meet.bridge.chromium_bridge import ChromiumBridge
from src.connectors.google_meet.virtual_camera.adapter import VirtualCameraAdapter
from src.connectors.google_meet.virtual_microphone.adapter import VirtualMicrophoneAdapter
from src.domain.health import ComponentHealth
from src.domain.media import AudioFrame, VideoFrame
from src.domain.meeting import MeetingContext
from src.infrastructure.logging import get_logger

logger = get_logger(__name__)

COMPONENT_NAME = "google_meet_publisher"


class ChromiumMediaSink:
    """Publishes the avatar's audio and video into a Google Meet conference."""

    __slots__ = ("_bridge", "_camera", "_microphone")

    def __init__(
        self,
        *,
        bridge: ChromiumBridge,
        camera: VirtualCameraAdapter,
        microphone: VirtualMicrophoneAdapter,
    ) -> None:
        self._bridge = bridge
        self._camera = camera
        self._microphone = microphone

    async def start(self, meeting: MeetingContext) -> None:
        """Ensure the browser has joined and the page can carry media.

        ``GoogleMeetSession`` normally starts the bridge before this is reached, and
        ``ChromiumBridge.start`` is idempotent, so this is usually a no-op. It stays
        idempotent so that a sink started on its own — a verification run, or a test —
        still works.

        Raises:
            GoogleMeetError: the join failed. Propagated so session creation fails with the
                real reason rather than reporting success and publishing into nothing.
        """
        await self._bridge.start(meeting)

    async def stop(self) -> None:
        """No-op: ``GoogleMeetSession`` stops the bridge once, for both legs."""

    async def publish_audio(self, frame: AudioFrame) -> None:
        """Publish one PCM frame through the synthetic microphone."""
        await self._microphone.publish(frame)

    async def publish_video(self, frame: VideoFrame) -> None:
        """Publish one I420 frame through the synthetic camera."""
        await self._camera.publish(frame)

    def health(self) -> ComponentHealth:
        """The bridge's health, renamed for this leg, with publish counts as detail.

        The counts matter more here than on the other connectors. Every other failure this
        connector can have is visible — a crashed tab, a dropped channel, a denied join. The
        one that is not is Meet holding a perfectly good track it has not been told to
        publish, which looks healthy everywhere and is silent in the meeting. A published
        count that stays at zero while the bridge is healthy is the signal for that, so it is
        put where an operator reading ``GET /sessions/{id}`` will see it.
        """
        bridge_health = self._bridge.health()
        detail = (
            f"audio={self._microphone.published} video={self._camera.published} "
            f"dropped_audio={self._microphone.dropped} dropped_video={self._camera.dropped}"
        )
        if bridge_health.detail:
            detail = f"{bridge_health.detail}; {detail}"
        return ComponentHealth(name=COMPONENT_NAME, state=bridge_health.state, detail=detail)

    @property
    def dropped_video(self) -> int:
        return self._camera.dropped

    @property
    def dropped_audio(self) -> int:
        return self._microphone.dropped
