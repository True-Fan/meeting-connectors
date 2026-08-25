"""Identifier types.

``SessionId`` and ``CorrelationId`` are distinct ``NewType``s over ``str`` so that a
type checker rejects passing one where the other is expected. They are plain strings
at runtime, which keeps them free to use as dict keys and log values.

Distinction:

* ``SessionId`` — one per meeting session. Stable for the session's whole lifetime.
* ``CorrelationId`` — one per causal chain. A session has one, and inbound HTTP
  requests carry their own so an operator action can be traced end to end.
"""

from __future__ import annotations

from typing import NewType
from uuid import uuid4

SessionId = NewType("SessionId", str)
CorrelationId = NewType("CorrelationId", str)

_SESSION_PREFIX = "ses"
_CORRELATION_PREFIX = "cor"


def new_session_id() -> SessionId:
    """Return a fresh, human-scannable session identifier."""
    return SessionId(f"{_SESSION_PREFIX}_{uuid4().hex}")


def new_correlation_id() -> CorrelationId:
    """Return a fresh correlation identifier."""
    return CorrelationId(f"{_CORRELATION_PREFIX}_{uuid4().hex}")
