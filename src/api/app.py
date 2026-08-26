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
        platforms=sorted(container.connector_registry().supported()),
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
        summary="Bridge between browser-joined meetings and a Streaming Avatar Agent",
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

    # **No platform-specific HTTP surface any more.** There used to be two Zoom routes
    # mounted here from the container: an RTMS webhook receiver, and an app-install OAuth
    # callback for the Zoom Marketplace app that webhook belonged to. Both went with the
    # Meeting-SDK connector — every remaining connector joins with a browser and is driven
    # entirely by ``POST /sessions``, so nothing calls in from outside.

    return app
