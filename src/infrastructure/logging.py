"""Structured logging.

``configure_logging`` installs a structlog pipeline whose ``add_context`` processor
reads the ambient identity from ``infrastructure.context``. The effect is that every
log line emitted anywhere inside a session carries ``session_id`` and
``correlation_id`` without the call site passing them — which is what makes the
requirement hold uniformly rather than wherever someone remembered.

JSON rendering in production-like environments, colourised key-value output locally.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog
from structlog.types import EventDict, FilteringBoundLogger, Processor, WrappedLogger

from src.config.settings import Environment, ObservabilitySettings
from src.infrastructure.context import context_fields


def add_context(_logger: WrappedLogger, _method_name: str, event_dict: EventDict) -> EventDict:
    """Inject ambient session/correlation ids into every event.

    An explicit value at the call site wins, so a log about *another* session is
    still possible where that is genuinely intended.
    """
    for key, value in context_fields().items():
        event_dict.setdefault(key, value)
    return event_dict


def configure_logging(
    settings: ObservabilitySettings, *, env: Environment = Environment.LOCAL
) -> None:
    """Configure structlog and the stdlib root logger.

    Idempotent: safe to call from both the app factory and test fixtures.
    """
    level = logging.getLevelNamesMapping()[settings.log_level]

    # Processors must be factory-agnostic: PrintLoggerFactory has no ``.name``, so
    # the ``structlog.stdlib`` variants cannot be used here. The logger name is
    # bound explicitly in ``get_logger`` instead.
    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        add_context,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    renderer: Processor
    if settings.json_logs or env.is_production_like:
        shared.append(structlog.processors.format_exc_info)
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )

    # Route stdlib logging (uvicorn, websockets) through the same handler so output
    # is homogeneous rather than two interleaved formats.
    logging.basicConfig(format="%(message)s", stream=sys.stderr, level=level, force=True)
    for noisy in ("uvicorn.access", "uvicorn.error", "websockets.client", "websockets.server"):
        logging.getLogger(noisy).setLevel(max(level, logging.INFO))


def get_logger(name: str, **initial: Any) -> FilteringBoundLogger:
    """Return a bound logger. Prefer module-level ``__name__`` as ``name``."""
    return structlog.get_logger().bind(logger=name, **initial)
