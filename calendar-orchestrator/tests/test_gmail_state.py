"""Dedup-store tests.

This file is what stands between one invite email and the bot joining the same meeting
twelve times a minute, so its persistence and bounding behaviour are worth pinning down.
"""

from __future__ import annotations

import json

from app.config import GmailSettings
from app.gmail_service import GmailService
from app.gmail_state import ProcessedMessageStore


async def test_processed_ids_round_trip_through_the_file(tmp_path):
    path = tmp_path / "processed.json"
    store = ProcessedMessageStore(path)
    await store.mark_processed("a")
    await store.mark_processed("b")

    reloaded = ProcessedMessageStore(path)

    assert await reloaded.filter_unseen(["a", "b", "c"]) == ["c"]


async def test_filter_unseen_collapses_duplicates_within_a_batch(tmp_path):
    store = ProcessedMessageStore(tmp_path / "processed.json")

    assert await store.filter_unseen(["a", "a", "b"]) == ["a", "b"]


async def test_window_is_bounded(tmp_path):
    store = ProcessedMessageStore(tmp_path / "processed.json", limit=50)

    for i in range(120):
        await store.mark_processed(str(i))

    assert await store.count() == 50
    assert await store.filter_unseen(["119"]) == []  # newest retained
    assert await store.filter_unseen(["0"]) == ["0"]  # oldest evicted


async def test_corrupt_state_file_starts_empty_rather_than_crashing(tmp_path):
    path = tmp_path / "processed.json"
    path.write_text("{not json", encoding="utf-8")

    store = ProcessedMessageStore(path)

    assert await store.count() == 0


async def test_a_bare_list_file_is_still_readable(tmp_path):
    """Tolerated so the on-disk shape can evolve without stranding an existing file."""
    path = tmp_path / "processed.json"
    path.write_text(json.dumps(["a", "b"]), encoding="utf-8")

    store = ProcessedMessageStore(path)

    assert await store.filter_unseen(["a", "c"]) == ["c"]


def test_query_targets_unread_mail_from_the_allowed_sender():
    """The query is the first filter; ``invite_parser`` re-checks the sender as the real one."""
    service = GmailService.__new__(GmailService)  # no API client needed for query building
    service._settings = GmailSettings(enabled=True)

    query = service.build_query()

    assert "from:meetings-noreply@google.com" in query
    assert "is:unread" in query
    assert "newer_than:1d" in query


def test_query_drops_the_unread_filter_when_configured():
    service = GmailService.__new__(GmailService)
    service._settings = GmailSettings(enabled=True, unread_only=False)

    assert "is:unread" not in service.build_query()


def test_query_ors_multiple_senders():
    service = GmailService.__new__(GmailService)
    service._settings = GmailSettings(
        enabled=True, allowed_senders=("a@example.com", "b@example.com")
    )

    query = service.build_query()

    # Substring-per-term rather than the whole parenthesised group: the group now also
    # carries a ``subject:`` term for the in-meeting invite, which has no ``from:`` that
    # could ever match it (``GmailSettings.any_sender_subject_markers``). What this test is
    # actually about — that several senders are OR-ed rather than AND-ed — is unchanged.
    assert "from:a@example.com OR from:b@example.com" in query
