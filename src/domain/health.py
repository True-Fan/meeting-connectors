"""Health models.

Component health is the input to session state derivation (``domain.session``) and
to the ``/health`` endpoint. Kept separate from ``SessionState`` because a component
is healthy or not regardless of what any session concludes from it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


class ComponentState(StrEnum):
    """Health of a single component."""

    UNKNOWN = "unknown"
    """Not yet started, or starting. Distinct from unhealthy."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    """Functioning but impaired — e.g. dropping frames under backpressure."""

    UNHEALTHY = "unhealthy"

    @property
    def is_serving(self) -> bool:
        """True when the component can still carry media."""
        return self in (ComponentState.HEALTHY, ComponentState.DEGRADED)


@dataclass(frozen=True, slots=True)
class ComponentHealth:
    """A point-in-time health reading for one named component."""

    name: str
    state: ComponentState
    detail: str | None = None
    observed_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def healthy(cls, name: str, detail: str | None = None) -> ComponentHealth:
        return cls(name=name, state=ComponentState.HEALTHY, detail=detail)

    @classmethod
    def unhealthy(cls, name: str, detail: str) -> ComponentHealth:
        return cls(name=name, state=ComponentState.UNHEALTHY, detail=detail)

    @classmethod
    def unknown(cls, name: str, detail: str | None = None) -> ComponentHealth:
        return cls(name=name, state=ComponentState.UNKNOWN, detail=detail)


@dataclass(frozen=True, slots=True)
class HealthReport:
    """Aggregate health of a set of components."""

    components: tuple[ComponentHealth, ...] = ()

    @property
    def state(self) -> ComponentState:
        """Worst-of aggregation.

        An empty report is ``HEALTHY``: at M1 the service has no media components,
        and a running process with nothing to do is healthy.
        """
        if not self.components:
            return ComponentState.HEALTHY
        order = (
            ComponentState.UNHEALTHY,
            ComponentState.UNKNOWN,
            ComponentState.DEGRADED,
            ComponentState.HEALTHY,
        )
        states = {c.state for c in self.components}
        for candidate in order:
            if candidate in states:
                return candidate
        return ComponentState.HEALTHY

    def component(self, name: str) -> ComponentHealth | None:
        return next((c for c in self.components if c.name == name), None)
