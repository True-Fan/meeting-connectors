"""Shared fixtures."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from src.config.settings import Environment, ObservabilitySettings, Settings
from src.containers import Container
from src.domain.context import FrameContext
from src.domain.ids import CorrelationId, SessionId
from src.infrastructure.logging import configure_logging
from src.infrastructure.metrics import MetricsCollector


@pytest.fixture(scope="session", autouse=True)
def _logging() -> None:
    """Quiet, deterministic logging for the whole test session."""
    configure_logging(
        ObservabilitySettings(log_level="WARNING", json_logs=True), env=Environment.LOCAL
    )


@pytest.fixture
def settings() -> Settings:
    """Settings with no environment or .env influence, so tests are hermetic."""
    return Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.fixture
def container(settings: Settings) -> Iterator[Container]:
    """A container with the hermetic settings above."""
    built = Container()
    built.settings.override(settings)
    yield built
    built.unwire()


@pytest.fixture
def metrics() -> MetricsCollector:
    return MetricsCollector(histogram_capacity=128)


@pytest.fixture
def frame_ctx() -> FrameContext:
    return FrameContext(
        session_id=SessionId("ses_test0000000000000000000000000"),
        correlation_id=CorrelationId("cor_test0000000000000000000000000"),
    )
