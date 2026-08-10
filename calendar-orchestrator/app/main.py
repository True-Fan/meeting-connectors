"""FastAPI entrypoint.

Runs one background job on an ``AsyncIOScheduler``: poll Google Calendar every
``poll_interval_s`` and reconcile join jobs against the result (see ``scheduler.py``). The
HTTP surface itself is small and mostly for operability — health, forcing a sync, and
inspecting what's currently scheduled — since the actual work happens on the scheduler, not
in response to a request.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI, HTTPException, status

from app.auth import CredentialError, build_credentials
from app.calendar_service import CalendarService, CalendarSyncError
from app.config import Settings
from app.scheduler import MeetingScheduler
from app.state import TriggeredEventStore

logger = logging.getLogger(__name__)

_SYNC_JOB_ID = "calendar-sync"


async def run_sync(calendar: CalendarService, meeting_scheduler: MeetingScheduler) -> int:
    """One poll-and-reconcile cycle. Returns the number of upcoming meetings found.

    Isolated as its own function (rather than inlined in the lifespan) so both the periodic
    job and the manual ``POST /sync`` endpoint share exactly one code path.
    """
    events = await calendar.list_upcoming_meetings()
    await meeting_scheduler.sync(events)
    logger.info("sync complete: %d upcoming meeting(s) with Meet links", len(events))
    return len(events)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    # apscheduler logs every job add/remove at INFO by default, which is noisy at this
    # service's own INFO level; let our own log lines carry that instead.
    logging.getLogger("apscheduler").setLevel(logging.WARNING)

    try:
        credentials = build_credentials(settings.google)
    except CredentialError:
        logger.exception("failed to build Google credentials — see README.md for setup")
        raise

    calendar = CalendarService(credentials, settings)
    state = TriggeredEventStore(settings.state_file)
    ap_scheduler = AsyncIOScheduler(timezone="UTC")
    meeting_scheduler = MeetingScheduler(ap_scheduler, state, settings)

    # The recurring job covers every sync after startup; next_run_time is left at its
    # default (now + interval) since the very first sync is run explicitly below instead
    # of making startup wait a full interval for the initial reconciliation.
    ap_scheduler.add_job(
        run_sync,
        trigger=IntervalTrigger(seconds=settings.scheduling.poll_interval_s),
        id=_SYNC_JOB_ID,
        kwargs={"calendar": calendar, "meeting_scheduler": meeting_scheduler},
    )
    ap_scheduler.start()

    try:
        await run_sync(calendar, meeting_scheduler)
    except CalendarSyncError:
        # Don't crash the service over a transient failure on the very first sync — the
        # periodic job will retry on its own schedule.
        logger.exception("initial calendar sync failed; will retry on schedule")

    app.state.settings = settings
    app.state.calendar = calendar
    app.state.scheduler = meeting_scheduler
    app.state.ap_scheduler = ap_scheduler

    yield

    ap_scheduler.shutdown(wait=False)


app = FastAPI(
    title="calendar-orchestrator",
    description="Watches a Google Calendar and triggers the meeting bot before each call.",
    lifespan=lifespan,
)


@app.get("/health", summary="Liveness and scheduler status")
async def health() -> dict:
    ap_scheduler: AsyncIOScheduler = app.state.ap_scheduler
    return {
        "status": "ok",
        "scheduler_running": ap_scheduler.running,
        "scheduled_jobs": len(
            [j for j in ap_scheduler.get_jobs() if j.id != _SYNC_JOB_ID]
        ),
    }


@app.get("/jobs", summary="List currently scheduled join jobs")
async def list_jobs() -> dict:
    ap_scheduler: AsyncIOScheduler = app.state.ap_scheduler
    jobs = [
        {"id": job.id, "name": job.name, "run_at": job.next_run_time.isoformat()}
        for job in ap_scheduler.get_jobs()
        if job.id != _SYNC_JOB_ID and job.next_run_time is not None
    ]
    return {"jobs": jobs, "total": len(jobs)}


@app.post("/sync", summary="Force an immediate calendar sync")
async def force_sync() -> dict:
    try:
        count = await run_sync(app.state.calendar, app.state.scheduler)
    except CalendarSyncError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        ) from exc
    return {"upcoming_meetings": count}
