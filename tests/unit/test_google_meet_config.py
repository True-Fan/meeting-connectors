"""Google Meet connector configuration.

Two classes of assertion, and the second matters more:

* validation catches at startup the mistakes that would otherwise fail mid-join, or worse,
  degrade silently;
* **backward compatibility** — a deployment that has never heard of Google Meet must behave
  exactly as it did before. Zoom is in production.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import SecretStr

from src.config.settings import GoogleMeetSettings, Settings
from src.connectors.google_meet.config import (
    MAX_PUBLISH_PIXELS,
    SUPPORTED_PUBLISH_SAMPLE_RATES,
    GoogleMeetConnectorConfig,
)
from src.connectors.google_meet.exceptions import MeetConfigurationError
from src.domain.avatar import AVATAR_INPUT_FORMAT


def _settings(**meet: object) -> Settings:
    defaults: dict[str, object] = {"profile_dir": Path("/tmp/meet-profile")}
    defaults.update(meet)
    return Settings(  # type: ignore[call-arg]
        _env_file=None, google_meet=GoogleMeetSettings(**defaults)  # type: ignore[arg-type]
    )


class TestBackwardCompatibility:
    """A deployment written before this connector existed must be untouched."""

    def test_defaults_leave_the_connector_unconfigured(self) -> None:
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        assert settings.google_meet.is_configured() is False

    def test_zoom_and_teams_settings_are_unchanged_by_the_new_section(self) -> None:
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        assert settings.zoom.display_name == "AI Avatar"
        assert settings.zoom.rtms_send_rate_ms == 20
        assert settings.zoom.is_configured() is False
        assert settings.teams.sidecar_port == 8445
        assert settings.teams.publish_sample_rate_hz == 16_000
        assert settings.teams.is_configured() is False

    def test_the_shared_media_defaults_are_unchanged(self) -> None:
        """Meet takes its own publish rate rather than moving the shared one."""
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        assert settings.media.publish_sample_rate_hz == 32_000
        assert settings.media.video_fps == 25

    def test_no_profile_dir_means_not_configured(self) -> None:
        assert GoogleMeetSettings().is_configured() is False
        assert GoogleMeetSettings(profile_dir=Path("/tmp/p")).is_configured() is True


class TestDefaults:
    def test_defaults_are_the_documented_ones(self) -> None:
        config = GoogleMeetConnectorConfig.from_settings(_settings())

        assert config.headless is True
        assert config.video_format.width == 1280
        assert config.video_format.height == 720
        assert config.video_format.fps == 25
        # 48 kHz is Web Audio's native rate on desktop Chromium, so the synthetic
        # microphone needs no resampling stage.
        assert config.publish_audio_format.sample_rate_hz == 48_000
        assert config.publish_audio_format.channels == 1
        # Ephemeral by default, so two concurrent sessions cannot collide on a port.
        assert config.bridge_port == 0
        assert config.bridge_host == "127.0.0.1"
        # Lower than the other connectors': a rejoin here relaunches a whole browser.
        assert config.rejoin_max_attempts == 5

    def test_the_lobby_budget_is_far_longer_than_the_join_budget(self) -> None:
        """A human host has to notice and click Admit; that is not a join failure."""
        config = GoogleMeetConnectorConfig.from_settings(_settings())
        assert config.lobby_timeout_s > config.join_timeout_s

    def test_ingest_format_is_the_avatar_contract_exactly(self) -> None:
        """No resampler exists anywhere; the capture AudioContext is built at 16 kHz."""
        config = GoogleMeetConnectorConfig.from_settings(_settings())
        assert config.ingest_audio_format == AVATAR_INPUT_FORMAT
        assert config.ingest_audio_format.sample_rate_hz == 16_000
        assert config.ingest_audio_format.channels == 1

    def test_pipeline_settings_come_from_the_shared_media_section(self) -> None:
        """Queue depths and echo timing are pipeline properties, not platform ones."""
        settings = _settings()
        config = GoogleMeetConnectorConfig.from_settings(settings)
        assert config.inbound_queue_size == settings.media.inbound_queue_size
        assert config.video_queue_size == settings.media.video_queue_size
        assert config.echo_gate_hangover_ms == settings.media.echo_gate_hangover_ms


class TestValidation:
    def test_video_above_1080p_is_refused(self) -> None:
        """Every frame crosses the page bridge as raw I420, and Meet downscales anyway."""
        with pytest.raises(MeetConfigurationError, match="pixel ceiling"):
            GoogleMeetConnectorConfig.from_settings(
                _settings(video_width=3840, video_height=2160)
            )

    def test_1080p_is_allowed(self) -> None:
        config = GoogleMeetConnectorConfig.from_settings(
            _settings(video_width=1920, video_height=1080, video_fps=25)
        )
        assert config.video_format.width * config.video_format.height <= MAX_PUBLISH_PIXELS

    def test_odd_dimensions_are_refused_by_the_domain_model(self) -> None:
        """I420 subsamples chroma 2x2, so odd dimensions have no valid plane layout."""
        from src.domain.exceptions import InvalidFrameError

        with pytest.raises(InvalidFrameError, match="even dimensions"):
            GoogleMeetConnectorConfig.from_settings(
                _settings(video_width=1281, video_height=721)
            )

    def test_an_unsupported_sample_rate_is_refused_rather_than_resampled(self) -> None:
        """Web Audio would resample silently, producing a pitch artefact nobody can trace."""
        with pytest.raises(MeetConfigurationError, match="cannot publish 44100 Hz"):
            GoogleMeetConnectorConfig.from_settings(_settings(publish_sample_rate_hz=44_100))

    @pytest.mark.parametrize("rate", sorted(SUPPORTED_PUBLISH_SAMPLE_RATES))
    def test_every_supported_rate_divides_into_48k(self, rate: int) -> None:
        """Which keeps the browser's implicit resample a clean integer one."""
        assert 48_000 % rate == 0
        config = GoogleMeetConnectorConfig.from_settings(
            _settings(publish_sample_rate_hz=rate)
        )
        assert config.publish_audio_format.sample_rate_hz == rate


class TestRequireConfigured:
    def test_it_returns_the_narrowed_path(self) -> None:
        """Callers get a checked value out of the same call that validates it."""
        config = GoogleMeetConnectorConfig.from_settings(_settings())
        assert config.require_configured() == Path("/tmp/meet-profile")

    def test_an_unconfigured_connector_reports_it_rather_than_inventing_a_path(self) -> None:
        """A substituted default would make ``is_configured()`` unable to return False."""
        config = GoogleMeetConnectorConfig.from_settings(
            Settings(_env_file=None, google_meet=GoogleMeetSettings())  # type: ignore[call-arg]
        )
        assert config.profile_dir is None
        assert config.is_configured() is False

    def test_the_error_names_the_environment_variable_to_set(self) -> None:
        config = replace(
            GoogleMeetConnectorConfig.from_settings(_settings()), profile_dir=None
        )
        with pytest.raises(MeetConfigurationError, match="MC_GOOGLE_MEET__PROFILE_DIR"):
            config.require_configured()


class TestSecrets:
    def test_the_google_password_is_a_secret(self) -> None:
        """Structured logging makes an accidental repr() an easy mistake to make."""
        settings = _settings(google_password="hunter2")
        assert isinstance(settings.google_meet.google_password, SecretStr)
        assert "hunter2" not in repr(settings.google_meet)
        assert "hunter2" not in str(settings.google_meet.google_password)
