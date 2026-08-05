"""Ambient session and correlation identity.

Requirement: every log and metric carries the session and correlation id. Threading
both through every function signature in the media path would be invasive and easy
to get wrong, so they live in ``ContextVar``s — which asyncio propagates into tasks
created inside the context, giving correct per-session attribution across the whole
pipeline for free.

Two consumers read from here:

* the structlog processor in ``infrastructure.logging``, so every log line is tagged
  without the call site mentioning it;
* ``MetricsCollector``, which attributes a sample to a session when no explicit
  ``FrameContext`` is supplied.

Frames do **not** rely on this — they carry an explicit ``FrameContext`` (see
``domain.context``), because a frame can outlive the task that created it.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token

from src.domain.context import FrameContext
from src.domain.ids import CorrelationId, SessionId

_session_id: ContextVar[SessionId | None] = ContextVar("mc_session_id", default=None)
_correlation_id: ContextVar[CorrelationId | None] = ContextVar("mc_correlation_id", default=None)


def current_session_id() -> SessionId | None:
    return _session_id.get()


def current_correlation_id() -> CorrelationId | None:
    return _correlation_id.get()


def current_frame_context() -> FrameContext | None:
    """The ambient identity as a ``FrameContext``, if both ids are set."""
    session = _session_id.get()
    correlation = _correlation_id.get()
    if session is None or correlation is None:
        return None
    return FrameContext(session_id=session, correlation_id=correlation)


def context_fields() -> dict[str, str]:
    """Ambient identity as structured-log fields. Omits unset values."""
    fields: dict[str, str] = {}
    if (session := _session_id.get()) is not None:
        fields["session_id"] = session
    if (correlation := _correlation_id.get()) is not None:
        fields["correlation_id"] = correlation
    return fields


@contextmanager
def bind_context(
    *,
    session_id: SessionId | None = None,
    correlation_id: CorrelationId | None = None,
) -> Iterator[None]:
    """Bind identity for the duration of the block, restoring it on exit.

    Only supplied values are bound; passing one leaves the other untouched, so an
    HTTP request can set a correlation id before a session exists.
    """
    tokens: list[tuple[ContextVar[object], Token[object]]] = []
    if session_id is not None:
        tokens.append((_session_id, _session_id.set(session_id)))  # type: ignore[arg-type]
    if correlation_id is not None:
        tokens.append((_correlation_id, _correlation_id.set(correlation_id)))  # type: ignore[arg-type]
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


@contextmanager
def bind_frame_context(ctx: FrameContext) -> Iterator[None]:
    """Bind both ids from a ``FrameContext``."""
    with bind_context(session_id=ctx.session_id, correlation_id=ctx.correlation_id):
        yield
