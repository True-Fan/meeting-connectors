"""One poll cycle: inbox -> filter -> dedupe -> trigger the bridge.

    every poll_interval_s (default 5s)
        -> messages.list  (unread, from the invite sender, last day)
        -> drop ids already in the dedup file
        -> fetch each remaining message
        -> drop anything older than max_invite_age_s
        -> parse: sender, subject markers, Meet code
        -> skip meetings already joined
        -> POST /sessions on the bridge
        -> record the message id as processed

Deliberately the same shape as ``scheduler.py``'s calendar reconciliation: whatever Google
says *right now* is authoritative, and the durable state file exists only to answer "did I
already act on this". There is no incremental cursor to keep in sync.

Two things hold it together at a 5-second cadence:

* **Cycles never overlap.** One lock here, plus ``max_instances=1`` on the APScheduler job
  in ``main.py``. A cycle that outruns its interval — a slow bridge, a retrying join — must
  not have the next one start behind it and act on the same message twice.
* **A message is recorded as processed only after its outcome is decided.** A crash
  mid-join replays it on the next cycle rather than losing it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from enum import StrEnum

from app.bot_client import BotTriggerError, has_active_session, trigger_bot_join
from app.config import Settings
from app.gmail_service import GmailError, GmailService
from app.gmail_state import ProcessedMessageStore
from app.invite_parser import InstantInvite, parse_invite
from app.meeting_link import join_url_for

logger = logging.getLogger(__name__)


class JoinOutcome(StrEnum):
    """What became of one invite, and therefore whether the message is finished with.

    **A bool could not answer that question, and the bug it hid is why this type exists.**
    ``_join`` used to return "did I trigger a join", so every non-join looked identical to the
    caller — and the caller treats a non-join as *retry me*. But two of the three non-joins are
    terminal: if the bridge is already in the meeting, or this process just sent it there, then
    no number of retries will ever turn that into a join.

    So a duplicate invite was retried ``max_attempts`` times before being recorded, which is
    the observable symptom: the mail sits **unread** in the inbox and is re-fetched on every
    poll for a minute and a half, at 5 quota units a time, after the bot is already in the
    meeting. Only ``FAILED`` is worth another attempt.
    """

    JOINED = "joined"
    """The bridge accepted the join. Finish the message."""

    ALREADY_HANDLED = "already_handled"
    """The meeting is already being attended — a second invite for it, or a second person
    clicking *Add people*. Nothing left to do, so finish the message: this is a success from
    the mailbox's point of view, not a failure to retry."""

    FAILED = "failed"
    """The bridge could not be reached or rejected the join, after ``bot_client``'s own
    retries. The only outcome worth another poll, bounded by ``max_attempts``."""


class GmailPoller:
    """Owns one poll cycle and the decision to trigger a join."""

    def __init__(
        self, gmail: GmailService, store: ProcessedMessageStore, settings: Settings
    ) -> None:
        self._gmail = gmail
        self._store = store
        self._settings = settings
        self._lock = asyncio.Lock()
        self._recent_joins: dict[str, float] = {}
        self._attempts: dict[str, int] = {}
        self.last_poll_at: float | None = None
        self.last_error: str | None = None
        self.total_joins = 0

    async def processed_count(self) -> int:
        """How many message ids the dedup window is holding, for ``GET /gmail/status``."""
        return await self._store.count()

    async def poll_once(self) -> int:
        """Run one cycle. Returns the number of joins triggered.

        Raises ``GmailError`` if the mailbox could not be listed; the caller logs and waits
        for the next tick rather than letting the job die.
        """
        async with self._lock:
            candidates = await self._gmail.list_invite_candidates()
            self.last_poll_at = time.time()
            self.last_error = None

            fresh = await self._store.filter_unseen(candidates)
            if not fresh:
                return 0

            logger.debug("poll: %d candidate(s), %d not yet processed", len(candidates), len(fresh))
            joined = 0
            for message_id in fresh:
                if await self._handle_message(message_id):
                    joined += 1
            self.total_joins += joined
            return joined

    async def _handle_message(self, message_id: str) -> bool:
        """Fetch, vet, and act on one message. Returns True if a join was triggered."""
        try:
            message = await self._gmail.get_message(message_id)
        except GmailError as exc:
            # Not recorded as processed: a transient fetch failure should be retried, and
            # the attempt counter below bounds how often.
            logger.warning("could not fetch message %s: %s", message_id, exc)
            self._count_attempt(message_id)
            return False

        invite = parse_invite(message, self._settings.gmail)
        if invite is None:
            # Not an invite and never will be — record it so it is not re-fetched every
            # five seconds for as long as it sits unread in the inbox.
            await self._finish(message_id)
            return False

        if self._is_stale(invite):
            logger.info(
                "ignoring invite %s for %s: %.0fs old, past the %ds window",
                message_id,
                invite.meeting_code,
                self._age_s(invite),
                self._settings.gmail.max_invite_age_s,
            )
            await self._finish(message_id)
            return False

        outcome = await self._join(invite)
        # **Anything but a failure is finished with**, which is the distinction a bool could
        # not carry. A duplicate invite is terminal — the meeting is already being attended, so
        # retrying can only re-fetch the same message and reach the same answer — and leaving it
        # unrecorded is what left handled mail sitting unread in the inbox for max_attempts
        # polls. See ``JoinOutcome``.
        if (
            outcome is not JoinOutcome.FAILED
            or self._count_attempt(message_id) >= self._settings.gmail.max_attempts
        ):
            await self._finish(message_id)
        return outcome is JoinOutcome.JOINED

    def _age_s(self, invite: InstantInvite) -> float:
        return time.time() - invite.internal_date_ms / 1000

    def _is_stale(self, invite: InstantInvite) -> bool:
        """Whether this invite is too old to still be worth joining.

        Load-bearing the first time the feature is switched on: without it, every old unread
        Meet invite still in the mailbox would be fired at the bridge at once, joining
        meetings that ended days ago. ``internal_date_ms`` of 0 means Gmail gave us no
        timestamp, which is treated as "not stale" — better to join a meeting that has
        finished than to silently drop a live one over a missing field.
        """
        if invite.internal_date_ms <= 0:
            return False
        return self._age_s(invite) > self._settings.gmail.max_invite_age_s

    async def _finish(self, message_id: str) -> None:
        """Record a message as handled and forget its attempt counter."""
        self._attempts.pop(message_id, None)
        await self._store.mark_processed(message_id)
        if self._settings.gmail.mark_as_read:
            await self._gmail.mark_read(message_id)

    def _count_attempt(self, message_id: str) -> int:
        count = self._attempts.get(message_id, 0) + 1
        self._attempts[message_id] = count
        return count

    async def _join(self, invite: InstantInvite) -> JoinOutcome:
        """Put the bot in the invited meeting, unless it is already there.

        Returns *why* nothing happened when nothing happened, because the caller has to tell a
        duplicate apart from a failure to decide whether the message is finished with — see
        ``JoinOutcome``.
        """
        code = invite.meeting_code

        if self._recently_joined(code):
            logger.info("ignoring invite %s: just joined %s", invite.message_id, code)
            return JoinOutcome.ALREADY_HANDLED

        if await has_active_session(code, self._settings.bridge):
            logger.info(
                "ignoring invite %s: bridge already has a session in %s", invite.message_id, code
            )
            self._mark_joined(code)
            return JoinOutcome.ALREADY_HANDLED

        logger.info(
            "instant invite from %s (%r) -> joining %s meeting %s",
            invite.sender,
            invite.subject,
            invite.platform,
            code,
        )
        try:
            await trigger_bot_join(
                code,
                self._settings.bridge,
                platform=invite.platform,
                passcode=invite.passcode,
                meeting_url=join_url_for(invite.platform, invite.meeting_url),
            )
        except BotTriggerError as exc:
            # bot_client has already exhausted its own retries. Left unrecorded so the next
            # cycle tries again, bounded by max_attempts and by the invite ageing out.
            logger.warning("failed to trigger bot join for invite %s: %s", invite.message_id, exc)
            self.last_error = str(exc)
            return JoinOutcome.FAILED

        self._mark_joined(code)
        return JoinOutcome.JOINED

    def _recently_joined(self, meeting_code: str) -> bool:
        """Short in-process guard covering the gap between a join being accepted and the
        session appearing in the bridge's session list — a window ``has_active_session``
        cannot see. Two people clicking "Add people" is two emails and one meeting."""
        ttl = self._settings.gmail.join_dedupe_ttl_s
        if ttl <= 0:
            return False
        now = time.monotonic()
        self._recent_joins = {c: t for c, t in self._recent_joins.items() if now - t < ttl}
        return meeting_code in self._recent_joins

    def _mark_joined(self, meeting_code: str) -> None:
        self._recent_joins[meeting_code] = time.monotonic()
