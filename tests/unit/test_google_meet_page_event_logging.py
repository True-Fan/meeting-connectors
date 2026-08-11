"""Regression: logging a page event must not kill the media channel.

**A one-keyword bug that stops meetings.** ``_dispatch`` passed the page's event name to
structlog as ``event=``, and structlog's bound-logger methods take the log message itself as a
parameter named ``event``. The call therefore raises::

    TypeError: meth() got multiple values for argument 'event'

Measured, not assumed: it raises at **every** level. A disabled level is replaced by a no-op
whose first parameter is also called ``event``, so ``_nop() got multiple values for argument
'event'`` — turning the log level down hides nothing.

**Why a lost log line is not the damage.** The call sits in ``_dispatch``, which runs inside
``_read_loop`` — the bridge's media channel. The exception tears that loop down, so audio stops
arriving from the meeting *and* stops being delivered to it, while the browser stays in the call
and the session settles into ``degraded``. ``stop()`` then awaits the same task, so the
exception resurfaces during teardown and **``DELETE /sessions/{id}`` fails**, leaving a session
that can be neither used nor removed. That is exactly how it presented.

The bug predates the outbound-audio work — ``git show HEAD`` has the identical call — but how
quickly it bites depends on how often the page reports anything, and enforcement reporting
``audioSenderForced`` about a second after every join made it immediate and reliable.

The last test is the one that matters most: an AST sweep of ``src/`` for the whole class, so the
next occurrence fails here rather than in a meeting.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
import structlog

from src.config.settings import Environment, ObservabilitySettings
from src.connectors.google_meet.bridge.chromium_bridge import ChromiumBridge
from src.infrastructure.logging import configure_logging

LOG_METHODS = frozenset({"debug", "info", "warning", "error", "exception", "critical"})


@pytest.fixture
def debug_logging():
    """Real structlog at DEBUG, restored afterwards.

    ``conftest`` configures WARNING for the session. A double accepting ``**kwargs`` would
    reproduce neither the collision nor its blast radius, so this uses the real thing.
    """
    configure_logging(
        ObservabilitySettings(log_level="DEBUG", json_logs=True), env=Environment.LOCAL
    )
    yield
    configure_logging(
        ObservabilitySettings(log_level="WARNING", json_logs=True), env=Environment.LOCAL
    )


class TestStructlogKeywordCollision:
    """The trap itself, so the next person recognises the error message."""

    @pytest.mark.parametrize("level", ["DEBUG", "WARNING"])
    def test_event_as_a_keyword_raises_at_every_level(self, level: str) -> None:
        """Including a level at which the method is a no-op — the no-op collides too."""
        configure_logging(
            ObservabilitySettings(log_level=level, json_logs=True), env=Environment.LOCAL
        )
        try:
            with pytest.raises(TypeError, match="event"):
                structlog.get_logger("test.collision").debug("a.message", event="pcState")
        finally:
            configure_logging(
                ObservabilitySettings(log_level="WARNING", json_logs=True),
                env=Environment.LOCAL,
            )

    def test_a_renamed_keyword_is_fine(self, debug_logging: None) -> None:
        structlog.get_logger("test.collision").debug(
            "a.message", page_event="pcState", detail={}
        )


class TestEveryPageEventSurvivesDebugLogging:
    """The media channel must survive whatever the page reports, at any log level.

    Parametrised over the events ``bridge.js`` actually emits, including the ones outbound
    audio enforcement added — those are what made this fire on every session.
    """

    @pytest.mark.parametrize(
        "event",
        [
            "pcState",
            "remoteAudioAttached",
            "remoteAudioDetached",
            "remoteAudioClaimReleased",
            "audioSenderForced",
            "audioSenderForceFailed",
            "stages",
            "getUserMedia",
            None,
        ],
    )
    def test_it_does_not_raise(self, event: str | None, debug_logging: None) -> None:
        # Unbound: the method reads nothing from ``self``, and building a real bridge would
        # need a browser. What is under test is the logging call, not the object.
        ChromiumBridge._log_page_event(  # type: ignore[arg-type]
            None, event, {"audio": True, "video": True, "tracks": 2}
        )

    def test_an_incomplete_get_user_media_still_logs(self, debug_logging: None) -> None:
        """The warning path has its own keywords and must be equally safe."""
        ChromiumBridge._log_page_event(  # type: ignore[arg-type]
            None, "getUserMedia", {"audio": True, "video": True, "tracks": 1}
        )

    def test_an_empty_detail_is_safe(self, debug_logging: None) -> None:
        ChromiumBridge._log_page_event(None, "pcState", {})  # type: ignore[arg-type]


def _logger_calls_passing_event(tree: ast.AST) -> list[int]:
    """Line numbers of ``logger.<level>(..., event=...)`` calls.

    An AST walk rather than a regex: ``event = payload.get("event")`` and ``if event ==`` are
    ordinary code that a textual scan flags, and false positives would get this guard deleted.
    """
    offenders: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in LOG_METHODS:
            continue
        target = node.func.value
        if not (isinstance(target, ast.Name) and "log" in target.id.lower()):
            continue
        if any(keyword.arg == "event" for keyword in node.keywords):
            offenders.append(node.lineno)
    return offenders


def test_no_logger_call_passes_event_as_a_keyword() -> None:
    """A guard against the class, not just the one call that was fixed."""
    offenders: list[str] = []
    for path in sorted(Path("src").rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        offenders.extend(f"{path}:{line}" for line in _logger_calls_passing_event(tree))

    assert not offenders, (
        "structlog takes the log message as a parameter named 'event', so these calls raise "
        "TypeError at every level and, inside a read loop, take the media channel down with "
        "them. Rename the keyword (e.g. page_event=):\n  " + "\n  ".join(offenders)
    )


def test_the_guard_would_catch_a_regression() -> None:
    """The guard is only worth having if it fires — so prove it does."""
    tree = ast.parse('logger.debug("a.message", event="x")\n')
    assert _logger_calls_passing_event(tree) == [1]

    ok = ast.parse('event = payload.get("event")\nif event == "x":\n    pass\n')
    assert _logger_calls_passing_event(ok) == []

    renamed = ast.parse('logger.debug("a.message", page_event="x")\n')
    assert _logger_calls_passing_event(renamed) == []
