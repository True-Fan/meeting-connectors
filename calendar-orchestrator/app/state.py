"""Durable dedup state: which events have already triggered a bot join.

A flat JSON file rather than a database — this service tracks one small set of strings and
restarts rarely need more than "did we already do this". Persisting it (instead of an
in-memory set) is what prevents a duplicate join if the process restarts between triggering
the bot and the meeting's actual start time.

Keyed on ``f"{event_id}:{start_iso}"`` rather than bare ``event_id``: if an event is deleted
and Google later reuses the id (rare, but observed with some calendar sync edge cases) or if
a recurring instance's start time is what actually changed, the key changes with it, so a
genuinely different meeting occurrence is never mistaken for one already handled.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class TriggeredEventStore:
    """Async-safe, file-backed set of "already joined" event keys."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self._keys: set[str] = self._load()

    def _load(self) -> set[str]:
        if not self._path.exists():
            return set()
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return set(data)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("could not read state file %s, starting empty: %s", self._path, exc)
            return set()

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(sorted(self._keys)), encoding="utf-8")
        tmp.replace(self._path)  # atomic on POSIX and Windows

    @staticmethod
    def key_for(event_id: str, start_iso: str) -> str:
        return f"{event_id}:{start_iso}"

    async def has_triggered(self, key: str) -> bool:
        async with self._lock:
            return key in self._keys

    async def mark_triggered(self, key: str) -> None:
        async with self._lock:
            self._keys.add(key)
            await asyncio.to_thread(self._save)

    async def prune(self, keep_prefixes: set[str]) -> None:
        """Drop keys for events no longer in the lookahead window, so the file doesn't
        grow forever. ``keep_prefixes`` is the set of currently-known event ids; any stored
        key whose event id isn't in it is old enough to forget."""
        async with self._lock:
            before = len(self._keys)
            self._keys = {k for k in self._keys if k.split(":", 1)[0] in keep_prefixes}
            if len(self._keys) != before:
                await asyncio.to_thread(self._save)
