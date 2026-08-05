"""Metrics endpoints.

Two views, for the reason set out in ``infrastructure.metrics``:

* ``/metrics`` — aggregated across sessions. Safe to scrape: no unbounded label.
* ``/metrics/sessions/{session_id}`` — full per-session detail including the
  correlation id, for debugging one conversation.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import PlainTextResponse

from src.api.dependencies import MetricsDep
from src.domain.ids import SessionId
from src.infrastructure.prometheus import render

router = APIRouter(tags=["metrics"])

_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


@router.get(
    "/metrics",
    response_class=PlainTextResponse,
    summary="Aggregated metrics in Prometheus text format",
)
async def metrics(collector: MetricsDep) -> PlainTextResponse:
    """Aggregate metrics across all sessions."""
    return PlainTextResponse(content=render(collector.snapshot()), media_type=_CONTENT_TYPE)


@router.get(
    "/metrics/sessions/{session_id}",
    response_class=PlainTextResponse,
    summary="Per-session metrics, including correlation id",
)
async def session_metrics(session_id: str, collector: MetricsDep) -> PlainTextResponse:
    """Metrics for one session. 404 when the session is unknown or already reaped."""
    snapshot = collector.session_snapshot(SessionId(session_id))
    if snapshot is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no metrics for session {session_id}",
        )
    return PlainTextResponse(content=render(snapshot), media_type=_CONTENT_TYPE)
