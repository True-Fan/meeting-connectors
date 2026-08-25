"""Teams connector configuration.

A flattened, connector-local view of ``Settings`` — the same pattern as
``connectors/zoom/config.py``, and for the same reason: the Teams feature depends on
the fields it needs rather than on the whole settings tree, so an unrelated setting
cannot change this package and a test can build one of these in a line.

The two connectors do **not** share a config base class. Their fields overlap only in
the parts that are genuinely shared infrastructure (avatar, media geometry, queue
depths), and a common base would couple two release cycles together to save a dozen
lines.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydantic import SecretStr

from src.config.settings import Settings
from src.connectors.teams.exceptions import TeamsConfigurationError
from src.domain.avatar import AVATAR_INPUT_FORMAT
from src.domain.media import AudioFormat, SampleFormat, VideoFormat

SUPPORTED_SEND_VIDEO_FORMATS: frozenset[tuple[int, int, int]] = frozenset(
    {
        (1920, 1080, 30),
        (1280, 720, 30),
        (640, 360, 30),
        (320, 180, 30),
        (1280, 720, 15),
        (640, 360, 15),
        (320, 180, 15),
    }
)
"""``(width, height, fps)`` combinations the Teams media platform accepts for an
outgoing NV12 video socket.

Unlike Zoom's external video source — which takes whatever geometry we hand it — the
Teams SDK negotiates against an enumerated list of ``VideoFormat`` constants, and a
value outside it fails at socket creation *inside the sidecar*, on a Windows host,
mid-join. Validating here turns that into a configuration error at startup.
"""

SUPPORTED_SEND_SAMPLE_RATES: frozenset[int] = frozenset({8_000, 16_000, 32_000, 44_100, 48_000})
"""PCM rates the media platform's audio socket accepts."""


@dataclass(frozen=True, slots=True)
class TeamsConnectorConfig:
    """Everything the Teams connector needs, and nothing else."""

    # Azure AD / Graph — forwarded to the sidecar, which owns the join
    tenant_id: str
    client_id: str
    client_secret: SecretStr

    # Windows media sidecar link
    sidecar_host: str
    sidecar_port: int
    sidecar_connect_timeout_s: float
    sidecar_ready_timeout_s: float
    sidecar_reconnect_max_attempts: int
    sidecar_tls_enabled: bool
    sidecar_ca_file: Path | None
    sidecar_client_cert_file: Path | None
    sidecar_client_key_file: Path | None

    # Media
    unmixed_audio: bool
    video_format: VideoFormat
    publish_audio_format: AudioFormat
    inbound_queue_size: int
    video_queue_size: int
    audio_queue_size: int
    echo_gate_hangover_ms: int
    idle_clip_path: Path | None

    # Avatar agent
    avatar_url: str
    avatar_connect_timeout_s: float
    avatar_send_queue_size: int
    avatar_reconnect_initial_delay_s: float
    avatar_reconnect_max_delay_s: float
    avatar_reconnect_max_attempts: int

    # Session defaults
    display_name: str

    def __post_init__(self) -> None:
        geometry = (self.video_format.width, self.video_format.height, self.video_format.fps)
        if geometry not in SUPPORTED_SEND_VIDEO_FORMATS:
            supported = ", ".join(
                f"{w}x{h}@{f}" for w, h, f in sorted(SUPPORTED_SEND_VIDEO_FORMATS)
            )
            raise TeamsConfigurationError(
                f"Teams cannot send {geometry[0]}x{geometry[1]}@{geometry[2]}; "
                f"supported: {supported}"
            )
        if self.publish_audio_format.sample_rate_hz not in SUPPORTED_SEND_SAMPLE_RATES:
            supported = ", ".join(str(r) for r in sorted(SUPPORTED_SEND_SAMPLE_RATES))
            raise TeamsConfigurationError(
                f"Teams cannot send {self.publish_audio_format.sample_rate_hz} Hz audio; "
                f"supported: {supported}"
            )
        if self.publish_audio_format.channels != 1:
            raise TeamsConfigurationError(
                "the Teams audio socket is mono; "
                f"got {self.publish_audio_format.channels} channels"
            )

    @property
    def ingest_audio_format(self) -> AudioFormat:
        """What the sidecar streams up to us.

        The media platform is configured for ``Pcm16K`` mono, which is *exactly*
        ``AVATAR_INPUT_FORMAT``. Zoom's zero-resample property therefore holds for
        Teams too — and for the same reason: it is a checked equality against the
        avatar's fixed contract, not a coincidence. Doc 002 §3.3 predicted a
        resampler here; the app-hosted media path removed the need for one.
        """
        return AVATAR_INPUT_FORMAT

    def is_configured(self) -> bool:
        return bool(
            self.tenant_id
            and self.client_id
            and self.client_secret.get_secret_value()
            and self.sidecar_host
        )

    def require_configured(self) -> None:
        """Fail fast when credentials are absent.

        Raises:
            TeamsConfigurationError: the connector cannot join anything.
        """
        if self.is_configured():
            return
        missing = [
            name
            for name, present in (
                ("teams.tenant_id", bool(self.tenant_id)),
                ("teams.client_id", bool(self.client_id)),
                ("teams.client_secret", bool(self.client_secret.get_secret_value())),
                ("teams.sidecar_host", bool(self.sidecar_host)),
            )
            if not present
        ]
        raise TeamsConfigurationError(f"Teams connector is not configured: missing {missing}")

    @classmethod
    def from_settings(cls, settings: Settings) -> TeamsConnectorConfig:
        return cls(
            tenant_id=settings.teams.tenant_id,
            client_id=settings.teams.client_id,
            client_secret=settings.teams.client_secret,
            sidecar_host=settings.teams.sidecar_host,
            sidecar_port=settings.teams.sidecar_port,
            sidecar_connect_timeout_s=settings.teams.sidecar_connect_timeout_s,
            sidecar_ready_timeout_s=settings.teams.sidecar_ready_timeout_s,
            sidecar_reconnect_max_attempts=settings.teams.sidecar_reconnect_max_attempts,
            sidecar_tls_enabled=settings.teams.sidecar_tls_enabled,
            sidecar_ca_file=settings.teams.sidecar_ca_file,
            sidecar_client_cert_file=settings.teams.sidecar_client_cert_file,
            sidecar_client_key_file=settings.teams.sidecar_client_key_file,
            unmixed_audio=settings.teams.unmixed_audio,
            video_format=VideoFormat(
                width=settings.teams.video_width,
                height=settings.teams.video_height,
                fps=settings.teams.video_fps,
            ),
            publish_audio_format=AudioFormat(
                sample_rate_hz=settings.teams.publish_sample_rate_hz,
                channels=1,
                sample_format=SampleFormat.S16LE,
            ),
            # Queue depths and echo timing are pipeline properties, not platform
            # ones — shared with Zoom deliberately.
            inbound_queue_size=settings.media.inbound_queue_size,
            video_queue_size=settings.media.video_queue_size,
            audio_queue_size=settings.media.audio_queue_size,
            echo_gate_hangover_ms=settings.media.echo_gate_hangover_ms,
            idle_clip_path=settings.media.idle_clip_path,
            avatar_url=settings.avatar.url,
            avatar_connect_timeout_s=settings.avatar.connect_timeout_s,
            avatar_send_queue_size=settings.avatar.send_queue_size,
            avatar_reconnect_initial_delay_s=settings.avatar.reconnect_initial_delay_s,
            avatar_reconnect_max_delay_s=settings.avatar.reconnect_max_delay_s,
            avatar_reconnect_max_attempts=settings.avatar.reconnect_max_attempts,
            display_name=settings.teams.display_name,
        )
