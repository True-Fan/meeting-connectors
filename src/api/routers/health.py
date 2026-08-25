"""Health endpoints."""

from __future__ import annotations

import time

from fastapi import APIRouter

from src import __version__
from src.api.dependencies import SettingsDep, SupervisorDep
from src.api.dto import HealthResponse
from src.domain.health import HealthReport

router = APIRouter(tags=["health"])

_STARTED_AT = time.monotonic()


@router.get("/health", response_model=HealthResponse, summary="Service health")
async def health(settings: SettingsDep, supervisor: SupervisorDep) -> HealthResponse:
    """Report service health.

    Deliberately reports process health, **not** aggregate session health: one failing
    session must not make the whole service look unready and get it restarted or pulled
    from a load balancer. Per-session health is at ``GET /sessions/{id}``.
    """
    report = HealthReport()
    return HealthResponse(
        status=report.state,
        app=settings.app_name,
        version=__version__,
        env=settings.env,
        uptime_seconds=round(time.monotonic() - _STARTED_AT, 3),
        active_sessions=len(supervisor),
        components=(),
    )
