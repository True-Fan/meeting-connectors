"""A raised hand as a yielded floor: detection policy, and the frame it becomes.

Somebody puts their hand up; the avatar stops talking and hands over. The bridge's part is to
notice the hand, decide whether it is a request worth acting on, and tell the agent — never to
decide what the avatar says about it.

The decisions worth testing are the ones where the obvious behaviour is wrong:

* a hand is a *state* in the DOM, so the same raised hand must not interrupt the avatar on
  every re-render;
* hands already up when the avatar joins are not interrupting anything — it had not spoken;
* the avatar's own hand must never interrupt the avatar, for the reason its own chat message
  must not be answered;
* an agent that predates barge-in should still hear about the hand, because a late reply beats
  no reply — which is the one place this deliberately differs from chat's withhold-and-warn.
"""

from __future__ import annotations

import asyncio

import pytest

from src.avatar.client import AvatarClient
from src.connectors.google_meet.meeting.hand_raise import (
    ANONYMOUS,
    DEFAULT_PROMPT,
    MeetHandRaiseSource,
    render_prompt,
)
from src.domain.avatar import (
    AVATAR_CHAT_MIN_VERSION,
    AVATAR_PROTOCOL_VERSION,
    AvatarChatMessage,
    AvatarServerHello,
)
from src.domain.context import FrameContext
from src.domain.meeting import ChatMessage, HandRaise
from src.protocols.hand_raise_source import HandRaiseSource
from src.services.media.clock import MediaClock
from tests.fakes.avatar import FakeAvatarTransport


class TestPromptRendering:
    """The wording is configuration, so a bad template must cost the wording, not the feature."""

    def test_the_name_is_substituted(self) -> None:
        assert "Priya" in render_prompt(DEFAULT_PROMPT, "Priya")

    def test_an_unattributed_hand_still_names_somebody(self) -> None:
        """Meet renders indicators it does not always attribute, and "raised their hand" with
        a gap where the name goes reads as a bug to whatever reads it next."""
        rendered = render_prompt(DEFAULT_PROMPT, None)
        assert ANONYMOUS in rendered
        assert "{name}" not in rendered

    def test_a_blank_name_is_treated_as_no_name(self) -> None:
        assert ANONYMOUS in render_prompt(DEFAULT_PROMPT, "   ")

    @pytest.mark.parametrize(
        "template",
        [
            "{name} wants to speak {",  # unbalanced brace
            "{unknown} wants to speak",  # a placeholder nothing fills
            "{0} wants to speak",  # positional, and nothing is passed positionally
        ],
    )
    def test_a_broken_template_falls_back_rather_than_raising(self, template: str) -> None:
        """This renders on the path that stops the avatar talking over somebody. An operator's
        typo in an env var must not be able to break that."""
        rendered = render_prompt(template, "Dev")
        assert "Dev" in rendered
        assert rendered == DEFAULT_PROMPT.format(name="Dev")

    def test_a_custom_template_is_used_verbatim(self) -> None:
        assert render_prompt("{name} has a question.", "Dev") == "Dev has a question."


class TestMeetHandRaiseSource:
    @staticmethod
    def _source(**kwargs: object) -> MeetHandRaiseSource:
        return MeetHandRaiseSource(clock=MediaClock(), **kwargs)  # type: ignore[arg-type]

    def test_it_satisfies_the_port(self) -> None:
        assert isinstance(self._source(), HandRaiseSource)

    async def test_a_raised_hand_is_yielded_as_an_event(self) -> None:
        source = self._source()
        await source.start()
        assert source.offer({"id": "p1", "name": "Priya"}) is True

        event = await asyncio.wait_for(anext(source.events()), timeout=1)
        assert event.participant == "Priya"
        assert "Priya" in event.prompt
        assert event.raised_at_us > 0

    async def test_the_same_participant_is_ignored_during_the_cooldown(self) -> None:
        """Meet's indicator re-renders constantly, and an impatient person lowers and re-raises.
        Either produces a burst, and an avatar interrupted repeatedly never gets as far as
        saying "go ahead"."""
        source = self._source(cooldown_s=60.0)
        await source.start()

        assert source.offer({"id": "p1", "name": "Priya"}) is True
        for _ in range(5):
            assert source.offer({"id": "p1", "name": "Priya"}) is False

        assert source.received == 1
        assert source.ignored == 5

    async def test_a_different_participant_is_not_held_by_someone_elses_cooldown(self) -> None:
        """The limit is per person: two people wanting to speak is two requests."""
        source = self._source(cooldown_s=60.0, maxsize=4)
        await source.start()

        assert source.offer({"id": "p1", "name": "Priya"}) is True
        assert source.offer({"id": "p2", "name": "Dev"}) is True
        assert source.received == 2

    async def test_a_cooldown_of_zero_lets_every_hand_through(self) -> None:
        source = self._source(cooldown_s=0, maxsize=4)
        await source.start()

        assert source.offer({"id": "p1", "name": "Priya"}) is True
        assert source.offer({"id": "p1", "name": "Priya"}) is True
        assert source.received == 2

    async def test_the_avatars_own_hand_is_ignored(self) -> None:
        """The page's own verdict, which is the only side that can see whose row it is."""
        source = self._source()
        await source.start()
        assert source.offer({"id": "self", "name": "AI Avatar", "isSelf": True}) is False
        assert source.received == 0
        assert source.ignored == 1

    async def test_a_hand_matching_a_known_self_name_is_ignored(self) -> None:
        """The second half of the check: the page compares against the configured name, and a
        signed-in profile joins under the Google account's name instead. The roster is where
        that name comes from."""
        source = self._source(self_names=("AI Avatar",))
        source.observe_self_name("TrueFan Interview Avatar")
        await source.start()

        assert source.offer({"id": "p9", "name": "trueFAN interview avatar"}) is False
        assert source.received == 0

    async def test_a_full_queue_keeps_the_hand_already_waiting(self) -> None:
        """The opposite of chat's policy, deliberately. What is queued is an interruption that
        has not been delivered yet; replacing it with a newer one delays the floor changing
        hands rather than hastening it."""
        source = self._source(maxsize=1, cooldown_s=0)
        await source.start()

        assert source.offer({"id": "p1", "name": "First"}) is True
        assert source.offer({"id": "p2", "name": "Second"}) is False
        assert source.dropped == 1

        event = await asyncio.wait_for(anext(source.events()), timeout=1)
        assert event.participant == "First"

    async def test_an_unattributed_hand_is_still_a_request(self) -> None:
        source = self._source()
        await source.start()
        assert source.offer({"id": "abc"}) is True

        event = await asyncio.wait_for(anext(source.events()), timeout=1)
        assert event.participant is None
        assert ANONYMOUS in event.prompt

    @pytest.mark.parametrize("body", ["not a dict", None, 42])
    async def test_offer_never_raises_on_junk(self, body: object) -> None:
        """It is called from the bridge's read loop, which is the media channel: an exception
        here stops audio in both directions."""
        source = self._source()
        await source.start()
        assert source.offer(body) is False  # type: ignore[arg-type]

    def test_health_is_unknown_before_start(self) -> None:
        assert self._source().health().state.name == "UNKNOWN"

    async def test_health_is_healthy_once_started(self) -> None:
        source = self._source()
        await source.start()
        assert source.health().state.name == "HEALTHY"


class TestTheFrameTheAgentReceives:
    @staticmethod
    def _client(ctx: FrameContext, version: str) -> tuple[AvatarClient, FakeAvatarTransport]:
        transport = FakeAvatarTransport(
            ctx=ctx, reply=AvatarServerHello(protocol_version=version)
        )
        return AvatarClient(transport=transport, ctx=ctx), transport

    async def test_the_agent_receives_it_as_a_chat_frame(self, frame_ctx: FrameContext) -> None:
        """The decision this feature turns on.

        A dedicated ``interrupt`` kind was written first and reverted: against the agent that
        exists today a raised hand did nothing at all, while every layer here reported
        success. What the agent already parses is ``chat`` — a line of text from a named
        participant — so that is what a raised hand becomes, byte for byte identical to a
        typed question.
        """
        client, transport = self._client(frame_ctx, str(AVATAR_PROTOCOL_VERSION))
        await client.start()

        sent = await client.send_hand_raise(
            HandRaise(participant="Priya", prompt="Priya wants to speak.", raised_at_us=42)
        )

        assert sent is True
        frame = AvatarChatMessage.model_validate_json(transport.sent_control[0])
        assert frame.kind == "chat"
        assert frame.text == "Priya wants to speak."
        assert frame.sender == "Priya"
        assert frame.sent_at_us == 42

    async def test_it_is_the_same_frame_a_typed_question_produces(
        self, frame_ctx: FrameContext
    ) -> None:
        """Not merely similar — identical, which is what makes an unmodified agent handle it.

        If these ever diverge, the raised-hand path has grown a shape the agent was never
        taught, and it will fail exactly the way the interrupt frame did: silently.
        """
        client, transport = self._client(frame_ctx, str(AVATAR_CHAT_MIN_VERSION))
        await client.start()

        await client.send_hand_raise(HandRaise(participant="Dev", prompt="Dev wants to speak."))
        await client.send_chat(ChatMessage(text="Dev wants to speak.", sender="Dev"))

        assert transport.sent_control[0] == transport.sent_control[1]

    async def test_an_agent_without_a_text_channel_is_sent_nothing(
        self, frame_ctx: FrameContext
    ) -> None:
        """Below 1.1 there is no text channel at all, so there is nothing to send."""
        client, transport = self._client(frame_ctx, "1.0")
        await client.start()

        assert await client.send_hand_raise(HandRaise(prompt="somebody wants to speak")) is False
        assert transport.sent_control == []

    async def test_the_avatars_own_hand_is_never_forwarded(
        self, frame_ctx: FrameContext
    ) -> None:
        client, transport = self._client(frame_ctx, str(AVATAR_PROTOCOL_VERSION))
        await client.start()

        assert await client.send_hand_raise(HandRaise(prompt="x", is_self=True)) is False
        assert transport.sent_control == []

    @pytest.mark.parametrize("prompt", ["", "   ", "\n"])
    async def test_an_empty_prompt_is_not_worth_stopping_for(
        self, frame_ctx: FrameContext, prompt: str
    ) -> None:
        """A template that renders to nothing would have the avatar stop speaking to say
        nothing, which is worse than not interrupting."""
        client, transport = self._client(frame_ctx, str(AVATAR_PROTOCOL_VERSION))
        await client.start()

        assert await client.send_hand_raise(HandRaise(prompt=prompt)) is False
        assert transport.sent_control == []
