"""Connector session port.

**This port did not exist for Zoom alone, and deliberately so.** Doc 003 §0 set the
rule: *a protocol earns its place only if a second implementation exists in this
repository today.* With one connector, ``MeetingService`` held a duck-typed
``session_factory: object`` and ``SessionSupervisor`` a ``zoom_session: object`` —
honest about there being nothing to abstract over.

A second connector supplied that implementation, so the shape those two modules were
already calling is now written down. Three satisfy it today:

===============================  ==============================================
Implementation                    Package
===============================  ==============================================
``ZoomWebSession``                ``connectors.zoom_web.session``
``TeamsWebSession``               ``connectors.teams_web.session``
``GoogleMeetSession``             ``connectors.google_meet.session``
===============================  ==============================================

Both are ``Protocol``s, so they are **structural**: each session and factory satisfies
them without a single edit to its connector. That is the point — the abstraction is
written against the code that already exists rather than the code being bent to fit it.

The factory is deliberately *not* asked to declare its own platform. Registration
supplies the key (``ConnectorRegistry.register(platform, factory)``), which keeps each
factory untouched and keeps the platform mapping in one readable place:
``containers.py``.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from src.domain.health import ComponentState, HealthReport
from src.domain.session import SessionContext


@runtime_checkable
class ConnectorSession(Protocol):
    """One avatar participating in one meeting, on one platform.

    Owns the platform legs (ingest and publish) and the media pipeline wiring them
    to the avatar. ``SessionSupervisor`` drives this and nothing else, which is why
    it needs no platform knowledge.
    """

    @property
    def session(self) -> SessionContext:
        """The session this connector session belongs to."""
        ...

    async def start(self) -> None:
        """Join, attach media, and begin routing."""
        ...

    async def stop(self) -> None:
        """Leave and release every resource. Must be idempotent."""
        ...

    def health(self) -> HealthReport:
        """Component-level health. Called by the API; must not block."""
        ...

    def leg_states(self) -> tuple[ComponentState, ComponentState]:
        """``(ingest, publish)`` health — the input to session-state derivation.

        Zoom's two legs recover independently, so the pair genuinely differs.
        Teams runs one media session for both directions, so its two values move
        together — that is a property of the platform, correctly expressed as data
        rather than as a second supervisor.
        """
        ...


@runtime_checkable
class ConnectorSessionFactory(Protocol):
    """Builds a fully wired ``ConnectorSession`` for one platform.

    All concrete-type knowledge for a connector lives behind this call, so
    ``MeetingService`` composes sessions without naming RTMS, Graph, ffmpeg, or a
    sidecar.
    """

    def build(self, session: SessionContext) -> ConnectorSession:
        """Wire a connector session for ``session``. Must not perform I/O."""
        ...
