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
from src.connectors.teams.config import TeamsConnectorConfig
from src.connectors.teams.session.teams_session import TeamsSessionFactory
from src.connectors.zoom.auth.webhook_verifier import WebhookVerifier
from src.connectors.zoom.config import ZoomConnectorConfig
from src.connectors.zoom.oauth.router import build_router as build_zoom_oauth_router
from src.connectors.zoom.session.zoom_session import ZoomSessionFactory
from src.connectors.zoom.webhook.router import build_router as build_zoom_webhook_router
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
    zoom_factory: ZoomSessionFactory,
    teams_factory: Callable[[], TeamsSessionFactory],
) -> ConnectorRegistry:
    """Register every connector this deployment can serve.

    **Registration is conditional on configuration.** A connector whose credentials are
    absent is not registered, so requesting it returns a precise "no connector
    registered for teams" at session-creation time instead of failing deep inside a
    join with a missing-tenant error. A Zoom-only deployment — which is what production
    is today — therefore carries no Teams surface at all.

    Zoom registers unconditionally, exactly as it did before Teams existed. Preserving
    that is worth more than symmetry between the two branches.

    **Two safeguards exist solely to protect Zoom**, because this function is on the
    startup path of a service that is already in production:

    1. ``teams_factory`` is a *callable*, not an instance, so building the Teams
       factory — and validating Teams config — only happens when Teams is configured.
       Passing an instance would make dependency-injection resolve it eagerly and put
       Teams' config validation on Zoom's startup path.
    2. A Teams failure is caught and logged. A malformed Teams setting must degrade to
       "Teams unavailable", never to a service that will not boot. Zoom is already
       registered by the time this can trigger.

    Adding Google Meet is one more branch here and nothing else in ``services/``.
    """
    registry = ConnectorRegistry()
    registry.register(MeetingPlatform.ZOOM, zoom_factory)

    if not settings.teams.is_configured():
        logger.info("connectors.teams_not_registered", reason="not configured")
        return registry

    try:
        registry.register(MeetingPlatform.TEAMS, teams_factory())
    except Exception as exc:
        # Deliberately broad: whatever is wrong with the Teams configuration, the
        # correct outcome is a Zoom-only service plus a loud log line.
        logger.error("connectors.teams_registration_failed", error=str(exc))
        return registry

    logger.info("connectors.registered", platforms=sorted(registry.supported()))
    return registry


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

    # -- Zoom connector ----------------------------------------------------

    zoom_config = providers.Singleton(ZoomConnectorConfig.from_settings, settings)

    zoom_session_factory = providers.Singleton(
        ZoomSessionFactory,
        config=zoom_config,
        metrics=metrics,
    )

    zoom_webhook_verifier = providers.Singleton(
        WebhookVerifier,
        secret_token=providers.Callable(
            lambda s: s.zoom.webhook_secret_token, settings
        ),
    )

    # -- Teams connector ---------------------------------------------------
    #
    # Structurally parallel to the Zoom block above and sharing nothing with it. The
    # Teams media runtime is a .NET sidecar on a Windows host, so there is no local
    # process or socket to provide here — only the config the link needs to reach it
    # (see connectors/teams/sidecar/link.py).

    teams_config = providers.Singleton(TeamsConnectorConfig.from_settings, settings)

    teams_session_factory = providers.Singleton(
        TeamsSessionFactory,
        config=teams_config,
        metrics=metrics,
    )

    # -- orchestration -----------------------------------------------------

    # ``Delegate`` passes the provider itself rather than its resolved value, which is
    # what keeps Teams construction off Zoom's startup path — see the docstring above.
    connector_registry = providers.Singleton(
        build_connector_registry,
        settings=settings,
        zoom_factory=zoom_session_factory,
        teams_factory=providers.Delegate(teams_session_factory),
    )

    meeting_service = providers.Singleton(
        MeetingService,
        registry=session_registry,
        lifecycle=session_lifecycle,
        supervisor=session_supervisor,
        connectors=connector_registry,
        default_display_name=providers.Callable(lambda s: s.zoom.display_name, settings),
        metrics=metrics,
    )

    # -- platform-specific HTTP surface ------------------------------------
    #
    # Built here and mounted by ``api/app.py`` from the container, so the API layer
    # never imports a connector even though the webhook itself is Zoom-specific.
    zoom_webhook_router = providers.Singleton(
        build_zoom_webhook_router,
        verifier=zoom_webhook_verifier,
        meeting_service=meeting_service,
    )

    # The app-install OAuth redirect target — see connectors/zoom/oauth/router.py for
    # why this exists despite the bridge never exchanging the code it carries.
    zoom_oauth_router = providers.Singleton(build_zoom_oauth_router)
