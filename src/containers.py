"""Dependency injection wiring.

The only module that knows concrete types, and the only module permitted to import
``src.connectors`` — enforced by ``tests/architecture/test_layering.py``. Everything
else depends on protocols or domain models, so composition happens here and nowhere
else.

No globals: the container is instantiated by ``create_app`` (or directly by a test) and
reached through ``request.app.state.container``. There is no module-level singleton to
reset between tests, which makes test isolation free rather than a fixture obligation.
"""

from __future__ import annotations

from collections.abc import Callable

from dependency_injector import containers, providers

from src.config.settings import Settings
from src.connectors.google_meet.config import GoogleMeetConnectorConfig
from src.connectors.google_meet.session.google_meet_session import GoogleMeetSessionFactory
from src.connectors.teams_web.config import TeamsWebConnectorConfig
from src.connectors.teams_web.session.teams_web_session import TeamsWebSessionFactory
from src.connectors.zoom_web.config import ZoomWebConnectorConfig
from src.connectors.zoom_web.session.zoom_web_session import ZoomWebSessionFactory
from src.domain.meeting import MeetingPlatform
from src.infrastructure.logging import get_logger
from src.infrastructure.metrics import MetricsCollector
from src.services.meeting.connector_registry import ConnectorRegistry
from src.services.meeting.service import MeetingService
from src.services.session.lifecycle import SessionLifecycle
from src.services.session.registry import SessionRegistry
from src.services.session.supervisor import SessionSupervisor

logger = get_logger(__name__)


def build_connector_registry(
    *,
    settings: Settings,
    google_meet_factory: Callable[[], GoogleMeetSessionFactory] | None = None,
    zoom_web_factory: Callable[[], ZoomWebSessionFactory] | None = None,
    teams_web_factory: Callable[[], TeamsWebSessionFactory] | None = None,
) -> ConnectorRegistry:
    """Register every connector this deployment can serve.

    **Registration is conditional on configuration.** A connector that is not enabled is not
    registered, so requesting it returns a precise "no connector registered for teams_web" at
    session-creation time instead of failing deep inside a join.

    Each of the three is opt-in, and none of them has credentials to infer "wanted" from:
    they join with a browser, so what they carry is a *host* dependency — a Chromium
    install, and for Zoom a profile with a microphone already selected — that should never
    appear in a deployment that did not ask for it. So each is gated on its own
    ``MC_<NAME>__ENABLED``.

    **This function used to register a connector unconditionally**, and the shape it left
    behind is worth explaining. Two connectors have been removed: a Zoom one publishing
    through a native Meeting-SDK sidecar, and a Teams one using Graph app-hosted media on a
    Windows host. The Zoom one was in production and registered eagerly, which is why every
    optional factory still arrives as a *callable* rather than an instance — building it, and
    validating its config, happens only when that connector is configured, so one
    connector's malformed setting cannot land on another's startup path.

    A failure in any connector is caught and logged for the same reason: a malformed setting
    must degrade to "that platform unavailable", never to a service that will not boot.
    """
    registry = ConnectorRegistry()

    for platform, configured, factory in (
        (
            MeetingPlatform.GOOGLE_MEET,
            settings.google_meet.is_configured(),
            google_meet_factory,
        ),
        (MeetingPlatform.ZOOM_WEB, settings.zoom_web.is_configured(), zoom_web_factory),
        (MeetingPlatform.TEAMS_WEB, settings.teams_web.is_configured(), teams_web_factory),
    ):
        _register_optional(
            registry, platform=platform, configured=configured, factory=factory
        )

    logger.info("connectors.registered", platforms=sorted(registry.supported()))
    return registry


def _register_optional(
    registry: ConnectorRegistry,
    *,
    platform: MeetingPlatform,
    configured: bool,
    factory: Callable[[], object] | None,
) -> None:
    """Register one optional connector, absorbing any failure it brings."""
    if factory is None or not configured:
        logger.info(f"connectors.{platform}_not_registered", reason="not configured")
        return
    try:
        registry.register(platform, factory())  # type: ignore[arg-type]
    except Exception as exc:
        # Deliberately broad: whatever is wrong with this connector's configuration, the
        # correct outcome is a service without it plus a loud log line.
        logger.error(f"connectors.{platform}_registration_failed", error=str(exc))


class Container(containers.DeclarativeContainer):
    """Application object graph."""

    settings = providers.Singleton(Settings)

    # -- infrastructure ----------------------------------------------------

    metrics = providers.Singleton(
        MetricsCollector,
        histogram_capacity=providers.Callable(
            lambda s: s.observability.histogram_capacity, settings
        ),
    )

    # -- session services --------------------------------------------------

    session_registry = providers.Singleton(SessionRegistry)
    session_lifecycle = providers.Singleton(SessionLifecycle)

    session_supervisor = providers.Singleton(
        SessionSupervisor,
        registry=session_registry,
        lifecycle=session_lifecycle,
        metrics=metrics,
    )

    # -- connectors --------------------------------------------------------
    #
    # Three blocks, structurally identical and sharing nothing. None of them has a
    # credential provider, because none of them has an API credential: each joins with a
    # browser, so the nearest thing to a credential is a profile on disk. See
    # docs/connectors/google-meet.md for why that is the only way in on Meet, and
    # each connector's __init__.py for the measurements behind the other two.

    google_meet_config = providers.Singleton(GoogleMeetConnectorConfig.from_settings, settings)

    google_meet_session_factory = providers.Singleton(
        GoogleMeetSessionFactory,
        config=google_meet_config,
        metrics=metrics,
    )

    zoom_web_config = providers.Singleton(ZoomWebConnectorConfig.from_settings, settings)

    zoom_web_session_factory = providers.Singleton(
        ZoomWebSessionFactory,
        config=zoom_web_config,
        metrics=metrics,
    )

    teams_web_config = providers.Singleton(TeamsWebConnectorConfig.from_settings, settings)

    teams_web_session_factory = providers.Singleton(
        TeamsWebSessionFactory,
        config=teams_web_config,
        metrics=metrics,
    )

    # -- orchestration -----------------------------------------------------

    # ``Delegate`` passes the provider itself rather than its resolved value, which is what
    # keeps each connector's construction — and its config validation — off the startup
    # path of the others. See the docstring on ``build_connector_registry``.
    connector_registry = providers.Singleton(
        build_connector_registry,
        settings=settings,
        google_meet_factory=providers.Delegate(google_meet_session_factory),
        zoom_web_factory=providers.Delegate(zoom_web_session_factory),
        teams_web_factory=providers.Delegate(teams_web_session_factory),
    )

    meeting_service = providers.Singleton(
        MeetingService,
        registry=session_registry,
        lifecycle=session_lifecycle,
        supervisor=session_supervisor,
        connectors=connector_registry,
        # The fallback for a request that names no display name. Read from ``zoom_web``
        # because that is the default platform; a request for another connector that omits
        # the name gets this too, which is why it is a plain string rather than anything
        # platform-shaped.
        default_display_name=providers.Callable(lambda s: s.zoom_web.display_name, settings),
        metrics=metrics,
    )
