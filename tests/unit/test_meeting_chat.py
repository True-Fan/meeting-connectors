"""Meeting chat as a spoken answer: parsing, policy, and the forwarding path.

A participant types a question; the avatar answers out loud. The bridge's part is to observe the
message, decide whether it deserves an answer, and hand it to the agent as text — never to
compose the answer, and never to speak it. Everything here is about that boundary.

The decisions worth testing are the ones where the obvious behaviour is wrong:

* the avatar's own chat message must not be answered — the text-channel form of the feedback
  loop ``EchoGuard`` exists to prevent;
* history rendered when the chat panel opens is not a set of new questions;
* an agent that predates chat must be told nothing rather than sent frames it cannot parse;
* one message must be answered once, however many times Meet re-renders its list.
"""

from __future__ import annotations

import asyncio

import pytest

from src.avatar.client import AvatarClient
from src.connectors.google_meet.meeting.chat import MAX_CHARS, MeetChatSource, parse_chat_message
from src.domain.avatar import (
    AVATAR_CHAT_MIN_VERSION,
    AVATAR_PROTOCOL_VERSION,
    AvatarChatMessage,
    AvatarServerHello,
)
from src.domain.context import FrameContext
from src.domain.health import ComponentState
from src.domain.meeting import ChatMessage
from src.protocols.chat_source import ChatSource
from src.services.media.clock import MediaClock
from tests.fakes.avatar import FakeAvatarTransport
from tests.fakes.chat import ScriptedChatSource


class TestParsing:
    """The page reports on a DOM it does not control, so parsing never raises."""

    def test_a_plain_message_parses(self) -> None:
        message = parse_chat_message({"text": "what is the notice period?", "sender": "Priya"})
        assert message is not None
        assert message.text == "what is the notice period?"
        assert message.sender == "Priya"
        assert message.is_self is False

    def test_surrounding_whitespace_is_stripped(self) -> None:
        message = parse_chat_message({"text": "  hello  ", "sender": "  Dev  "})
        assert message is not None
        assert message.text == "hello"
        assert message.sender == "Dev"

    @pytest.mark.parametrize("body", [{}, {"text": ""}, {"text": "   "}, {"text": None}])
    def test_a_message_with_no_text_is_dropped(self, body: dict) -> None:
        """A chat panel scrape can yield blank strings from a UI element. Waking the agent to
        answer nothing would make the avatar interrupt itself for no reason."""
        assert parse_chat_message(body) is None

    def test_a_missing_sender_is_not_a_reason_to_drop(self) -> None:
        """An unattributed question is still worth answering."""
        message = parse_chat_message({"text": "is this recorded?"})
        assert message is not None
        assert message.sender is None

    def test_an_overlong_message_is_truncated_not_dropped(self) -> None:
        """The text becomes an LLM prompt, so it is capped — but a long genuine question
        should still get an answer."""
        message = parse_chat_message({"text": "x" * (MAX_CHARS + 500)})
        assert message is not None
        assert len(message.text) == MAX_CHARS

    def test_self_authorship_is_carried_through(self) -> None:
        message = parse_chat_message({"text": "hi", "sender": "AI Avatar", "isSelf": True})
        assert message is not None
        assert message.is_self is True

    def test_a_non_dict_payload_is_survivable(self) -> None:
        assert parse_chat_message("not a dict") is None  # type: ignore[arg-type]


class TestMeetChatSource:
    def test_it_satisfies_the_port(self) -> None:
        assert isinstance(MeetChatSource(clock=MediaClock()), ChatSource)

    async def test_offered_messages_are_yielded(self) -> None:
        source = MeetChatSource(clock=MediaClock())
        await source.start()
        assert source.offer({"text": "first"}, message_id="m1")

        iterator = source.messages()
        message = await asyncio.wait_for(anext(iterator), timeout=1)
        assert message.text == "first"

    async def test_the_same_message_id_is_only_accepted_once(self) -> None:
        """Meet re-renders the chat list on almost every DOM mutation. Without identity one
        typed question is forwarded on every scan and answered repeatedly."""
        source = MeetChatSource(clock=MediaClock())
        await source.start()

        assert source.offer({"text": "how long is the process?"}, message_id="m1") is True
        for _ in range(5):
            assert source.offer({"text": "how long is the process?"}, message_id="m1") is False

        assert source.received == 1

    async def test_a_full_queue_drops_the_newest(self) -> None:
        """The opposite of the audio policy, and correct for the same reason it is wrong there:
        a conversation must stay coherent, so an earlier question keeps its place."""
        source = MeetChatSource(clock=MediaClock(), maxsize=2)
        await source.start()
        assert source.offer({"text": "one"}, message_id="a") is True
        assert source.offer({"text": "two"}, message_id="b") is True
        assert source.offer({"text": "three"}, message_id="c") is False

        assert source.dropped == 1
        first = await asyncio.wait_for(anext(source.messages()), timeout=1)
        assert first.text == "one"

    async def test_offer_never_raises(self) -> None:
        """It is called from the bridge's read loop, which is the media channel. An exception
        there stops audio in both directions — a catastrophic price for a bad chat payload."""
        source = MeetChatSource(clock=MediaClock())
        await source.start()
        for body in ({}, {"text": None}, {"text": ""}):
            assert source.offer(body) is False

    def test_health_is_unknown_before_start(self) -> None:
        source = MeetChatSource(clock=MediaClock())
        assert source.health().state is ComponentState.UNKNOWN

    async def test_health_is_healthy_once_started(self) -> None:
        source = MeetChatSource(clock=MediaClock())
        await source.start()
        assert source.health().state is ComponentState.HEALTHY


class TestForwardingPolicy:
    """``AvatarClient.send_chat`` — what reaches the agent, and what is deliberately withheld."""

    async def _client(self, frame_ctx: FrameContext, *, agent_version: str | None = None):
        reply = (
            AvatarServerHello(protocol_version=agent_version)
            if agent_version is not None
            else None
        )
        transport = FakeAvatarTransport(ctx=frame_ctx, reply=reply)
        client = AvatarClient(transport=transport, ctx=frame_ctx)
        await client.start()
        return client, transport

    async def test_a_participant_message_is_forwarded_as_a_chat_frame(
        self, frame_ctx: FrameContext
    ) -> None:
        client, transport = await self._client(frame_ctx)

        assert await client.send_chat(ChatMessage(text="what is the CTC?", sender="Priya"))

        assert len(transport.sent_control) == 1
        frame = AvatarChatMessage.model_validate_json(transport.sent_control[0])
        assert frame.kind == "chat"
        assert frame.text == "what is the CTC?"
        assert frame.sender == "Priya"

    async def test_the_avatars_own_message_is_never_forwarded(
        self, frame_ctx: FrameContext
    ) -> None:
        """Answering your own chat message is the text form of the feedback loop the echo
        guard exists to prevent — and no echo guard runs on this path."""
        client, transport = await self._client(frame_ctx)

        assert await client.send_chat(
            ChatMessage(text="Hello! I am Gunika", sender="AI Avatar", is_self=True)
        ) is False
        assert transport.sent_control == []

    @pytest.mark.parametrize("text", ["", "   ", "\n\t "])
    async def test_blank_text_is_not_forwarded(
        self, text: str, frame_ctx: FrameContext
    ) -> None:
        client, transport = await self._client(frame_ctx)
        assert await client.send_chat(ChatMessage(text=text)) is False
        assert transport.sent_control == []

    async def test_an_agent_predating_chat_is_sent_nothing(
        self, frame_ctx: FrameContext
    ) -> None:
        """Withheld rather than sent-and-ignored: a 1.0 agent cannot tell us it did not
        understand, so the operator is warned instead of left with silent chat."""
        client, transport = await self._client(frame_ctx, agent_version="1.0")

        assert client.supports_chat is False
        assert await client.send_chat(ChatMessage(text="hello?", sender="Dev")) is False
        assert transport.sent_control == []

    async def test_a_current_agent_supports_chat(self, frame_ctx: FrameContext) -> None:
        client, _ = await self._client(frame_ctx)
        assert client.supports_chat is True
        assert AVATAR_PROTOCOL_VERSION >= AVATAR_CHAT_MIN_VERSION

    async def test_an_agent_ahead_on_minor_still_gets_chat(
        self, frame_ctx: FrameContext
    ) -> None:
        """Minor versions are additive both ways; a newer agent must not lose a feature."""
        client, transport = await self._client(frame_ctx, agent_version="1.9")
        assert await client.send_chat(ChatMessage(text="hi", sender="Dev")) is True
        assert len(transport.sent_control) == 1


class TestScriptedSourceSatisfiesThePort:
    def test_the_fake_is_a_chat_source(self) -> None:
        assert isinstance(ScriptedChatSource(), ChatSource)
