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
from src.connectors.teams.config import TeamsConnectorConfig
from src.connectors.teams.session.teams_session import TeamsSessionFactory
from src.connectors.zoom.auth.webhook_verifier import WebhookVerifier
from src.connectors.zoom.config import ZoomConnectorConfig
from src.connectors.zoom.oauth.router import build_router as build_zoom_oauth_router
from src.connectors.zoom.session.zoom_session import ZoomSessionFactory
from src.connectors.zoom.webhook.router import build_router as build_zoom_webhook_router
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
    zoom_factory: ZoomSessionFactory,
    teams_factory: Callable[[], TeamsSessionFactory],
    google_meet_factory: Callable[[], GoogleMeetSessionFactory] | None = None,
    zoom_web_factory: Callable[[], ZoomWebSessionFactory] | None = None,
) -> ConnectorRegistry:
    """Register every connector this deployment can serve.

    **Registration is conditional on configuration.** A connector whose credentials are
    absent is not registered, so requesting it returns a precise "no connector
    registered for teams" at session-creation time instead of failing deep inside a
    join with a missing-tenant error. A Zoom-only deployment — which is what production
    is today — therefore carries no Teams surface at all.

    Zoom registers unconditionally, exactly as it did before Teams existed. Preserving
    that is worth more than symmetry between the branches.

    **Two safeguards exist solely to protect Zoom**, because this function is on the
    startup path of a service that is already in production:

    1. Each optional factory arrives as a *callable*, not an instance, so building it —
       and validating its config — only happens when that connector is configured.
       Passing an instance would make dependency-injection resolve it eagerly and put
       another connector's config validation on Zoom's startup path.
    2. A failure in an optional connector is caught and logged. A malformed setting must
       degrade to "that platform unavailable", never to a service that will not boot.
       Zoom is already registered by the time any of this can trigger.

    Adding Google Meet was one more branch here and nothing else in ``services/``, exactly
    as this docstring predicted. ``google_meet_factory`` is keyword-only with a default so
    that any caller written before it existed — including the tests that guard Zoom's
    behaviour — keeps working unchanged.
    """
    registry = ConnectorRegistry()
    registry.register(MeetingPlatform.ZOOM, zoom_factory)

    _register_optional(
        registry,
        platform=MeetingPlatform.TEAMS,
        configured=settings.teams.is_configured(),
        factory=teams_factory,
    )
    _register_optional(
        registry,
        platform=MeetingPlatform.GOOGLE_MEET,
        configured=settings.google_meet.is_configured(),
        factory=google_meet_factory,
    )
    # Opt-in via MC_ZOOM_WEB__ENABLED. It has no credentials of its own to infer
    # "wanted" from, and it carries a host dependency (a capture device) that should
    # never appear in a deployment that did not ask for it.
    _register_optional(
        registry,
        platform=MeetingPlatform.ZOOM_WEB,
        configured=settings.zoom_web.is_configured(),
        factory=zoom_web_factory,
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
    """Register one optional connector, absorbing any failure it brings.

    Extracted when Google Meet arrived, because the alternative was a second copy of the
    same guard-and-catch — and the two copies drifting is precisely how a broken Teams
    config would start taking Zoom's startup with it.
    """
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

    # -- Google Meet connector ---------------------------------------------
    #
    # Structurally parallel to the two blocks above and sharing nothing with them. There is
    # no credential provider here because there is no API credential: Google ships no way
    # to publish media into a conference, so the avatar joins as a signed-in Chromium and
    # the "credential" is a browser profile on disk. See
    # connectors/google_meet/capabilities.py for the evidence behind that.

    google_meet_config = providers.Singleton(GoogleMeetConnectorConfig.from_settings, settings)

    zoom_web_config = providers.Singleton(ZoomWebConnectorConfig.from_settings, settings)

    zoom_web_session_factory = providers.Singleton(
        ZoomWebSessionFactory,
        config=zoom_web_config,
        metrics=metrics,
    )

    google_meet_session_factory = providers.Singleton(
        GoogleMeetSessionFactory,
        config=google_meet_config,
        metrics=metrics,
    )

    # -- orchestration -----------------------------------------------------

    # ``Delegate`` passes the provider itself rather than its resolved value, which is
    # what keeps Teams and Google Meet construction off Zoom's startup path — see the
    # docstring above.
    connector_registry = providers.Singleton(
        build_connector_registry,
        settings=settings,
        zoom_factory=zoom_session_factory,
        teams_factory=providers.Delegate(teams_session_factory),
        google_meet_factory=providers.Delegate(google_meet_session_factory),
        zoom_web_factory=providers.Delegate(zoom_web_session_factory),
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
