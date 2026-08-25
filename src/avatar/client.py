"""AvatarClient — protocol concerns over an ``AvatarTransport``.

Split from the transport so its logic is testable without a socket. The client owns:

* the handshake (built from the fixed contract in ``domain.avatar``);
* **init-segment caching**, replayed into every new decoder;
* reconnect with backoff;
* PCM forwarding with format validation.

The init-segment cache is the load-bearing piece. An fMP4 decoder cannot resume from
a mid-stream ``moof``, so on decoder restart the cached ``ftyp``+``moov`` must be
replayed first. Without it, restart looks successful and produces permanently black
video (doc 003 §0.2).

This module is platform-blind by construction: no Zoom import, enforced by
``tests/architecture/test_layering.py``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from src.domain.avatar import (
    AVATAR_CHAT_MIN_VERSION,
    AVATAR_INPUT_FORMAT,
    AVATAR_MEETING_CONTEXT_MIN_VERSION,
    AvatarChatMessage,
    AvatarClientHello,
    AvatarMeetingContext,
    AvatarProtocolVersion,
    check_handshake,
)
from src.domain.context import FrameContext
from src.domain.exceptions import AvatarProtocolMismatchError, InvalidFrameError
from src.domain.health import ComponentHealth, ComponentState
from src.domain.media import AudioFrame, MediaChunk
from src.domain.meeting import ChatMessage, HandRaise
from src.infrastructure.logging import get_logger
from src.infrastructure.metrics import MetricName, MetricsCollector
from src.infrastructure.reconnect import ReconnectPolicy
from src.protocols.avatar import AvatarTransport

logger = get_logger(__name__)

COMPONENT_NAME = "avatar_client"


class AvatarClient:
    """Sends PCM to the avatar agent and yields the fMP4 it streams back."""

    __slots__ = (
        "_chat_sent",
        "_chat_unsupported_warned",
        "_connected",
        "_context_sent",
        "_context_unsupported_warned",
        "_ctx",
        "_init_segment",
        "_metrics",
        "_negotiated",
        "_policy",
        "_transport",
        "_transport_factory",
    )

    def __init__(
        self,
        *,
        transport: AvatarTransport,
        ctx: FrameContext,
        policy: ReconnectPolicy | None = None,
        metrics: MetricsCollector | None = None,
        transport_factory: object | None = None,
    ) -> None:
        self._transport = transport
        self._ctx = ctx
        self._policy = policy or ReconnectPolicy()
        self._metrics = metrics
        self._transport_factory = transport_factory
        self._init_segment: MediaChunk | None = None
        self._connected = False
        self._negotiated: AvatarProtocolVersion | None = None
        self._chat_sent = 0
        self._chat_unsupported_warned = False
        self._context_sent = 0
        self._context_unsupported_warned = False

    @property
    def init_segment(self) -> MediaChunk | None:
        """The cached ``ftyp``+``moov``, for decoder (re)start."""
        return self._init_segment

    @property
    def is_connected(self) -> bool:
        return self._connected

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Connect and complete the handshake.

        Raises:
            AvatarProtocolMismatchError: the agent is incompatible. Not retried —
                a version mismatch will not resolve itself.
            AvatarTransportError: the connection failed.
        """
        hello = AvatarClientHello(
            session_id=self._ctx.session_id, correlation_id=self._ctx.correlation_id
        )
        reply = await self._transport.connect(hello)
        # Retained because it decides what may be sent. ``check_handshake`` returns the lower
        # of the two minors, so an agent that predates chat negotiates 1.0 and this stays
        # below the chat threshold for the session's life.
        self._negotiated = check_handshake(hello, reply)
        self._connected = True

    @property
    def supports_chat(self) -> bool:
        """Whether the negotiated version includes inbound chat."""
        return self._negotiated is not None and self._negotiated >= AVATAR_CHAT_MIN_VERSION

    @property
    def supports_meeting_context(self) -> bool:
        """Whether the negotiated version includes silent meeting-context briefs."""
        return (
            self._negotiated is not None
            and self._negotiated >= AVATAR_MEETING_CONTEXT_MIN_VERSION
        )

    async def stop(self) -> None:
        """Disconnect. Idempotent."""
        self._connected = False
        await self._transport.close()

    def health(self) -> ComponentHealth:
        transport_health = self._transport.health()
        if transport_health.state is ComponentState.HEALTHY and not self._connected:
            return ComponentHealth.unknown(COMPONENT_NAME, "not started")
        return ComponentHealth(
            name=COMPONENT_NAME,
            state=transport_health.state,
            detail=transport_health.detail,
        )

    # -- media -------------------------------------------------------------

    async def send(self, frame: AudioFrame) -> None:
        """Forward one audio frame to the agent.

        The format is asserted rather than converted. RTMS is configured to deliver
        exactly ``AVATAR_INPUT_FORMAT``, so a mismatch is a wiring bug that should
        surface here — not something to paper over with a silent resample in the hot
        path (doc 003 §3.3).

        Raises:
            InvalidFrameError: the frame is not in the avatar's input format.
        """
        if frame.format != AVATAR_INPUT_FORMAT:
            raise InvalidFrameError(
                f"avatar requires {AVATAR_INPUT_FORMAT}, got {frame.format}. "
                "RTMS should have been subscribed to the avatar's native format."
            )
        await self._transport.send_pcm(frame.pcm)

    async def send_chat(self, message: ChatMessage) -> bool:
        """Forward one chat message to the agent as a text frame.

        Returns True when the frame was handed to the transport. False means the message was
        deliberately withheld, which happens in two cases and neither is an error:

        * the avatar's own account sent it — answering your own chat message is the text
          equivalent of the feedback loop ``EchoGuard`` exists to prevent;
        * the negotiated protocol predates chat, so the agent has no way to parse the frame.
          Withheld rather than sent-and-ignored, because an old agent cannot tell us it did
          not understand and the operator deserves to know why chat is doing nothing.

        Empty or whitespace-only text is dropped too: a chat panel scrape can produce blank
        strings from a UI element, and waking the agent to answer nothing would make the
        avatar interrupt itself for no reason.
        """
        if message.is_self:
            return False

        text = message.text.strip()
        if not text:
            return False

        if not self.supports_chat:
            if not self._chat_unsupported_warned:
                self._chat_unsupported_warned = True
                logger.warning(
                    "avatar.chat_unsupported",
                    negotiated=str(self._negotiated) if self._negotiated else None,
                    required=str(AVATAR_CHAT_MIN_VERSION),
                    note="the agent predates chat support, so meeting chat will be ignored; "
                    "upgrade the avatar agent to receive typed questions",
                )
            return False

        frame = AvatarChatMessage(
            text=text, sender=message.sender, sent_at_us=message.received_at_us
        )
        await self._transport.send_control(frame.model_dump_json())
        self._chat_sent += 1
        logger.info(
            "avatar.chat_forwarded",
            sender=message.sender,
            chars=len(text),
            total=self._chat_sent,
        )
        return True

    async def send_hand_raise(self, event: HandRaise) -> bool:
        """Tell the agent that somebody in the meeting wants the floor.

        **Delivered as a chat frame, on purpose.** A dedicated ``interrupt`` kind was written
        first and reverted: it made the whole feature depend on the agent learning a second
        frame kind, and against the agent that exists today a raised hand did nothing at all
        while every layer here reported success. What the agent already understands is
        ``chat`` — a line of text from a named participant — and "Priya raised their hand and
        wants to say something" *is* that. So the bytes on this wire are byte-for-byte the
        ones a typed question produces, and the feature works against an unmodified agent.

        The barge-in itself does not travel over this socket: ``Pacer.interrupt`` cuts the
        avatar's audio in the meeting directly, which is both faster than a round trip and
        independent of what the agent chooses to do. This frame is what makes it *say*
        something rather than just falling silent.

        Returns True when the frame was handed to the transport. False means it was withheld,
        which happens for the same three reasons chat is: our own hand, empty text, or an agent
        that predates the text channel entirely.
        """
        logger.info(
            "avatar.hand_raise_forwarded",
            participant=event.participant,
            note="delivered on the chat channel, which is the frame the agent already parses",
        )
        return await self.send_chat(
            ChatMessage(
                text=event.prompt,
                sender=event.participant,
                received_at_us=event.raised_at_us,
                is_self=event.is_self,
            )
        )

    async def send_meeting_context(
        self,
        text: str,
        *,
        topic: str = "attendance",
        observed_at_us: int = 0,
        require_negotiation: bool = True,
    ) -> bool:
        """Give the agent standing context it should know but not speak.

        Returns True when the frame was handed to the transport. False means it was withheld,
        and the only interesting case is an agent that negotiated below
        ``AVATAR_MEETING_CONTEXT_MIN_VERSION`` — warned once, because an operator who has just
        switched attendance on deserves to be told the agent cannot receive it rather than
        watching it silently do nothing. That was the actual failure mode this replaced: the
        bridge knew who was in the meeting and the agent answered "I don't have access".

        Unlike ``send_chat`` there is no self-filter and no empty-text special case beyond the
        obvious: this is not a turn, so there is no feedback loop to prevent.

        ``require_negotiation=False`` sends regardless of the negotiated version. That is a
        deliberate escape hatch rather than a default, and the asymmetry with chat is the
        reason it can exist at all: an unknown *chat* frame is dangerous because the agent
        understands the kind and would speak it, whereas an unknown *control* frame is only as
        dangerous as the agent's tolerance for one it does not recognise. An agent that ignores
        unknown kinds — most do — can therefore be given this without bumping its handshake,
        which turns a two-part agent change into a one-part one. An agent that instead throws on
        an unknown kind must not be sent it, which is why the safe behaviour is the default.
        """
        brief = text.strip()
        if not brief:
            return False

        if require_negotiation and not self.supports_meeting_context:
            if not self._context_unsupported_warned:
                self._context_unsupported_warned = True
                logger.warning(
                    "avatar.meeting_context_unsupported",
                    negotiated=str(self._negotiated) if self._negotiated else None,
                    required=str(AVATAR_MEETING_CONTEXT_MIN_VERSION),
                    note="the agent cannot receive meeting context, so it will not be able to "
                    "answer who is in the meeting; handle kind='meeting_context' in the agent, "
                    "or have it query GET /sessions/{id}/participants instead",
                )
            return False

        frame = AvatarMeetingContext(text=brief, topic=topic, observed_at_us=observed_at_us)
        await self._transport.send_control(frame.model_dump_json())
        self._context_sent += 1
        logger.info(
            "avatar.meeting_context_sent",
            topic=topic,
            chars=len(brief),
            total=self._context_sent,
        )
        return True

    async def chunks(self) -> AsyncIterator[MediaChunk]:
        """Yield fMP4 chunks, caching the init segment as it passes."""
        async for chunk in self._transport.chunks():
            if chunk.is_init_segment:
                self._init_segment = chunk
                logger.info(
                    "avatar.init_segment_cached",
                    bytes=chunk.size_bytes,
                    note="replayed into every decoder restart",
                )
            yield chunk

    async def reconnect(self) -> bool:
        """Reconnect with backoff.

        The cached init segment is **kept** across reconnects: the agent may not
        resend it, and the decoder still needs it.

        Returns:
            True on success, False when the retry budget is exhausted.
        """
        await self._transport.close()
        self._connected = False

        attempt = 0
        while True:
            attempt += 1
            if self._policy.exhausted(attempt):
                logger.error("avatar.reconnect_exhausted", attempts=attempt - 1)
                return False

            delay = await self._policy.sleep(attempt)
            try:
                await self.start()
            except AvatarProtocolMismatchError:
                raise  # never recoverable
            except Exception as exc:
                logger.warning(
                    "avatar.reconnect_failed",
                    attempt=attempt,
                    delay_s=round(delay, 3),
                    error=str(exc),
                )
                continue

            if self._metrics is not None:
                self._metrics.increment(
                    MetricName.RECONNECTS_TOTAL, ctx=self._ctx, component=COMPONENT_NAME
                )
            logger.info("avatar.reconnected", attempts=attempt)
            return True

    async def __aenter__(self) -> AvatarClient:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await asyncio.shield(self.stop())
