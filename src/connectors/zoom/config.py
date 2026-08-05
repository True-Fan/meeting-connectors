"""Zoom connector configuration.

A flattened, connector-local view of ``Settings``. The point is that the Zoom feature
depends on the fields it needs rather than on the whole application settings tree — so
adding an unrelated setting cannot change this package, and a test can build one of
these in a line.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydantic import SecretStr

from src.config.settings import Settings
from src.domain.media import AudioFormat, VideoFormat


@dataclass(frozen=True, slots=True)
class ZoomConnectorConfig:
    """Everything the Zoom connector needs, and nothing else."""

    # RTMS ingest
    client_id: str
    client_secret: SecretStr
    rtms_send_rate_ms: int
    per_participant_audio: bool
    inbound_queue_size: int

    # Meeting SDK publish
    sdk_key: str
    sdk_secret: SecretStr
    sidecar_uds_path: Path
    sidecar_connect_timeout_s: float
    sidecar_reconnect_max_attempts: int

    # Media pipeline
    video_format: VideoFormat
    publish_audio_format: AudioFormat
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

    @classmethod
    def from_settings(cls, settings: Settings) -> ZoomConnectorConfig:
        return cls(
            client_id=settings.zoom.client_id,
            client_secret=settings.zoom.client_secret,
            rtms_send_rate_ms=settings.zoom.rtms_send_rate_ms,
            per_participant_audio=settings.zoom.rtms_per_participant_audio,
            inbound_queue_size=settings.media.inbound_queue_size,
            sdk_key=settings.zoom.sdk_key,
            sdk_secret=settings.zoom.sdk_secret,
            sidecar_uds_path=settings.sidecar.uds_path,
            sidecar_connect_timeout_s=settings.sidecar.connect_timeout_s,
            sidecar_reconnect_max_attempts=settings.sidecar.reconnect_max_attempts,
            video_format=settings.media.video_format(),
            publish_audio_format=settings.media.publish_audio_format(),
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
            display_name=settings.zoom.display_name,
        )
