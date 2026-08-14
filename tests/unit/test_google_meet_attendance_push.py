"""Pushing attendance to the agent.

The failure these exist for is specific and was observed live: the ledger held the right names,
the HTTP endpoint served them, and the agent still said *"I don't have access to your meeting
details or a list of participants"* — because nothing crossed the avatar socket. So the
assertions here are about **delivery**, and about the two ways delivery is allowed to be
withheld.
"""

from __future__ import annotations

import asyncio
import json

from src.avatar.client import AvatarClient
from src.connectors.google_meet.meeting.attendance import AttendanceLedger
from src.connectors.google_meet.meeting.attendance_announcer import (
    AttendanceAnnouncer,
    signature,
)
from src.connectors.google_meet.meeting.participants import MeetParticipant, MeetRoster
from src.domain.avatar import AvatarProtocolVersion, AvatarServerHello
from src.domain.context import FrameContext
from src.domain.ids import CorrelationId, SessionId
from tests.fakes.avatar import FakeAvatarTransport


def _ctx() -> FrameContext:
    return FrameContext(session_id=SessionId("ses_x"), correlation_id=CorrelationId("cor_x"))


def _roster(*names: str) -> MeetRoster:
    return MeetRoster(
        participants=(
            *(MeetParticipant(page_id=f"p-{n}", display_name=n) for n in names),
            MeetParticipant(page_id="self", display_name="AI Avatar", is_self=True),
        ),
        self_name="AI Avatar",
    )


async def _client(*, version: str | None = None) -> tuple[AvatarClient, FakeAvatarTransport]:
    ctx = _ctx()
    reply = AvatarServerHello(protocol_version=version) if version else None
    transport = FakeAvatarTransport(ctx=ctx, reply=reply)
    client = AvatarClient(transport=transport, ctx=ctx)
    await client.start()
    return client, transport


def _contexts(transport: FakeAvatarTransport) -> list[dict]:
    """Only the meeting-context frames, so a chat frame cannot satisfy these assertions."""
    frames = [json.loads(payload) for payload in transport.sent_control]
    return [f for f in frames if f.get("kind") == "meeting_context"]


class TestTheFrame:
    async def test_the_brief_reaches_the_agent(self) -> None:
        client, transport = await _client()

        assert await client.send_meeting_context("Aarav Sharma is in the meeting.") is True

        frames = _contexts(transport)
        assert len(frames) == 1
        assert frames[0]["text"] == "Aarav Sharma is in the meeting."
        assert frames[0]["topic"] == "attendance"

    async def test_it_is_not_a_chat_frame(self) -> None:
        """The whole reason for a separate kind: chat is spoken aloud, this must not be."""
        client, transport = await _client()

        await client.send_meeting_context("Aarav Sharma is in the meeting.")

        assert [f["kind"] for f in _contexts(transport)] == ["meeting_context"]
        assert not [
            f for f in (json.loads(p) for p in transport.sent_control) if f.get("kind") == "chat"
        ], "attendance must never travel on the channel the avatar speaks"

    async def test_an_agent_that_predates_the_kind_receives_nothing(self) -> None:
        """Withheld rather than downgraded to chat, which the agent would read aloud."""
        client, transport = await _client(version="1.1")

        assert client.supports_meeting_context is False
        assert await client.send_meeting_context("Aarav Sharma is in the meeting.") is False
        assert _contexts(transport) == []

    async def test_a_1_1_agent_still_receives_chat(self) -> None:
        """The bump must not have cost the older feature."""
        client, _ = await _client(version="1.1")

        assert client.supports_chat is True

    async def test_empty_text_is_not_sent(self) -> None:
        client, transport = await _client()

        assert await client.send_meeting_context("   ") is False
        assert _contexts(transport) == []


class TestTheAnnouncer:
    async def test_it_pushes_once_the_roster_is_known(self) -> None:
        client, transport = await _client()
        ledger = AttendanceLedger()
        ledger.observe_roster(_roster("Aarav Sharma"))
        announcer = AttendanceAnnouncer(
            ledger=ledger, avatar=client, interval_s=0.5, settle_s=0.0
        )

        await announcer.start()
        try:
            await asyncio.sleep(0.15)
        finally:
            await announcer.stop()

        frames = _contexts(transport)
        assert len(frames) == 1
        assert "Aarav Sharma" in frames[0]["text"]

    async def test_an_unchanged_roster_is_not_resent(self) -> None:
        """Standing context: repeating it is noise in the agent's context window."""
        client, transport = await _client()
        ledger = AttendanceLedger()
        ledger.observe_roster(_roster("Aarav Sharma"))
        announcer = AttendanceAnnouncer(
            ledger=ledger, avatar=client, interval_s=0.05, settle_s=0.0
        )

        await announcer.start()
        try:
            await asyncio.sleep(0.3)  # many ticks, one roster
        finally:
            await announcer.stop()

        assert len(_contexts(transport)) == 1

    async def test_a_change_is_pushed(self) -> None:
        client, transport = await _client()
        ledger = AttendanceLedger()
        ledger.observe_roster(_roster("Aarav Sharma"))
        announcer = AttendanceAnnouncer(
            ledger=ledger, avatar=client, interval_s=0.05, settle_s=0.0
        )

        await announcer.start()
        try:
            await asyncio.sleep(0.15)
            ledger.observe_roster(_roster("Aarav Sharma", "Priya Menon"))
            await asyncio.sleep(0.15)
        finally:
            await announcer.stop()

        frames = _contexts(transport)
        assert len(frames) == 2
        assert "Priya Menon" in frames[-1]["text"]

    async def test_nothing_is_sent_before_a_roster_is_observed(self) -> None:
        """"Attendance is unknown" is true, useless, and would need correcting a tick later."""
        client, transport = await _client()
        announcer = AttendanceAnnouncer(
            ledger=AttendanceLedger(), avatar=client, interval_s=0.05, settle_s=0.0
        )

        await announcer.start()
        try:
            await asyncio.sleep(0.2)
        finally:
            await announcer.stop()

        assert _contexts(transport) == []

    async def test_a_withheld_push_is_retried_after_the_agent_catches_up(self) -> None:
        """Delivery, not observation, is what marks a brief as sent."""
        client, transport = await _client(version="1.1")
        ledger = AttendanceLedger()
        ledger.observe_roster(_roster("Aarav Sharma"))
        announcer = AttendanceAnnouncer(
            ledger=ledger, avatar=client, interval_s=0.05, settle_s=0.0
        )

        await announcer.start()
        try:
            await asyncio.sleep(0.15)
            assert _contexts(transport) == [], "a 1.1 agent receives nothing"

            # The same agent, now speaking 1.2 — as a reconnect to an upgraded agent would be.
            client._negotiated = AvatarProtocolVersion(major=1, minor=2)
            await asyncio.sleep(0.15)
        finally:
            await announcer.stop()

        assert len(_contexts(transport)) == 1, (
            "the roster never changed, but this agent had not been told"
        )

    async def test_a_send_failure_does_not_kill_the_loop(self) -> None:
        """Losing context must not fail a session that is otherwise carrying audio."""
        client, transport = await _client()
        ledger = AttendanceLedger()
        ledger.observe_roster(_roster("Aarav Sharma"))

        calls = {"n": 0}
        original = transport.send_control

        async def flaky(payload: str) -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("socket went away")
            await original(payload)

        transport.send_control = flaky  # type: ignore[method-assign]
        announcer = AttendanceAnnouncer(
            ledger=ledger, avatar=client, interval_s=0.05, settle_s=0.0
        )

        await announcer.start()
        try:
            await asyncio.sleep(0.3)
        finally:
            await announcer.stop()

        assert calls["n"] >= 2, "the loop kept going after the failure"
        assert len(_contexts(transport)) >= 1

    async def test_stop_is_idempotent(self) -> None:
        client, _ = await _client()
        announcer = AttendanceAnnouncer(ledger=AttendanceLedger(), avatar=client)

        await announcer.start()
        await announcer.stop()
        await announcer.stop()


class TestSignature:
    def test_presence_changes_are_news_and_durations_are_not(self) -> None:
        ledger = AttendanceLedger()
        ledger.observe_roster(_roster("Aarav Sharma"))
        first = signature(ledger.snapshot())

        ledger.observe_roster(_roster("Aarav Sharma"))
        assert signature(ledger.snapshot()) == first, "time passing is not a change"

        ledger.observe_roster(_roster("Aarav Sharma", "Priya Menon"))
        assert signature(ledger.snapshot()) != first


class TestSkippingTheHandshakeChange:
    """``require_negotiation=False`` — the escape hatch for an agent stuck on 1.1.

    Adding attendance to an existing agent otherwise needs two edits, and forgetting the
    handshake one silently disables the feature. This makes the handler the only requirement.
    """

    async def test_a_1_1_agent_is_sent_the_brief_when_negotiation_is_not_required(self) -> None:
        client, transport = await _client(version="1.1")

        sent = await client.send_meeting_context(
            "Aarav Sharma is in the meeting.", require_negotiation=False
        )

        assert sent is True
        assert len(_contexts(transport)) == 1

    async def test_it_is_still_not_a_chat_frame(self) -> None:
        """The escape hatch changes who is sent the frame, never the frame's kind."""
        client, transport = await _client(version="1.1")

        await client.send_meeting_context("Aarav Sharma is here.", require_negotiation=False)

        kinds = {json.loads(p).get("kind") for p in transport.sent_control}
        assert kinds == {"meeting_context"}, "attendance must never become something spoken"

    async def test_the_announcer_honours_it(self) -> None:
        client, transport = await _client(version="1.1")
        ledger = AttendanceLedger()
        ledger.observe_roster(_roster("dev Choudhary"))
        announcer = AttendanceAnnouncer(
            ledger=ledger,
            avatar=client,
            interval_s=0.05,
            settle_s=0.0,
            require_negotiation=False,
        )

        await announcer.start()
        try:
            await asyncio.sleep(0.15)
        finally:
            await announcer.stop()

        frames = _contexts(transport)
        assert len(frames) == 1
        assert "dev Choudhary" in frames[0]["text"]

    async def test_the_default_still_withholds(self) -> None:
        """Off by default: an agent that raises on an unknown kind must not be handed one."""
        client, transport = await _client(version="1.1")

        assert await client.send_meeting_context("Aarav Sharma is here.") is False
        assert _contexts(transport) == []
