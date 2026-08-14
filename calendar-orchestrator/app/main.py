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
from app.gmail_poller import GmailPoller
from app.gmail_service import GmailError, GmailService
from app.gmail_state import ProcessedMessageStore
from app.scheduler import MeetingScheduler
from app.state import TriggeredEventStore

logger = logging.getLogger(__name__)

_SYNC_JOB_ID = "calendar-sync"
_GMAIL_JOB_ID = "gmail-poll"
_HOUSEKEEPING_JOB_IDS = frozenset({_SYNC_JOB_ID, _GMAIL_JOB_ID})


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
        # One credential, scoped to whatever is switched on. With instant invites off this
        # asks for exactly the Calendar scope it always did.
        credentials = build_credentials(settings.google, settings.required_scopes())
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
    app.state.gmail_poller = _setup_gmail_poller(credentials, settings, ap_scheduler)

    yield

    ap_scheduler.shutdown(wait=False)


def _setup_gmail_poller(
    credentials, settings: Settings, ap_scheduler: AsyncIOScheduler
) -> GmailPoller | None:
    """Register the Gmail poll job alongside the calendar sync, or return ``None`` if off.

    Failure here is deliberately non-fatal. The calendar path is this service's primary job
    and works entirely without Gmail; taking the whole service down because a Gmail scope
    was missing would trade a missing bonus feature for every scheduled meeting.
    """
    if not settings.gmail.enabled:
        logger.info("instant invites disabled (ORCH_GMAIL__ENABLED=false)")
        return None

    try:
        gmail = GmailService(credentials, settings.gmail)
        store = ProcessedMessageStore(settings.gmail.state_file, settings.gmail.seen_limit)
        poller = GmailPoller(gmail, store, settings)
    except GmailError:
        logger.exception("could not start the Gmail poller; the calendar path is unaffected")
        return None

    ap_scheduler.add_job(
        _run_gmail_poll,
        trigger=IntervalTrigger(seconds=settings.gmail.poll_interval_s),
        id=_GMAIL_JOB_ID,
        kwargs={"poller": poller},
        # The three settings that make a 5-second job safe, and the reason this is a
        # separate add_job rather than a copy of the calendar one:
        #
        # max_instances=1  a cycle that outruns its interval (slow bridge, retrying join)
        #                  must not have the next one start behind it and act on the same
        #                  message twice. GmailPoller holds its own lock too, but this stops
        #                  the queue forming in the first place.
        # coalesce=True    if several runs were missed, run once on resume rather than
        #                  firing a burst of catch-up polls that all see the same inbox.
        # misfire_grace_time  a poll more than one interval late is pointless — the next
        #                  tick is already due and will see the same messages.
        max_instances=1,
        coalesce=True,
        misfire_grace_time=int(max(settings.gmail.poll_interval_s, 1)),
    )
    logger.info(
        "instant invites enabled: polling Gmail every %.1fs (query: %s)",
        settings.gmail.poll_interval_s,
        gmail.build_query(),
    )
    return poller


async def _run_gmail_poll(poller: GmailPoller) -> None:
    """The function APScheduler fires. Swallows everything by design.

    At this cadence a transient Gmail hiccup is unremarkable and the next tick is five
    seconds away — an exception escaping here would be logged by APScheduler as a job
    failure every time, drowning the log in noise for a condition that self-heals. Errors
    are recorded on the poller so ``GET /gmail/status`` can still show them.
    """
    try:
        await poller.poll_once()
    except GmailError as exc:
        poller.last_error = str(exc)
        logger.warning("Gmail poll failed: %s", exc)
    except Exception as exc:  # deliberate catch-all at a scheduler-job boundary
        poller.last_error = str(exc)
        logger.exception("unexpected error in the Gmail poll")


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
            [j for j in ap_scheduler.get_jobs() if j.id not in _HOUSEKEEPING_JOB_IDS]
        ),
        "instant_invites": getattr(app.state, "gmail_poller", None) is not None,
    }


@app.get("/jobs", summary="List currently scheduled join jobs")
async def list_jobs() -> dict:
    ap_scheduler: AsyncIOScheduler = app.state.ap_scheduler
    jobs = [
        {"id": job.id, "name": job.name, "run_at": job.next_run_time.isoformat()}
        for job in ap_scheduler.get_jobs()
        if job.id not in _HOUSEKEEPING_JOB_IDS and job.next_run_time is not None
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


def _require_poller() -> GmailPoller:
    poller: GmailPoller | None = getattr(app.state, "gmail_poller", None)
    if poller is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="instant invites are disabled (set ORCH_GMAIL__ENABLED=true)",
        )
    return poller


@app.get("/gmail/status", summary="Instant-invite poller state")
async def gmail_status() -> dict:
    poller: GmailPoller | None = getattr(app.state, "gmail_poller", None)
    if poller is None:
        return {"enabled": False}
    return {
        "enabled": True,
        "poll_interval_s": app.state.settings.gmail.poll_interval_s,
        "last_poll_at": poller.last_poll_at,
        "last_error": poller.last_error,
        "total_joins": poller.total_joins,
        "processed_message_ids": await poller.processed_count(),
    }


@app.post("/gmail/poll", summary="Force an immediate Gmail poll")
async def force_gmail_poll() -> dict:
    """The Gmail counterpart to ``POST /sync`` — checks the inbox now instead of waiting for
    the next tick. Useful for confirming setup without watching the clock."""
    poller = _require_poller()
    try:
        joined = await poller.poll_once()
    except GmailError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        ) from exc
    return {"joins_triggered": joined}
