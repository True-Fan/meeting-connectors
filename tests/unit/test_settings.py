"""Configuration."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.config.settings import (
    Environment,
    MediaSettings,
    ObservabilitySettings,
    Settings,
    ZoomSettings,
)
from src.domain.avatar import AVATAR_INPUT_FORMAT


class TestDefaults:
    def test_defaults_are_usable(self, settings: Settings) -> None:
        assert settings.app_name == "meeting-connectors"
        assert settings.env is Environment.LOCAL
        assert settings.zoom.rtms_send_rate_ms == 20
        assert settings.media.video_fps == 25

    def test_unconfigured_zoom_is_reported(self, settings: Settings) -> None:
        """Startup logs this so a missing credential is obvious, not mysterious."""
        assert not settings.zoom.is_configured()
        assert not settings.zoom.is_publish_configured()


class TestEnvOverrides:
    def test_nested_env_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MC_ZOOM__CLIENT_ID", "abc")
        monkeypatch.setenv("MC_MEDIA__VIDEO_FPS", "30")
        loaded = Settings(_env_file=None)  # type: ignore[call-arg]
        assert loaded.zoom.client_id == "abc"
        assert loaded.media.video_fps == 30

    def test_credentials_are_configured_when_all_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for key, value in {
            "MC_ZOOM__CLIENT_ID": "id",
            "MC_ZOOM__CLIENT_SECRET": "secret",
            "MC_ZOOM__WEBHOOK_SECRET_TOKEN": "token",
            "MC_ZOOM__SDK_KEY": "k",
            "MC_ZOOM__SDK_SECRET": "s",
        }.items():
            monkeypatch.setenv(key, value)
        loaded = Settings(_env_file=None)  # type: ignore[call-arg]
        assert loaded.zoom.is_configured()
        assert loaded.zoom.is_publish_configured()


class TestSecretHandling:
    def test_secrets_are_masked_in_repr(self) -> None:
        """Structured logging makes an accidental repr() an easy leak."""
        zoom = ZoomSettings(client_secret="super-secret")  # type: ignore[arg-type]
        assert "super-secret" not in repr(zoom)
        assert zoom.client_secret.get_secret_value() == "super-secret"

    def test_secrets_masked_in_root_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MC_ZOOM__SDK_SECRET", "leak-me")
        loaded = Settings(_env_file=None)  # type: ignore[call-arg]
        assert "leak-me" not in str(loaded)


class TestValidation:
    @pytest.mark.parametrize("rate", [10, 19, 30, 1020])
    def test_send_rate_must_be_a_20ms_multiple_in_range(self, rate: int) -> None:
        """The protocol permits 20 ms increments up to 1000 ms."""
        with pytest.raises(ValidationError):
            ZoomSettings(rtms_send_rate_ms=rate)

    @pytest.mark.parametrize("rate", [20, 40, 100, 1000])
    def test_valid_send_rates(self, rate: int) -> None:
        assert ZoomSettings(rtms_send_rate_ms=rate).rtms_send_rate_ms == rate

    def test_log_level_is_normalised(self) -> None:
        assert ObservabilitySettings(log_level="debug").log_level == "DEBUG"

    def test_bad_log_level_rejected(self) -> None:
        with pytest.raises(ValidationError, match="log_level"):
            ObservabilitySettings(log_level="chatty")

    def test_fps_is_capped_at_thirty(self) -> None:
        """RTMS documents a 30 fps ceiling; publishing above it is meaningless."""
        with pytest.raises(ValidationError):
            MediaSettings(video_fps=60)


class TestDerivedFormats:
    def test_video_format(self) -> None:
        fmt = MediaSettings(video_width=640, video_height=480, video_fps=25).video_format()
        assert (fmt.width, fmt.height, fmt.fps) == (640, 480, 25)
        assert fmt.frame_size_bytes == 640 * 480 * 3 // 2

    def test_publish_audio_format(self) -> None:
        fmt = MediaSettings(publish_sample_rate_hz=32_000).publish_audio_format()
        assert fmt.sample_rate_hz == 32_000
        assert fmt.channels == 1

    def test_publish_rate_is_config_not_the_avatar_rate(self) -> None:
        """Ingest is 16 kHz (fixed by the avatar); publish is a separate config value
        pending confirmation against the SDK headers in M5."""
        assert MediaSettings().publish_audio_format() != AVATAR_INPUT_FORMAT


class TestEnvironment:
    def test_production_like(self) -> None:
        assert Environment.PRODUCTION.is_production_like
        assert Environment.STAGING.is_production_like
        assert not Environment.LOCAL.is_production_like
