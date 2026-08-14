"""Poll-cycle tests: dedup across cycles, the age bound, and failure handling.

Uses a fake Gmail service rather than mocking the API client, so the assertions are about
*what the poller does with what it sees* — repeated identical poll results being the whole
hazard of a polling design.
"""

from __future__ import annotations

import base64
import time

import pytest
from app import gmail_poller as module
from app.config import GmailSettings, Settings
from app.gmail_poller import GmailPoller
from app.gmail_service import GmailError
from app.gmail_state import ProcessedMessageStore


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _invite(message_id: str, code: str = "abc-defg-hij", age_s: float = 0) -> dict:
    return {
        "id": message_id,
        "threadId": f"thread-{message_id}",
        "snippet": "",
        "internalDate": str(_now_ms() - int(age_s * 1000)),
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "From", "value": "Google Meet <meetings-noreply@google.com>"},
                {"name": "Subject", "value": "Happening now: Sync"},
            ],
            "body": {"data": _b64(f"Join https://meet.google.com/{code}")},
        },
    }


class FakeGmail:
    """Stands in for ``GmailService``. ``candidates`` is what each poll sees."""

    def __init__(self, messages: dict[str, dict]):
        self.messages = messages
        self.candidates = list(messages)
        self.list_calls = 0
        self.fetched: list[str] = []
        self.marked_read: list[str] = []
        self.list_error: GmailError | None = None
        self.fetch_error: GmailError | None = None

    def build_query(self) -> str:
        return "(from:meetings-noreply@google.com) newer_than:1d is:unread"

    async def list_invite_candidates(self) -> list[str]:
        self.list_calls += 1
        if self.list_error is not None:
            raise self.list_error
        return list(self.candidates)

    async def get_message(self, message_id: str) -> dict:
        self.fetched.append(message_id)
        if self.fetch_error is not None:
            raise self.fetch_error
        return self.messages[message_id]

    async def mark_read(self, message_id: str) -> None:
        self.marked_read.append(message_id)


def _settings(**gmail_kwargs) -> Settings:
    return Settings(
        google={"auth_mode": "oauth", "oauth_client_secret_file": "unused.json"},
        gmail=GmailSettings(enabled=True, **gmail_kwargs),
    )


@pytest.fixture
def joins(monkeypatch):
    """Capture bridge calls; assume no session is live unless a test says otherwise."""
    triggered: list[str] = []

    async def fake_trigger(meeting_code, bridge_settings, **kwargs):
        triggered.append(meeting_code)

    async def fake_active(meeting_code, bridge_settings):
        return False

    monkeypatch.setattr(module, "trigger_bot_join", fake_trigger)
    monkeypatch.setattr(module, "has_active_session", fake_active)
    return triggered


def _poller(gmail: FakeGmail, tmp_path, settings: Settings | None = None):
    settings = settings or _settings()
    store = ProcessedMessageStore(tmp_path / "processed.json", settings.gmail.seen_limit)
    return GmailPoller(gmail, store, settings), store


async def test_invite_triggers_a_join(tmp_path, joins):
    poller, _ = _poller(FakeGmail({"m1": _invite("m1")}), tmp_path)

    assert await poller.poll_once() == 1
    assert joins == ["abc-defg-hij"]


async def test_repeated_polls_do_not_rejoin(tmp_path, joins):
    """The core polling hazard: the same unread mail comes back every single cycle."""
    gmail = FakeGmail({"m1": _invite("m1")})
    poller, _ = _poller(gmail, tmp_path)

    for _ in range(5):
        await poller.poll_once()

    assert joins == ["abc-defg-hij"]
    assert gmail.list_calls == 5
    assert gmail.fetched == ["m1"]  # fetched once, then skipped by the dedup file


async def test_dedup_survives_a_restart(tmp_path, joins):
    """State is on disk, so a restart between the join and the mail being read is safe."""
    gmail = FakeGmail({"m1": _invite("m1")})
    settings = _settings()
    store = ProcessedMessageStore(tmp_path / "processed.json", settings.gmail.seen_limit)
    await GmailPoller(gmail, store, settings).poll_once()

    reloaded_store = ProcessedMessageStore(tmp_path / "processed.json", settings.gmail.seen_limit)
    restarted = GmailPoller(gmail, reloaded_store, settings)

    assert await restarted.poll_once() == 0
    assert joins == ["abc-defg-hij"]


async def test_old_invites_are_ignored(tmp_path, joins):
    """First-run protection: an unread backlog must not join meetings that already ended."""
    gmail = FakeGmail({"old": _invite("old", age_s=3600)})
    poller, _ = _poller(gmail, tmp_path, _settings(max_invite_age_s=600))

    assert await poller.poll_once() == 0
    assert joins == []


async def test_recent_invite_inside_the_age_window_is_joined(tmp_path, joins):
    gmail = FakeGmail({"m1": _invite("m1", age_s=30)})
    poller, _ = _poller(gmail, tmp_path, _settings(max_invite_age_s=600))

    assert await poller.poll_once() == 1


async def test_non_invite_mail_is_recorded_not_refetched(tmp_path, joins):
    newsletter = _invite("m1")
    newsletter["payload"]["headers"][1]["value"] = "Your recording is ready"
    gmail = FakeGmail({"m1": newsletter})
    poller, _ = _poller(gmail, tmp_path)

    await poller.poll_once()
    await poller.poll_once()

    assert joins == []
    assert gmail.fetched == ["m1"]  # not re-fetched every 5 seconds forever


async def test_two_invites_to_the_same_meeting_join_once(tmp_path, joins):
    """Two people clicking "Add people" produces two emails and one meeting."""
    gmail = FakeGmail({"m1": _invite("m1"), "m2": _invite("m2")})
    poller, _ = _poller(gmail, tmp_path)

    assert await poller.poll_once() == 1
    assert joins == ["abc-defg-hij"]


async def test_join_is_skipped_when_the_bridge_already_has_the_meeting(
    tmp_path, monkeypatch, joins
):
    """A calendar-scheduled join plus a live "Add people" invite is the same meeting twice."""

    async def already_joined(meeting_code, bridge_settings):
        return True

    monkeypatch.setattr(module, "has_active_session", already_joined)
    poller, _ = _poller(FakeGmail({"m1": _invite("m1")}), tmp_path)

    assert await poller.poll_once() == 0
    assert joins == []


async def test_failed_join_is_retried_then_given_up_on(tmp_path, monkeypatch):
    """A bridge outage should not lose the invite on the first try, nor retry forever."""
    attempts: list[str] = []

    async def always_fails(meeting_code, bridge_settings, **kwargs):
        attempts.append(meeting_code)
        raise module.BotTriggerError("bridge down")

    async def not_active(meeting_code, bridge_settings):
        return False

    monkeypatch.setattr(module, "trigger_bot_join", always_fails)
    monkeypatch.setattr(module, "has_active_session", not_active)

    gmail = FakeGmail({"m1": _invite("m1")})
    poller, _ = _poller(gmail, tmp_path, _settings(max_attempts=3))

    for _ in range(6):
        await poller.poll_once()

    assert len(attempts) == 3  # capped, not once and not forever


async def test_recovers_when_the_bridge_comes_back(tmp_path, monkeypatch):
    calls: list[str] = []
    fail = True

    async def flaky(meeting_code, bridge_settings, **kwargs):
        calls.append(meeting_code)
        if fail:
            raise module.BotTriggerError("bridge down")

    async def not_active(meeting_code, bridge_settings):
        return False

    monkeypatch.setattr(module, "trigger_bot_join", flaky)
    monkeypatch.setattr(module, "has_active_session", not_active)

    poller, _ = _poller(FakeGmail({"m1": _invite("m1")}), tmp_path, _settings(max_attempts=5))

    assert await poller.poll_once() == 0
    fail = False
    assert await poller.poll_once() == 1
    assert len(calls) == 2


async def test_fetch_failure_does_not_record_the_message(tmp_path, joins):
    """A transient fetch error must leave the invite retryable, not silently swallowed."""
    gmail = FakeGmail({"m1": _invite("m1")})
    gmail.fetch_error = GmailError("boom", status=500)
    poller, _ = _poller(gmail, tmp_path)

    assert await poller.poll_once() == 0

    gmail.fetch_error = None
    assert await poller.poll_once() == 1


async def test_list_failure_propagates_for_the_job_to_log(tmp_path, joins):
    gmail = FakeGmail({})
    gmail.list_error = GmailError("quota", status=429)
    poller, _ = _poller(gmail, tmp_path)

    with pytest.raises(GmailError):
        await poller.poll_once()


async def test_mark_as_read_is_off_by_default(tmp_path, joins):
    gmail = FakeGmail({"m1": _invite("m1")})
    poller, _ = _poller(gmail, tmp_path)

    await poller.poll_once()

    assert gmail.marked_read == []


async def test_mark_as_read_when_enabled(tmp_path, joins):
    gmail = FakeGmail({"m1": _invite("m1")})
    poller, _ = _poller(gmail, tmp_path, _settings(mark_as_read=True))

    await poller.poll_once()

    assert gmail.marked_read == ["m1"]


async def test_empty_inbox_is_a_single_cheap_call(tmp_path, joins):
    gmail = FakeGmail({})
    poller, _ = _poller(gmail, tmp_path)

    assert await poller.poll_once() == 0
    assert gmail.fetched == []


async def test_concurrent_polls_do_not_double_join(tmp_path, joins):
    """max_instances=1 on the job is belt; this lock is braces."""
    import asyncio

    gmail = FakeGmail({"m1": _invite("m1")})
    poller, _ = _poller(gmail, tmp_path)

    await asyncio.gather(poller.poll_once(), poller.poll_once(), poller.poll_once())

    assert joins == ["abc-defg-hij"]
