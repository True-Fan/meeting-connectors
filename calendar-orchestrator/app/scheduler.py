"""Turns a list of calendar events into APScheduler jobs, keeping the two in sync.

The reconciliation strategy is deliberately simple: every poll (see ``calendar_service.py``)
produces the full list of currently-valid upcoming events, and ``MeetingScheduler.sync``
diffs that against the scheduler's current jobs. There's no separate "update" or "cancel"
code path —

* a **new** event gets a new job.
* an **updated** event (time changed, etc.) gets its existing job replaced —
  ``add_job(..., replace_existing=True)`` with the event id as the job id handles this for
  free, no diffing of individual fields needed.
* a **cancelled or removed** event's job id no longer appears in the fresh event list, so
  it gets removed.

This trades a little redundant work (re-adding an unchanged job every poll) for a much
simpler and more robust model: whatever Google Calendar says *right now* is authoritative,
full stop.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

from app.bot_client import BotTriggerError, trigger_bot_join
from app.config import Settings
from app.models import CalendarEvent
from app.state import TriggeredEventStore

logger = logging.getLogger(__name__)

# Namespaced so job ids can never collide with the scheduler's own periodic sync job
# (registered separately in main.py as "calendar-sync").
_JOB_PREFIX = "meeting:"


class MeetingScheduler:
    """Owns the APScheduler instance and the join-trigger jobs on it."""

    def __init__(
        self, scheduler: AsyncIOScheduler, state: TriggeredEventStore, settings: Settings
    ) -> None:
        self._scheduler = scheduler
        self._state = state
        self._settings = settings

    async def sync(self, events: list[CalendarEvent]) -> None:
        """Reconcile scheduler jobs against the current set of upcoming events."""
        current_job_ids = set()
        for event in events:
            job_id = self._schedule_event(event)
            if job_id is not None:
                current_job_ids.add(job_id)

        self._remove_stale_jobs(current_job_ids)
        await self._state.prune({e.event_id for e in events})

    def _schedule_event(self, event: CalendarEvent) -> str | None:
        """Add or replace the join job for one event. Returns the job id, or ``None`` if
        the event was skipped (already triggered, or its join moment has passed)."""
        job_id = _JOB_PREFIX + event.event_id
        sched = self._settings.scheduling
        now = datetime.now(UTC)
        run_at = event.start - timedelta(seconds=sched.join_lead_time_s)

        if run_at < now:
            overdue_by = (now - run_at).total_seconds()
            if overdue_by > sched.late_join_grace_s:
                logger.info(
                    "skipping %r (%s): join moment was %.0fs ago, past the %ds grace window",
                    event.summary,
                    event.event_id,
                    overdue_by,
                    sched.late_join_grace_s,
                )
                self._remove_job(job_id)
                return None
            run_at = now  # within grace: join right away instead of dropping the meeting

        self._scheduler.add_job(
            _run_join,
            trigger=DateTrigger(run_date=run_at),
            id=job_id,
            name=f"join:{event.summary}",
            replace_existing=True,
            misfire_grace_time=sched.misfire_grace_s,
            kwargs={"event": event, "state": self._state, "settings": self._settings},
        )
        logger.debug("scheduled %r (%s) to join at %s", event.summary, event.event_id, run_at)
        return job_id

    def _remove_stale_jobs(self, current_job_ids: set[str]) -> None:
        for job in self._scheduler.get_jobs():
            if job.id.startswith(_JOB_PREFIX) and job.id not in current_job_ids:
                logger.info("removing job for meeting no longer on the calendar: %s", job.id)
                self._remove_job(job.id)

    def _remove_job(self, job_id: str) -> None:
        try:
            self._scheduler.remove_job(job_id)
        except JobLookupError:
            pass  # already gone — fine, that was the goal


async def _run_join(event: CalendarEvent, state: TriggeredEventStore, settings: Settings) -> None:
    """The function APScheduler actually fires. Module-level (not a bound method) so it
    stays trivially picklable if a persistent job store is ever added later."""
    key = TriggeredEventStore.key_for(event.event_id, event.start.isoformat())
    if await state.has_triggered(key):
        logger.info("skipping %r (%s): already triggered", event.summary, event.event_id)
        return

    logger.info(
        "triggering bot join for %r (%s) -> meeting %s",
        event.summary,
        event.event_id,
        event.meeting_code,
    )
    try:
        await trigger_bot_join(
            event.meeting_code, settings.bridge, attendees=event.attendees
        )
    except BotTriggerError:
        logger.exception("failed to trigger bot join for %s", event.event_id)
        # Not marked as triggered: bot_client already retried internally, so a further
        # attempt here would just repeat the same failure until the next full sync drops
        # the event from the window. Surfacing the exception in logs is the operator signal.
        return
    await state.mark_triggered(key)
