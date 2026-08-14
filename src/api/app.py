"""FastAPI application factory.

A factory rather than a module-level ``app``: no global application object, so a test
can build an app with overridden settings and get genuine isolation.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from src import __version__
from src.api.middleware import CorrelationIdMiddleware
from src.api.routers import health, metrics, participants, sessions
from src.config.settings import Settings
from src.containers import Container
from src.infrastructure.logging import configure_logging, get_logger

logger = get_logger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start-up and shut-down."""
    container: Container = app.state.container
    settings: Settings = container.settings()

    logger.info(
        "service.startup",
        app=settings.app_name,
        version=__version__,
        env=settings.env,
        zoom_ingest_configured=settings.zoom.is_configured(),
        zoom_publish_configured=settings.zoom.is_publish_configured(),
        avatar_url=settings.avatar.url,
    )
    try:
        yield
    finally:
        # Drain sessions on the way out, so a redeploy never leaves a bot sitting in a
        # meeting talking to nobody.
        try:
            await container.meeting_service().stop_all()
        except Exception as exc:
            logger.warning("service.shutdown_drain_failed", error=str(exc))
        logger.info("service.shutdown", app=settings.app_name)


def create_app(*, container: Container | None = None) -> FastAPI:
    """Build the application.

    Args:
        container: Pre-built container, for tests that need overridden providers.
            A fresh one is created when omitted.
    """
    container = container or Container()
    settings: Settings = container.settings()

    configure_logging(settings.observability, env=settings.env)

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        summary="Bridge between Zoom meetings and a Streaming Avatar Agent",
        lifespan=_lifespan,
    )
    app.state.container = container
    app.add_middleware(CorrelationIdMiddleware)

    app.include_router(health.router)
    app.include_router(metrics.router)
    app.include_router(sessions.router)
    # Same ``/sessions`` prefix, separate module: attendance is read and seeded per session but
    # is not part of the lifecycle, and keeping it out of ``sessions.py`` keeps that router's
    # claim intact — it still mentions no platform and no connector-specific concept.
    app.include_router(participants.router)

    # Platform-specific webhook, resolved from the container rather than imported, so
    # this module names no connector (doc 003 §1.5).
    app.include_router(container.zoom_webhook_router(), prefix="/webhooks/zoom")
    app.include_router(container.zoom_oauth_router())

    return app
