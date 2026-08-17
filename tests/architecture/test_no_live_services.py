"""The test suite must not reach a running service on the developer's machine.

**Written after it did.** Tests that build a session through its real factory and call ``start()``
connect the avatar leg — which is the regression ``test_media_router_startup.py`` exists to guard —
and the URL came from settings, which read the developer's ``.env``. So every run opened a live
WebSocket to the avatar service, which creates a room per session and dispatches an agent into it.
A batch of real agent jobs per ``pytest`` run, each with its own STT and TTS connection, and the
provider quota errors that followed in an actual meeting minutes later.

The containment is one autouse fixture in ``tests/conftest.py``; this asserts it is in force, so
the guarantee cannot be removed silently by a later edit to a file nobody re-reads.
"""

from __future__ import annotations

from src.config.settings import Settings
from tests.conftest import UNREACHABLE_AVATAR_URL


def test_settings_never_point_at_a_live_avatar_during_tests() -> None:
    """Both construction styles, because tests use both and only one reads ``.env``."""
    assert Settings(_env_file=None).avatar.url == UNREACHABLE_AVATAR_URL  # type: ignore[call-arg]
    assert Settings().avatar.url == UNREACHABLE_AVATAR_URL  # type: ignore[call-arg]


def test_the_unreachable_url_is_actually_unreachable() -> None:
    """Port 1 is reserved, so a connect is refused immediately rather than hanging — which is what
    keeps the fix from trading live connections for slow tests."""
    assert UNREACHABLE_AVATAR_URL.startswith("ws://127.0.0.1:1/")
