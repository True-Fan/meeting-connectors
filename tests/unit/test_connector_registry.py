"""Connector selection, and the guarantees that protect Zoom.

Doc 003 §0.1 cut a registry as "indirection with one entry", correctly. With two
connectors it removes an ``if platform is ZOOM ... elif`` from ``MeetingService`` that
every future connector would otherwise have to edit.

The tests that matter most here are the backward-compatibility ones: Zoom is in
production, so a Teams misconfiguration must never be able to change how it behaves.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from src.config.settings import Settings, TeamsSettings
from src.containers import Container, build_connector_registry
from src.domain.meeting import MeetingPlatform
from src.services.meeting.connector_registry import (
    ConnectorRegistry,
    UnsupportedPlatformError,
)


class StubFactory:
    """Satisfies ``ConnectorSessionFactory`` structurally, like the real ones do."""

    def __init__(self, name: str = "stub") -> None:
        self.name = name

    def build(self, session: object) -> object:  # pragma: no cover - never called here
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #


def test_register_and_get() -> None:
    zoom, teams = StubFactory("zoom"), StubFactory("teams")
    registry = ConnectorRegistry()
    registry.register(MeetingPlatform.ZOOM, zoom).register(MeetingPlatform.TEAMS, teams)

    assert registry.get(MeetingPlatform.ZOOM) is zoom
    assert registry.get(MeetingPlatform.TEAMS) is teams
    assert registry.supported() == frozenset({MeetingPlatform.ZOOM, MeetingPlatform.TEAMS})
    assert len(registry) == 2
    assert MeetingPlatform.ZOOM in registry
    assert sorted(registry) == sorted([MeetingPlatform.ZOOM, MeetingPlatform.TEAMS])


def test_unregistered_platform_names_what_is_available() -> None:
    registry = ConnectorRegistry().register(MeetingPlatform.ZOOM, StubFactory())

    with pytest.raises(UnsupportedPlatformError) as exc_info:
        registry.get(MeetingPlatform.TEAMS)

    message = str(exc_info.value)
    assert "teams" in message
    assert "registered: zoom" in message
    assert exc_info.value.platform is MeetingPlatform.TEAMS


def test_double_registration_is_refused() -> None:
    """Silently replacing one connector with another is never intentional."""
    registry = ConnectorRegistry().register(MeetingPlatform.ZOOM, StubFactory("a"))
    with pytest.raises(ValueError, match="already registered"):
        registry.register(MeetingPlatform.ZOOM, StubFactory("b"))


def test_empty_registry_reports_none_available() -> None:
    with pytest.raises(UnsupportedPlatformError, match="registered: none"):
        ConnectorRegistry().get(MeetingPlatform.ZOOM)


# --------------------------------------------------------------------------- #
# Container wiring — the Zoom-safety properties
# --------------------------------------------------------------------------- #


def _teams_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "tenant_id": "72f988bf-86f1-41af-91ab-2d7cd011db47",
        "client_id": "8b081ef6-4792-4def-b2c9-c363a1bf41d5",
        "client_secret": SecretStr("secret"),
        "sidecar_host": "teams-bot.internal",
    }
    base.update(overrides)
    return Settings(_env_file=None, teams=TeamsSettings(**base))  # type: ignore[arg-type,call-arg]


def test_zoom_only_deployment_registers_only_zoom() -> None:
    """Production today. Adding the Teams connector must not add surface here."""
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    registry = build_connector_registry(
        settings=settings,
        zoom_factory=StubFactory("zoom"),
        teams_factory=lambda: StubFactory("teams"),
    )

    assert registry.supported() == frozenset({MeetingPlatform.ZOOM})


def test_teams_factory_is_not_built_when_unconfigured() -> None:
    """It is passed as a callable precisely so Teams' config validation stays off Zoom's
    startup path. If it were built eagerly, a bad Teams setting would break boot."""
    built = False

    def _factory() -> StubFactory:
        nonlocal built
        built = True
        return StubFactory("teams")

    build_connector_registry(
        settings=Settings(_env_file=None),  # type: ignore[call-arg]
        zoom_factory=StubFactory("zoom"),
        teams_factory=_factory,
    )

    assert built is False


def test_configured_teams_registers_both() -> None:
    registry = build_connector_registry(
        settings=_teams_settings(),
        zoom_factory=StubFactory("zoom"),
        teams_factory=lambda: StubFactory("teams"),
    )

    assert registry.supported() == frozenset({MeetingPlatform.ZOOM, MeetingPlatform.TEAMS})


def test_a_broken_teams_factory_leaves_zoom_working() -> None:
    """The safeguard that matters most. A malformed Teams setting must degrade to
    "Teams unavailable", never to a service that will not boot."""

    def _explodes() -> StubFactory:
        raise ValueError("Teams cannot send 1280x720@25")

    registry = build_connector_registry(
        settings=_teams_settings(),
        zoom_factory=StubFactory("zoom"),
        teams_factory=_explodes,
    )

    assert registry.supported() == frozenset({MeetingPlatform.ZOOM})


def test_real_container_registers_zoom_by_default() -> None:
    container = Container()
    container.settings.override(Settings(_env_file=None))  # type: ignore[call-arg]

    assert container.connector_registry().supported() == frozenset({MeetingPlatform.ZOOM})
    assert container.meeting_service().supported_platforms == frozenset(
        {MeetingPlatform.ZOOM}
    )


def test_real_container_registers_teams_when_configured() -> None:
    container = Container()
    container.settings.override(_teams_settings())

    assert container.meeting_service().supported_platforms == frozenset(
        {MeetingPlatform.ZOOM, MeetingPlatform.TEAMS}
    )


def test_real_container_survives_an_invalid_teams_geometry() -> None:
    """25 fps is this repository's shared default and Teams does not offer it — the most
    likely real misconfiguration, and it must not take Zoom down with it."""
    container = Container()
    container.settings.override(_teams_settings(video_fps=25))

    assert container.connector_registry().supported() == frozenset({MeetingPlatform.ZOOM})
