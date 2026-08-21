"""``TeamsWebConnectorConfig`` — the folds, and the independence from the Graph connector.

Two properties are worth guarding here, and neither is arithmetic:

* **A consumer that is off switches its observer off.** Every fold in ``from_settings`` exists
  because an observer whose ledger is disabled would be scanning a DOM on the thread that
  encodes the avatar's audio to produce events nothing reads — and ``captions_auto_enable``
  goes further, because *enabling* captions is a visible action in somebody else's meeting.
* **This connector does not read ``settings.teams``.** The two Teams connectors have opposite
  requirements, so a deployment must be able to run this one with no Azure AD app at all.
"""

from __future__ import annotations

from pathlib import Path

from src.config.settings import Settings
from src.connectors.teams_web.config import TeamsWebConnectorConfig


def _config(**teams_web: object) -> TeamsWebConnectorConfig:
    settings = Settings(
        teams_web={"enabled": True, **teams_web},  # type: ignore[arg-type]
        _env_file=None,  # type: ignore[call-arg]
    )
    return TeamsWebConnectorConfig.from_settings(settings)


class TestDefaults:
    def test_the_connector_is_off_until_asked_for(self) -> None:
        """Opt-in, like every connector after the first: it carries a Chromium dependency."""
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        assert settings.teams_web.is_configured() is False
        assert settings.teams_web.enabled is False

    def test_every_awareness_feature_is_on_once_enabled(self) -> None:
        """The features are the point of the connector; the connector itself is the switch."""
        config = _config()
        assert config.attendance_enabled
        assert config.speaker_tracking_enabled
        assert config.transcript_enabled
        assert config.chat_enabled
        assert config.hand_raise_enabled
        assert config.voice_interrupt_enabled
        assert config.context_push_enabled

    def test_captions_are_read_but_never_enabled_by_default(self) -> None:
        """Reading a panel somebody else opened and switching one on are different acts.

        The second is visible to everybody in the meeting, which is why it defaults to off
        while the first defaults to on.
        """
        config = _config()
        assert config.captions_enabled is True
        assert config.captions_auto_enable is False


class TestFolds:
    def test_captions_are_not_read_when_nothing_records_them(self) -> None:
        """The transcript is the only consumer of a caption."""
        config = _config(transcript_enabled=False)
        assert config.captions_enabled is False
        assert config.captions_auto_enable is False

    def test_captions_are_not_enabled_when_they_are_not_read(self) -> None:
        """Clicking a control the room can see, to fill a panel nobody reads."""
        config = _config(captions_auto_enable=True, captions_enabled=False)
        assert config.captions_auto_enable is False

    def test_auto_enable_survives_when_both_consumers_want_it(self) -> None:
        config = _config(captions_auto_enable=True)
        assert config.captions_enabled is True
        assert config.captions_auto_enable is True


class TestIndependenceFromTheGraphConnector:
    def test_no_graph_credential_is_required(self) -> None:
        """The whole reason this connector exists: it needs nothing from the tenant.

        If this ever fails, the credential-free connector has acquired a credential and a
        deployment that cannot get one has lost its only way into a Teams meeting.
        """
        settings = Settings(
            teams_web={"enabled": True},  # type: ignore[arg-type]
            _env_file=None,  # type: ignore[call-arg]
        )
        assert settings.teams.is_configured() is False
        assert settings.teams_web.is_configured() is True
        # Builds without touching ``settings.teams``.
        assert TeamsWebConnectorConfig.from_settings(settings).display_name == "AI Avatar"

    def test_the_two_connectors_carry_separate_display_names(self) -> None:
        """Deliberately not shared: the two join as different kinds of participant, and an
        operator may well want the guest one labelled as such."""
        settings = Settings(
            teams={"display_name": "Graph Avatar"},  # type: ignore[arg-type]
            teams_web={"enabled": True, "display_name": "Web Avatar"},  # type: ignore[arg-type]
            _env_file=None,  # type: ignore[call-arg]
        )
        assert settings.teams.display_name == "Graph Avatar"
        assert TeamsWebConnectorConfig.from_settings(settings).display_name == "Web Avatar"


class TestProfileDir:
    def test_a_tilde_path_is_expanded_and_absolute(self) -> None:
        """Pydantic parses ``~/x`` into a *relative* path whose first component is ``~``.

        Left alone, Chromium silently launches an empty profile in the working directory and
        the session looks correct while none of the profile's state is present — the trap
        ``ZoomWebSettings.profile_dir`` documents, and the reason both validators exist.
        """
        config = _config(profile_dir="~/.mc/teams-web-profile")
        assert config.profile_dir is not None
        assert config.profile_dir.is_absolute()
        assert str(config.profile_dir).startswith(str(Path.home()))
        assert "~" not in str(config.profile_dir)

    def test_no_profile_is_a_supported_configuration(self) -> None:
        """Unlike Zoom-web, where a profile is what makes the microphone work at all: Teams
        accepts the track ``getUserMedia`` returns, so a throwaway directory publishes fine."""
        assert _config().profile_dir is None


class TestEchoGate:
    def test_the_shared_default_is_used_when_the_connector_sets_nothing(self) -> None:
        assert _config().echo_gate_hangover_ms == 200

    def test_the_connector_s_own_value_wins(self) -> None:
        """Carried even though the gate is disabled — see the field's docstring."""
        assert _config(echo_gate_hangover_ms=900).echo_gate_hangover_ms == 900
