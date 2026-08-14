"""Durable dedup state: which Gmail message ids have already been handled.

The same shape and reasoning as ``state.py`` — a flat JSON file for one small set of
strings, written rarely — but a separate file and class because the two track different
things and must not be able to corrupt each other.

**This is what makes polling safe.** The poll query is stateless and deliberately so: it
asks "unread invite mail from the last day", which returns the *same* message on every cycle
until something changes. At a 5-second cadence that is twelve identical answers a minute, so
without a durable record of what has been acted on, one invite would put the bot in the same
meeting over and over. Durable rather than in-memory because a restart between the join and
the message ageing out would otherwise replay it.

Bounded to the most recent ``limit`` ids: this is a dedup window, not an archive. An evicted
id can never come back as a live invite, because the poller independently ignores anything
older than ``max_invite_age_s``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from pathlib import Path

logger = logging.getLogger(__name__)


class ProcessedMessageStore:
    """Async-safe, file-backed FIFO of already-handled Gmail message ids."""

    def __init__(self, path: Path, limit: int = 500) -> None:
        self._path = path
        self._limit = limit
        self._lock = asyncio.Lock()
        self._seen: deque[str] = deque(maxlen=limit)
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("could not read %s, starting empty: %s", self._path, exc)
            return
        # Accept both a bare list and the object form, so the file stays readable if it ever
        # grows a sibling field.
        ids = data.get("processed_message_ids", []) if isinstance(data, dict) else data
        self._seen = deque((str(i) for i in ids), maxlen=self._limit)

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"processed_message_ids": list(self._seen)}
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self._path)  # atomic on POSIX and Windows

    async def filter_unseen(self, message_ids: list[str]) -> list[str]:
        """Return the ids not yet handled, order preserved and duplicates collapsed."""
        async with self._lock:
            known = set(self._seen)
            fresh: list[str] = []
            for message_id in message_ids:
                if message_id not in known:
                    known.add(message_id)
                    fresh.append(message_id)
            return fresh

    async def mark_processed(self, message_id: str) -> None:
        async with self._lock:
            self._seen.append(str(message_id))
            await asyncio.to_thread(self._save)

    async def count(self) -> int:
        async with self._lock:
            return len(self._seen)
