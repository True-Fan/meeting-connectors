"""Configuration.

Config-driven with no globals: ``Settings`` is constructed once and supplied through
the DI container. Nothing reads ``os.environ`` directly.

Secrets are ``SecretStr`` so they cannot be leaked by an accidental ``repr()`` in a
log line — with structured logging that would otherwise be an easy mistake to make.

Environment variables use the ``MC_`` prefix and ``__`` as the nesting delimiter,
e.g. ``MC_ZOOM__CLIENT_ID``.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.domain.media import AudioFormat, SampleFormat, VideoFormat


class Environment(StrEnum):
    LOCAL = "local"
    DEV = "dev"
    STAGING = "staging"
    PRODUCTION = "production"

    @property
    def is_production_like(self) -> bool:
        return self in (Environment.STAGING, Environment.PRODUCTION)


class ObservabilitySettings(BaseModel):
    """Logging and metrics configuration."""

    log_level: str = "INFO"
    json_logs: bool = False
    histogram_capacity: int = Field(default=4096, ge=64, le=1_048_576)
    """Ring-buffer size per latency histogram. Bounds memory and fixes the
    percentile window; see ``infrastructure.metrics``."""

    @field_validator("log_level")
    @classmethod
    def _upper(cls, value: str) -> str:
        level = value.upper()
        allowed = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
        if level not in allowed:
            raise ValueError(f"log_level must be one of {sorted(allowed)}, got {value!r}")
        return level


class ZoomSettings(BaseModel):
    """Zoom credentials and RTMS subscription parameters.

    Two independent credential paths (doc 003 §1.1):

    * ``client_id`` / ``client_secret`` / ``webhook_secret_token`` — RTMS webhook
      verification and handshake signature.
    * ``sdk_key`` / ``sdk_secret`` — Meeting SDK JWT for the publishing bot.
    """

    client_id: str = ""
    client_secret: SecretStr = SecretStr("")
    webhook_secret_token: SecretStr = SecretStr("")

    sdk_key: str = ""
    sdk_secret: SecretStr = SecretStr("")

    rtms_send_rate_ms: int = Field(default=20, ge=20, le=1000, multiple_of=20)
    """RTMS audio delivery interval. 20 ms is the protocol floor; the samples
    default to 100, which donates 80 ms of latency at the first hop (doc 003 §3.2)."""

    rtms_per_participant_audio: bool = True
    """Subscribe ``AUDIO_MULTI_STREAMS`` so the avatar's own audio can be filtered
    by participant. Disabling it forces ``EchoGuard`` into strict gating."""

    display_name: str = "AI Avatar"
    """The name other participants see. The avatar should read as a person."""

    def is_configured(self) -> bool:
        """True when RTMS ingest credentials are present."""
        return bool(
            self.client_id
            and self.client_secret.get_secret_value()
            and self.webhook_secret_token.get_secret_value()
        )

    def is_publish_configured(self) -> bool:
        """True when Meeting SDK publish credentials are present."""
        return bool(self.sdk_key and self.sdk_secret.get_secret_value())


class AvatarSettings(BaseModel):
    """Streaming Avatar Agent connection settings."""

    url: str = "ws://localhost:8100/stream"
    connect_timeout_s: float = Field(default=10.0, gt=0)
    reconnect_initial_delay_s: float = Field(default=0.5, gt=0)
    reconnect_max_delay_s: float = Field(default=15.0, gt=0)
    reconnect_max_attempts: int = Field(default=10, ge=1)
    send_queue_size: int = Field(default=25, ge=1)
    """Bounded outbound queue, ~500 ms at 20 ms frames. Overflow drops oldest and
    counts it — it must never block the ingest reader (doc 003 §7.2)."""


class MediaSettings(BaseModel):
    """Media pipeline geometry and queue depths."""

    video_width: int = Field(default=1280, ge=2)
    video_height: int = Field(default=720, ge=2)
    video_fps: int = Field(default=25, ge=1, le=30)

    publish_sample_rate_hz: int = Field(default=32_000, ge=8_000)
    """Sample rate fed to the Meeting SDK virtual microphone. A config value, not a
    constant, until confirmed against the SDK headers in M5 (doc 003 §9 Q4)."""
    publish_channels: int = Field(default=1, ge=1, le=2)

    inbound_queue_size: int = Field(default=50, ge=1)
    video_queue_size: int = Field(default=3, ge=1)
    audio_queue_size: int = Field(default=10, ge=1)

    echo_gate_hangover_ms: int = Field(default=200, ge=0)
    """How long after the avatar stops publishing the echo gate stays shut."""

    idle_clip_path: Path | None = None
    """Optional packed raw-I420 loop shown while the avatar is silent. When unset the
    last real frame is held, falling back to a neutral field (doc 003 §1.4)."""

    def video_format(self) -> VideoFormat:
        return VideoFormat(width=self.video_width, height=self.video_height, fps=self.video_fps)

    def publish_audio_format(self) -> AudioFormat:
        return AudioFormat(
            sample_rate_hz=self.publish_sample_rate_hz,
            channels=self.publish_channels,
            sample_format=SampleFormat.S16LE,
        )


class SidecarSettings(BaseModel):
    """C++ publisher sidecar IPC settings (M5)."""

    uds_path: Path = Path("/run/meeting-connectors/sidecar.sock")
    connect_timeout_s: float = Field(default=15.0, gt=0)
    heartbeat_interval_s: float = Field(default=2.0, gt=0)
    heartbeat_timeout_s: float = Field(default=6.0, gt=0)
    reconnect_max_attempts: int = Field(default=10, ge=1)


class Settings(BaseSettings):
    """Root application settings."""

    model_config = SettingsConfigDict(
        env_prefix="MC_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "meeting-connectors"
    env: Environment = Environment.LOCAL

    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
    zoom: ZoomSettings = Field(default_factory=ZoomSettings)
    avatar: AvatarSettings = Field(default_factory=AvatarSettings)
    media: MediaSettings = Field(default_factory=MediaSettings)
    sidecar: SidecarSettings = Field(default_factory=SidecarSettings)
