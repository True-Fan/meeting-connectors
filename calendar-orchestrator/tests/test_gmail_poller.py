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


async def test_a_duplicate_invite_is_finished_with_on_the_first_poll(
    tmp_path, monkeypatch, joins
):
    """**A skipped-as-duplicate invite is handled, not failed**, and conflating the two left
    already-answered mail sitting unread in the inbox.

    ``_join`` used to return a bare ``False`` here, which is the same value a bridge outage
    returns — and the caller retries a failure. So the message went unrecorded, was re-fetched
    on every poll at 5 quota units a time, and stayed **unread** until ``max_attempts`` ran
    out, all while the bot was already in the meeting. No retry could ever have changed the
    answer: the meeting is being attended, which is exactly why the join was skipped.
    """

    async def already_joined(meeting_code, bridge_settings):
        return True

    monkeypatch.setattr(module, "has_active_session", already_joined)
    gmail = FakeGmail({"m1": _invite("m1")})
    poller, store = _poller(
        gmail, tmp_path, _settings(mark_as_read=True, max_attempts=3)
    )

    for _ in range(3):
        await poller.poll_once()

    assert gmail.marked_read == ["m1"], "handled mail left unread in the inbox"
    assert gmail.fetched == ["m1"], "re-fetched a message there was nothing more to do about"
    assert await store.count() == 1


async def test_a_second_invite_to_a_meeting_this_process_just_joined_is_finished_with(
    tmp_path, monkeypatch, joins
):
    """The same fix on the other duplicate path — the in-process TTL rather than the bridge.

    Two people clicking "Add people" is two emails and one meeting, and the second email is
    fully dealt with the moment the first one joined.
    """
    gmail = FakeGmail({"m1": _invite("m1"), "m2": _invite("m2")})
    poller, store = _poller(gmail, tmp_path, _settings(mark_as_read=True))

    assert await poller.poll_once() == 1
    assert joins == ["abc-defg-hij"]
    # Both: the one that joined, and the duplicate that had nothing left to do.
    assert sorted(gmail.marked_read) == ["m1", "m2"]
    assert await store.count() == 2


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
    assert gmail.fetched == ["m1", "m1", "m1"], "a real failure must still be retried"


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


# --------------------------------------------------------------------------- #
# Every message the poller finishes with is marked read — on every platform
# --------------------------------------------------------------------------- #

# A realistic invite per platform: the envelope each one actually arrives with, not one
# envelope reused three times.
#
# **The senders differ because the platforms differ in what can be trusted, and getting this
# wrong is how a fixture passes for the wrong reason.** Google Meet has *no body route* — a
# Meet link from an arbitrary mailbox is refused, and rightly, because a real "Add people"
# mail comes from `meetings-noreply@google.com` and there is nothing invariant about the body
# to key on. Zoom and Teams do have one, and for Teams it is the only route there is, so for
# those two an arbitrary sender under an arbitrary subject is the realistic case rather than
# the awkward one.
_PLATFORM_INVITES = {
    "google_meet": (
        '"Google Meet" <meetings-noreply@google.com>',
        "Happening now: Sync",
        "Join https://meet.google.com/veg-fkxv-rhg",
    ),
    "zoom_web": (
        '"Someone" <someone@example.net>',
        "Weekly sync",
        "Join Zoom Meeting\nhttps://us05web.zoom.us/j/83843212151\n"
        "Meeting ID: 838 4321 2151\nPasscode: 139601",
    ),
    "teams_web": (
        '"Someone" <someone@example.net>',
        "Weekly sync",
        "Join the meeting now\nhttps://teams.live.com/meet/9339756425487"
        "?p=71cQWhQJ5X8fxHSmVy\nMeeting ID: 933 975 642 5487\nPasscode: 71cQWhQJ5X8fxHSmVy",
    ),
}
PLATFORMS = sorted(_PLATFORM_INVITES)


def _platform_invite(message_id: str, platform: str, age_s: float = 0) -> dict:
    """A live invite for ``platform``, in the envelope that platform really uses."""
    sender, subject, body = _PLATFORM_INVITES[platform]
    return {
        "id": message_id,
        "threadId": f"thread-{message_id}",
        "snippet": "",
        "internalDate": str(_now_ms() - int(age_s * 1000)),
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "From", "value": sender},
                {"name": "Subject", "value": subject},
            ],
            "body": {"data": _b64(body)},
        },
    }


@pytest.mark.parametrize("platform", PLATFORMS)
async def test_the_fixtures_are_accepted_as_invites(platform, tmp_path, joins):
    """**A guard on the fixtures, not on the poller.**

    Every test below asserts that a *handled* message was marked read — and a message rejected
    as "not an invite" is also handled, and also marked read. So a fixture that silently stopped
    parsing would leave the whole class passing while testing nothing about joining.

    This is not hypothetical: the first version of these fixtures used one arbitrary sender for
    all three platforms, which Google Meet correctly refuses, and the mark-read assertions
    passed anyway.

    Parametrised rather than looped, so each platform gets its own ``tmp_path`` and therefore
    its own dedup store — sharing one made the second platform's message look already-processed,
    which is the same failure wearing a different hat.
    """
    gmail = FakeGmail({"m1": _platform_invite("m1", platform)})
    poller, _ = _poller(gmail, tmp_path)

    assert await poller.poll_once() == 1, f"{platform} fixture no longer parses"


class TestEveryHandledMessageIsMarkedRead:
    """**The guarantee, asserted per platform rather than argued from the code.**

    ``mark_as_read`` lives in one place (``_finish``) and nothing about it is platform-aware,
    so it is tempting to test it once and reason that the rest follows. That reasoning is
    exactly what let the duplicate-invite bug through: the *decision to call* ``_finish`` is
    made on an outcome, and one outcome was being misread — so "marking works" was true and
    "handled mail gets marked" was not.

    So this walks every way handling can end, on every platform. The one exception is stated
    at the bottom, and it is an exception because the mail was never read in the first place.
    """

    @pytest.mark.parametrize("platform", PLATFORMS)
    async def test_a_joined_invite_is_marked_read(self, platform, tmp_path, joins):
        gmail = FakeGmail({"m1": _platform_invite("m1", platform)})
        poller, _ = _poller(gmail, tmp_path, _settings(mark_as_read=True))

        assert await poller.poll_once() == 1
        assert gmail.marked_read == ["m1"]

    @pytest.mark.parametrize("platform", PLATFORMS)
    async def test_a_duplicate_invite_is_marked_read_on_the_first_poll(
        self, platform, tmp_path, monkeypatch, joins
    ):
        """The regression. Retrying could never have turned this into a join."""

        async def already_joined(meeting_code, bridge_settings):
            return True

        monkeypatch.setattr(module, "has_active_session", already_joined)
        gmail = FakeGmail({"m1": _platform_invite("m1", platform)})
        poller, _ = _poller(gmail, tmp_path, _settings(mark_as_read=True))

        assert await poller.poll_once() == 0
        assert gmail.marked_read == ["m1"]
        assert gmail.fetched == ["m1"]

    @pytest.mark.parametrize("platform", PLATFORMS)
    async def test_an_invite_given_up_on_is_marked_read(
        self, platform, tmp_path, monkeypatch
    ):
        """A bridge outage retries ``max_attempts`` times and then stops — and when it stops,
        the mail is finished with. Otherwise it sits unread and is re-fetched until it ages
        out of the query a day later."""

        async def always_fails(meeting_code, bridge_settings, **kwargs):
            raise module.BotTriggerError("bridge down")

        async def not_active(meeting_code, bridge_settings):
            return False

        monkeypatch.setattr(module, "trigger_bot_join", always_fails)
        monkeypatch.setattr(module, "has_active_session", not_active)
        gmail = FakeGmail({"m1": _platform_invite("m1", platform)})
        poller, _ = _poller(
            gmail, tmp_path, _settings(mark_as_read=True, max_attempts=3)
        )

        for _ in range(5):
            await poller.poll_once()

        assert gmail.marked_read == ["m1"]
        assert gmail.fetched == ["m1"] * 3, "retried the capped number of times"

    @pytest.mark.parametrize("platform", PLATFORMS)
    async def test_an_invite_too_old_to_join_is_marked_read(
        self, platform, tmp_path, joins
    ):
        gmail = FakeGmail({"m1": _platform_invite("m1", platform, age_s=99_999)})
        poller, _ = _poller(gmail, tmp_path, _settings(mark_as_read=True))

        assert await poller.poll_once() == 0
        assert gmail.marked_read == ["m1"]

    async def test_mail_that_is_not_an_invite_is_marked_read(self, tmp_path, joins):
        """Otherwise a newsletter is re-fetched every poll for as long as it sits unread."""
        newsletter = _invite("m1")
        newsletter["payload"]["headers"][1]["value"] = "Your recording is ready"
        newsletter["payload"]["body"]["data"] = _b64("Nothing joinable in here.")
        gmail = FakeGmail({"m1": newsletter})
        poller, _ = _poller(gmail, tmp_path, _settings(mark_as_read=True))

        await poller.poll_once()

        assert gmail.marked_read == ["m1"]

    async def test_a_message_that_could_not_be_fetched_is_not_marked_read(
        self, tmp_path, joins
    ):
        """**The one deliberate exception, and the reason it is one.**

        Here the Gmail API call to *retrieve* the message failed, so nothing was ever read and
        there is nothing to have handled. Marking it read would hide a live invite behind a
        transient 5xx; leaving it unread keeps it retryable and visible to a human. It stops
        being retried on its own when it falls out of the ``newer_than:1d`` query.
        """
        gmail = FakeGmail({"m1": _invite("m1")})
        gmail.fetch_error = GmailError("boom", status=500)
        poller, _ = _poller(gmail, tmp_path, _settings(mark_as_read=True))

        await poller.poll_once()

        assert gmail.marked_read == []

        gmail.fetch_error = None
        assert await poller.poll_once() == 1
        assert gmail.marked_read == ["m1"], "marked once the fetch finally succeeded"
