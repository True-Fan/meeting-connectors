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

from dependency_injector import containers, providers

from src.config.settings import Settings
from src.connectors.zoom.auth.webhook_verifier import WebhookVerifier
from src.connectors.zoom.config import ZoomConnectorConfig
from src.connectors.zoom.session.zoom_session import ZoomSessionFactory
from src.connectors.zoom.webhook.router import build_router as build_zoom_webhook_router
from src.infrastructure.metrics import MetricsCollector
from src.services.meeting.service import MeetingService
from src.services.session.lifecycle import SessionLifecycle
from src.services.session.registry import SessionRegistry
from src.services.session.supervisor import SessionSupervisor


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

    # -- orchestration -----------------------------------------------------

    meeting_service = providers.Singleton(
        MeetingService,
        registry=session_registry,
        lifecycle=session_lifecycle,
        supervisor=session_supervisor,
        session_factory=zoom_session_factory,
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
