"""Shared fixtures."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from src.config.settings import Environment, ObservabilitySettings, Settings
from src.containers import Container
from src.domain.context import FrameContext
from src.domain.ids import CorrelationId, SessionId
from src.infrastructure.logging import configure_logging
from src.infrastructure.metrics import MetricsCollector

UNREACHABLE_AVATAR_URL = "ws://127.0.0.1:1/stream"
"""Where the test suite points the avatar agent: a port nothing can be listening on.

**This is a containment fix for a real incident, not a tidy-up.** Several tests build a session
through its *real* factory and call ``start()`` — which is the point, because
``MediaRouter.run()`` connecting the avatar leg is itself the regression they guard
(``test_media_router_startup.py``). The avatar URL came from settings, and settings read the
developer's ``.env``, so every one of those tests opened a **live** WebSocket to the avatar
service running on the machine. That service creates a room per session and dispatches an agent
into it, so a single ``pytest`` run spawned a batch of real agent jobs — observed as rooms named
``meet-ses_regress0000000000000000000000`` and ``meet-ses_teams00000000000000000000000``, each
with its own STT and TTS connection, and as the LiveKit quota errors (``429``) that followed in a
*live meeting* run minutes later.

Port 1 is reserved and refuses immediately, so the connect fails fast rather than hanging: the
router logs ``router.avatar_unreachable`` and degrades, which is exactly the documented behaviour
for an unreachable agent and leaves every assertion in those tests intact.

Tests that *need* an avatar bring their own — ``test_avatar_leg_startup.py`` starts a stub server
and passes its URL explicitly, and an explicit value outranks the environment.
"""


@pytest.fixture(scope="session", autouse=True)
def _no_live_avatar() -> Iterator[None]:
    """Point every ``Settings`` built in this suite at an avatar that cannot exist.

    Autouse and session-scoped on purpose: the property has to hold for tests nobody remembered
    to check, including ones written later. An environment variable is the lever because
    ``Settings`` reads the environment ahead of ``.env`` — so this wins without any test having to
    opt in, and without touching the developer's file.
    """
    previous = os.environ.get("MC_AVATAR__URL")
    os.environ["MC_AVATAR__URL"] = UNREACHABLE_AVATAR_URL
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("MC_AVATAR__URL", None)
        else:
            os.environ["MC_AVATAR__URL"] = previous


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
