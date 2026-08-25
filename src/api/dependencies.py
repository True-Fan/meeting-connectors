"""FastAPI dependency providers.

Resolved from the container on ``app.state`` rather than through
``dependency_injector``'s ``@inject`` decorator. Explicit lookup keeps the wiring
visible, avoids import-time magic, and lets a test build an app with an overridden
container and no global state to unwind.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from src.config.settings import Settings
from src.containers import Container
from src.infrastructure.metrics import MetricsCollector
from src.services.meeting.service import MeetingService
from src.services.session.supervisor import SessionSupervisor


def get_container(request: Request) -> Container:
    return request.app.state.container  # type: ignore[no-any-return]


def get_settings(container: Annotated[Container, Depends(get_container)]) -> Settings:
    return container.settings()


def get_metrics(container: Annotated[Container, Depends(get_container)]) -> MetricsCollector:
    return container.metrics()


def get_meeting_service(
    container: Annotated[Container, Depends(get_container)],
) -> MeetingService:
    return container.meeting_service()


def get_supervisor(
    container: Annotated[Container, Depends(get_container)],
) -> SessionSupervisor:
    return container.session_supervisor()


SettingsDep = Annotated[Settings, Depends(get_settings)]
MetricsDep = Annotated[MetricsCollector, Depends(get_metrics)]
MeetingServiceDep = Annotated[MeetingService, Depends(get_meeting_service)]
SupervisorDep = Annotated[SessionSupervisor, Depends(get_supervisor)]
