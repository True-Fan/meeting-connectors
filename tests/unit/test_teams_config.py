"""Teams connector configuration.

The validation here exists to convert a class of failure that would otherwise happen on a
Windows host, mid-join, into a startup error on the machine the operator is looking at.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from src.config.settings import Settings, TeamsSettings
from src.connectors.teams.config import (
    SUPPORTED_SEND_SAMPLE_RATES,
    SUPPORTED_SEND_VIDEO_FORMATS,
    TeamsConnectorConfig,
)
from src.connectors.teams.exceptions import TeamsConfigurationError
from src.domain.avatar import AVATAR_INPUT_FORMAT


def _settings(**teams: object) -> Settings:
    base = {
        "tenant_id": "72f988bf-86f1-41af-91ab-2d7cd011db47",
        "client_id": "8b081ef6-4792-4def-b2c9-c363a1bf41d5",
        "client_secret": SecretStr("secret"),
        "sidecar_host": "teams-bot.internal",
    }
    base.update(teams)
    return Settings(_env_file=None, teams=TeamsSettings(**base))  # type: ignore[call-arg]


def test_defaults_are_valid() -> None:
    config = TeamsConnectorConfig.from_settings(_settings())

    assert config.is_configured()
    assert config.video_format.width == 1280
    assert config.video_format.fps == 30
    assert config.publish_audio_format.sample_rate_hz == 16_000
    assert config.unmixed_audio is True


def test_ingest_format_matches_the_avatar_contract_exactly() -> None:
    """The headline finding for Teams: app-hosted media delivers ``Pcm16K`` mono, which
    *is* the avatar's input format — so Teams needs no resampler either.

    Doc 002 §3.3 predicted a resampler here based on Teams' network codecs (SILK/G.722).
    Those are the transport codecs; the media platform hands the application decoded PCM
    at the rate the socket was configured for. Zero-resample is therefore a property of
    both connectors, not a Zoom-only accident.
    """
    config = TeamsConnectorConfig.from_settings(_settings())
    assert config.ingest_audio_format == AVATAR_INPUT_FORMAT


def test_rejects_a_video_geometry_teams_cannot_send() -> None:
    """Zoom takes any geometry; Teams negotiates against an enumerated list. 25 fps is
    the trap — it is this repository's shared default and Teams does not offer it."""
    with pytest.raises(TeamsConfigurationError, match="cannot send 1280x720@25"):
        TeamsConnectorConfig.from_settings(_settings(video_fps=25))


def test_rejects_an_unsupported_sample_rate() -> None:
    with pytest.raises(TeamsConfigurationError, match="cannot send 22050 Hz"):
        TeamsConnectorConfig.from_settings(_settings(publish_sample_rate_hz=22_050))


def test_every_supported_video_format_is_accepted() -> None:
    for width, height, fps in sorted(SUPPORTED_SEND_VIDEO_FORMATS):
        config = TeamsConnectorConfig.from_settings(
            _settings(video_width=width, video_height=height, video_fps=fps)
        )
        assert (config.video_format.width, config.video_format.fps) == (width, fps)


def test_every_supported_sample_rate_is_accepted() -> None:
    for rate in sorted(SUPPORTED_SEND_SAMPLE_RATES):
        config = TeamsConnectorConfig.from_settings(_settings(publish_sample_rate_hz=rate))
        assert config.publish_audio_format.sample_rate_hz == rate


def test_is_configured_requires_every_credential() -> None:
    for field in ("tenant_id", "client_id", "sidecar_host"):
        config = TeamsConnectorConfig.from_settings(_settings(**{field: ""}))
        assert not config.is_configured()

    config = TeamsConnectorConfig.from_settings(_settings(client_secret=SecretStr("")))
    assert not config.is_configured()


def test_require_configured_names_what_is_missing() -> None:
    config = TeamsConnectorConfig.from_settings(_settings(tenant_id="", sidecar_host=""))
    with pytest.raises(TeamsConfigurationError) as exc_info:
        config.require_configured()

    message = str(exc_info.value)
    assert "teams.tenant_id" in message
    assert "teams.sidecar_host" in message
    assert "teams.client_id" not in message


def test_require_configured_passes_when_complete() -> None:
    TeamsConnectorConfig.from_settings(_settings()).require_configured()


def test_secrets_are_not_leaked_by_repr() -> None:
    """Structured logging makes an accidental repr of config an easy mistake, so the
    secret is a ``SecretStr`` and masks itself. The field *name* still appears, which is
    fine — the value is what must not."""
    config = TeamsConnectorConfig.from_settings(
        _settings(client_secret=SecretStr("s3cr3t-value"))
    )
    rendered = repr(config)

    assert "s3cr3t-value" not in rendered
    assert "**********" in rendered
    # But the real value must still be reachable for the join payload.
    assert config.client_secret.get_secret_value() == "s3cr3t-value"


def test_pipeline_settings_are_shared_with_zoom() -> None:
    """Queue depths and echo timing are pipeline properties, not platform ones. Teams
    reading them from the shared ``media`` block rather than duplicating them is the
    boundary working as intended."""
    settings = _settings()
    config = TeamsConnectorConfig.from_settings(settings)

    assert config.inbound_queue_size == settings.media.inbound_queue_size
    assert config.video_queue_size == settings.media.video_queue_size
    assert config.audio_queue_size == settings.media.audio_queue_size
    assert config.echo_gate_hangover_ms == settings.media.echo_gate_hangover_ms


def test_teams_audio_rate_is_independent_of_zoom() -> None:
    """The two SDKs want different rates, so Teams must not read Zoom's setting."""
    settings = _settings()
    assert settings.media.publish_sample_rate_hz == 32_000  # Zoom's default
    config = TeamsConnectorConfig.from_settings(settings)
    assert config.publish_audio_format.sample_rate_hz == 16_000


def test_teams_settings_absent_by_default() -> None:
    """A deployment that has never heard of Teams must be unaffected."""
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert not settings.teams.is_configured()
