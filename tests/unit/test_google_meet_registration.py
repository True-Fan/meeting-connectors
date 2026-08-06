"""Connector registration, and the guarantees that protect Zoom and Teams.

**These are the most important tests in this changeset.** Zoom and Teams are deployed. The
only shared code Google Meet needed was one enum member, one settings section, and one
registration branch — and this file asserts that none of it can change how the other two
behave, no matter how badly the Meet configuration is broken.

The properties, in order of what they would cost if violated:

1. A Meet misconfiguration must never prevent the service from booting. Zoom is registered
   before anything Meet-related can fail.
2. Building the Meet factory must not happen on Zoom's startup path, so Meet's config
   validation cannot run — or fail — in a Zoom-only deployment.
3. An unconfigured Meet connector must be absent from the registry entirely, so requesting
   it returns a precise 4xx at session-creation time rather than failing deep inside a join.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from src.config.settings import GoogleMeetSettings, Settings, TeamsSettings
from src.containers import Container, build_connector_registry
from src.domain.meeting import MeetingPlatform
from src.services.meeting.connector_registry import ConnectorRegistry, UnsupportedPlatformError

TENANT = "72f988bf-86f1-41af-91ab-2d7cd011db47"


class StubFactory:
    """Satisfies ``ConnectorSessionFactory`` structurally, like the real ones do."""

    def __init__(self, name: str = "stub") -> None:
        self.name = name

    def build(self, session: object) -> object:  # pragma: no cover - never called here
        raise NotImplementedError


def _settings(*, teams: bool = False, meet: bool = False) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        teams=TeamsSettings(
            tenant_id=TENANT if teams else "",
            client_id="8b081ef6-4792-4def-b2c9-c363a1bf41d5" if teams else "",
            client_secret=SecretStr("secret" if teams else ""),
            sidecar_host="teams-bot.internal" if teams else "",
        ),
        google_meet=GoogleMeetSettings(
            profile_dir=Path("/tmp/meet-profile") if meet else None
        ),
    )


class TestZoomIsUnaffected:
    def test_zoom_registers_unconditionally(self) -> None:
        """Exactly as it did before either other connector existed."""
        registry = build_connector_registry(
            settings=_settings(),
            zoom_factory=StubFactory("zoom"),  # type: ignore[arg-type]
            teams_factory=lambda: StubFactory("teams"),  # type: ignore[arg-type,return-value]
        )
        assert registry.supported() == frozenset({MeetingPlatform.ZOOM})

    def test_a_broken_meet_factory_cannot_stop_the_service_booting(self) -> None:
        """Zoom is already registered by the time this can trigger."""

        def exploding() -> StubFactory:
            raise RuntimeError("meet configuration is nonsense")

        registry = build_connector_registry(
            settings=_settings(meet=True),
            zoom_factory=StubFactory("zoom"),  # type: ignore[arg-type]
            teams_factory=lambda: StubFactory("teams"),  # type: ignore[arg-type,return-value]
            google_meet_factory=exploding,  # type: ignore[arg-type]
        )
        assert MeetingPlatform.ZOOM in registry
        assert MeetingPlatform.GOOGLE_MEET not in registry

    def test_a_broken_meet_factory_cannot_stop_teams_registering(self) -> None:
        """The two optional connectors must not be able to take each other down."""

        def exploding() -> StubFactory:
            raise RuntimeError("meet configuration is nonsense")

        registry = build_connector_registry(
            settings=_settings(teams=True, meet=True),
            zoom_factory=StubFactory("zoom"),  # type: ignore[arg-type]
            teams_factory=lambda: StubFactory("teams"),  # type: ignore[arg-type,return-value]
            google_meet_factory=exploding,  # type: ignore[arg-type]
        )
        assert registry.supported() == frozenset(
            {MeetingPlatform.ZOOM, MeetingPlatform.TEAMS}
        )

    def test_a_broken_teams_factory_still_cannot_stop_meet_registering(self) -> None:
        """The pre-existing guarantee, still holding after the branch was refactored."""

        def exploding() -> StubFactory:
            raise RuntimeError("teams configuration is nonsense")

        registry = build_connector_registry(
            settings=_settings(teams=True, meet=True),
            zoom_factory=StubFactory("zoom"),  # type: ignore[arg-type]
            teams_factory=exploding,  # type: ignore[arg-type]
            google_meet_factory=lambda: StubFactory("meet"),  # type: ignore[arg-type,return-value]
        )
        assert registry.supported() == frozenset(
            {MeetingPlatform.ZOOM, MeetingPlatform.GOOGLE_MEET}
        )

    def test_the_pre_meet_call_signature_still_works(self) -> None:
        """``google_meet_factory`` is keyword-only with a default, so every caller written
        before it existed — including the tests that guard Zoom — is unchanged."""
        registry = build_connector_registry(
            settings=_settings(teams=True),
            zoom_factory=StubFactory("zoom"),  # type: ignore[arg-type]
            teams_factory=lambda: StubFactory("teams"),  # type: ignore[arg-type,return-value]
        )
        assert registry.supported() == frozenset(
            {MeetingPlatform.ZOOM, MeetingPlatform.TEAMS}
        )


class TestLaziness:
    def test_the_meet_factory_is_not_built_when_meet_is_unconfigured(self) -> None:
        """Which is what keeps Meet's config validation off Zoom's startup path."""
        calls = 0

        def counting() -> StubFactory:
            nonlocal calls
            calls += 1
            return StubFactory("meet")

        build_connector_registry(
            settings=_settings(),
            zoom_factory=StubFactory("zoom"),  # type: ignore[arg-type]
            teams_factory=lambda: StubFactory("teams"),  # type: ignore[arg-type,return-value]
            google_meet_factory=counting,  # type: ignore[arg-type]
        )
        assert calls == 0

    def test_the_container_does_not_resolve_meet_for_a_zoom_only_deployment(self) -> None:
        """The end-to-end version of the property above, through dependency-injection.

        ``providers.Delegate`` passes the provider rather than its resolved value; without it
        DI would build the Meet factory eagerly and a Meet settings error would surface as a
        Zoom startup failure.
        """
        container = Container()
        container.settings.override(_settings())
        try:
            assert container.connector_registry().supported() == frozenset(
                {MeetingPlatform.ZOOM}
            )
        finally:
            container.unwire()


class TestMeetRegistration:
    def test_meet_registers_when_configured(self) -> None:
        registry = build_connector_registry(
            settings=_settings(meet=True),
            zoom_factory=StubFactory("zoom"),  # type: ignore[arg-type]
            teams_factory=lambda: StubFactory("teams"),  # type: ignore[arg-type,return-value]
            google_meet_factory=lambda: StubFactory("meet"),  # type: ignore[arg-type,return-value]
        )
        assert registry.supported() == frozenset(
            {MeetingPlatform.ZOOM, MeetingPlatform.GOOGLE_MEET}
        )

    def test_all_three_can_coexist(self) -> None:
        registry = build_connector_registry(
            settings=_settings(teams=True, meet=True),
            zoom_factory=StubFactory("zoom"),  # type: ignore[arg-type]
            teams_factory=lambda: StubFactory("teams"),  # type: ignore[arg-type,return-value]
            google_meet_factory=lambda: StubFactory("meet"),  # type: ignore[arg-type,return-value]
        )
        assert registry.supported() == frozenset(
            {MeetingPlatform.ZOOM, MeetingPlatform.TEAMS, MeetingPlatform.GOOGLE_MEET}
        )

    def test_the_container_wires_a_real_meet_factory(self, tmp_path: Path) -> None:
        """The production path, with the real ``GoogleMeetSessionFactory``."""
        from src.connectors.google_meet.session.google_meet_session import (
            GoogleMeetSessionFactory,
        )

        container = Container()
        container.settings.override(
            Settings(  # type: ignore[call-arg]
                _env_file=None,
                google_meet=GoogleMeetSettings(profile_dir=tmp_path / "profile"),
            )
        )
        try:
            registry = container.connector_registry()
            assert MeetingPlatform.GOOGLE_MEET in registry
            assert isinstance(
                registry.get(MeetingPlatform.GOOGLE_MEET), GoogleMeetSessionFactory
            )
        finally:
            container.unwire()

    def test_an_unregistered_meet_is_a_precise_error_not_a_failed_join(self) -> None:
        """The reason registration is conditional at all."""
        registry = ConnectorRegistry().register(
            MeetingPlatform.ZOOM,
            StubFactory("zoom"),  # type: ignore[arg-type]
        )
        with pytest.raises(UnsupportedPlatformError) as exc_info:
            registry.get(MeetingPlatform.GOOGLE_MEET)
        assert "google_meet" in str(exc_info.value)


class TestPlatformEnum:
    def test_the_member_carries_identity_only(self) -> None:
        """No urls, no ports, no SDK hints — which is what keeps "route on the enum" cheap
        and "reach for a connector" expensive."""
        platform = MeetingPlatform.GOOGLE_MEET
        assert platform.value == "google_meet"
        assert not hasattr(platform, "sdk")
        assert not hasattr(platform, "transport")

    def test_the_existing_members_are_unchanged(self) -> None:
        """A renamed value would break every stored session and every API caller."""
        assert MeetingPlatform.ZOOM.value == "zoom"
        assert MeetingPlatform.TEAMS.value == "teams"

    def test_a_meeting_context_still_defaults_to_zoom(self) -> None:
        """Every existing construction site must behave exactly as before."""
        from src.domain.meeting import MeetingContext

        assert MeetingContext(meeting_number="1", display_name="x").platform is (
            MeetingPlatform.ZOOM
        )
