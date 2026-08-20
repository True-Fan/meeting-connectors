"""Thin async wrapper around the (synchronous) Gmail API client.

Mirrors ``calendar_service.py`` deliberately, down to the ``asyncio.to_thread`` dispatch:
``googleapiclient`` is blocking, and at a 5-second cadence a synchronous call on the event
loop would stall everything else the service is doing — including the calendar sync it
shares that loop with.

**``messages.list`` rather than ``history.list``.** Both can answer "what is new", but they
are different tools. ``history.list`` is incremental and needs a durable ``historyId``
watermark that must be seeded, advanced only on success, and resynced when it ages out of
the roughly one week of history Gmail retains — machinery that earns its place when you are
reacting to a push notification and want the exact delta. Polling has no such constraint: a
query for "unread mail from this sender" is stateless, describes the wanted set directly,
and cannot desynchronise. The dedup file, not a watermark, is what stops repeats.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from google.auth.exceptions import RefreshError
from googleapiclient.discovery import Resource, build
from googleapiclient.errors import HttpError

from app.config import GmailSettings

logger = logging.getLogger(__name__)


class GmailError(RuntimeError):
    """A Gmail API call failed. Callers log and retry on the next poll rather than crash."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class GmailService:
    """Everything the poller needs from Gmail: find candidate mail, read it, optionally
    mark it read."""

    def __init__(self, credentials: Any, settings: GmailSettings) -> None:
        self._settings = settings
        self._service: Resource = build(
            "gmail", "v1", credentials=credentials, cache_discovery=False
        )

    def build_query(self) -> str:
        """The Gmail search query for one poll.

        Narrowed as far as the search syntax allows, so the common case — no new invite —
        costs a single request that returns an empty list, and so the expensive part
        (fetching a message body) only ever happens for plausible candidates.

        ``newer_than`` is a coarse bound because Gmail's search granularity stops at whole
        days; ``max_invite_age_s`` re-checks precisely against each message's
        ``internalDate`` once fetched. Without the coarse bound, switching the feature on
        against a mailbox with an old unread backlog would page through all of it.
        """
        parts = [*self._audience_clause(), "newer_than:1d"]
        if self._settings.unread_only:
            parts.append("is:unread")
        return " ".join(parts)

    def _audience_clause(self) -> list[str]:
        """How the query narrows to mail that could plausibly be an invite.

        Two independent ways a message qualifies, and the query has to admit **both** or the
        poll never retrieves what the parser would have accepted:

        * it is *from* an allow-listed sender — ``from:`` handles an exact address and a
          ``@domain`` entry alike, since Gmail treats ``from:@example.com`` as the domain
          match we want, so both forms pass through unchanged;
        * or its *subject* is one of ``any_sender_subject_markers``, which is how an
          in-meeting Zoom invite is recognised at all. Those come from the host's own
          mailbox, so no ``from:`` clause could ever match them — omitting this is exactly
          the bug where the feature is implemented, the parser is correct, and the mail is
          never fetched to be parsed.

        **A ``*`` entry has no ``from:`` equivalent**, so it drops the sender clause and
        leaves the subject one. Dropping both would return every unread message of the last
        day — and with ``max_results`` capping each poll at ten, a busy inbox would push a
        real invite out of the window and the bot would silently stop turning up.

        Losing precision here is safe and losing recall is not: this is a performance filter,
        and ``parse_invite`` re-checks everything that matters.
        """
        clauses: list[str] = []

        allowed = tuple(s.strip() for s in self._settings.allowed_senders if s.strip())
        if any(entry == "*" for entry in allowed):
            wide = self._quoted_subjects(
                (*self._settings.subject_markers, *self._settings.any_sender_subject_markers)
            )
            wide.extend(self._calendar_terms())
            wide.extend(self._body_terms())
            return [f"({' OR '.join(wide)})"] if wide else []
        clauses.extend(f"from:{entry}" for entry in allowed)
        clauses.extend(self._quoted_subjects(self._settings.any_sender_subject_markers))
        clauses.extend(self._calendar_terms())
        clauses.extend(self._body_terms())
        return [f"({' OR '.join(clauses)})"] if clauses else []

    def _calendar_terms(self) -> list[str]:
        """How a calendar invitation becomes retrievable.

        It matches no ``from:`` (the organiser is arbitrary) and no ``subject:`` (the subject
        is the event's title), so without this the message is never *fetched* — and the
        parser, which would have accepted it, never sees it. ``filename:ics`` is the one
        search term that describes the attachment Google puts on every invitation.
        """
        return ["filename:ics"] if self._settings.accept_calendar_invitations else []

    def _body_terms(self) -> list[str]:
        """How a Zoom invitation *pasted into a message* becomes retrievable.

        The body route accepts an invitation on its own text, from any sender and under any
        subject — so, exactly like a calendar invitation, nothing about the envelope can
        select it and the message would otherwise never be fetched. Gmail cannot express the
        block signature the parser applies, so the query casts one step wider: mail
        mentioning ``zoom.us`` at all.

        That is deliberately imprecise and safely so. Recall is what the query must not lose;
        precision is ``has_zoom_invite_block``'s job, and it runs on every candidate anyway.
        The result set stays small because ``newer_than:1d`` and ``is:unread`` still apply.
        """
        return ['"zoom.us"'] if self._settings.accept_zoom_invite_bodies else []

    @staticmethod
    def _quoted_subjects(markers: tuple[str, ...]) -> list[str]:
        """``subject:"…"`` terms, skipping anything that would break the query's quoting."""
        return [
            f'subject:"{marker.strip()}"'
            for marker in markers
            if marker.strip() and '"' not in marker
        ]

    async def list_invite_candidates(self) -> list[str]:
        """Message ids that might be invites, newest first."""
        query = self.build_query()
        response = await self._call(
            lambda: self._service.users()
            .messages()
            .list(userId="me", q=query, maxResults=self._settings.max_results)
            .execute()
        )
        return [message["id"] for message in response.get("messages", [])]

    async def get_message(self, message_id: str) -> dict:
        """Fetch a full message, body included.

        ``format="full"`` rather than ``metadata``: the Meet link lives in the message body
        and nowhere else, so metadata alone cannot answer the only question being asked.
        """
        return await self._call(
            lambda: self._service.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute()
        )

    async def mark_read(self, message_id: str) -> None:
        """Remove the UNREAD label. Only called when ``mark_as_read`` is configured.

        Never raises: dedup lives in the local state file, so failing to mark a message read
        is cosmetic. Letting it propagate would turn a mailbox-permission problem into a
        failed join for a meeting the bot is already in.
        """
        try:
            await self._call(
                lambda: self._service.users()
                .messages()
                .modify(userId="me", id=message_id, body={"removeLabelIds": ["UNREAD"]})
                .execute()
            )
        except GmailError as exc:
            logger.warning("could not mark message %s read: %s", message_id, exc)

    async def _call(self, request: Any) -> dict:
        """Run a blocking Gmail call off the event loop and normalise its failures."""
        try:
            return await asyncio.to_thread(request)
        except RefreshError as exc:
            raise GmailError(f"credential refresh failed: {exc}") from exc
        except HttpError as exc:
            status = getattr(exc.resp, "status", None)
            raise GmailError(f"Gmail API error {status}: {exc}", status=status) from exc
