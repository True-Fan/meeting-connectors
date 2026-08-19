"""MediaRouter — the data plane.

Moves frames between ingest, the avatar, the decoder and the pacer. Routing only:
echo policy lives in ``EchoGuard``, decoder lifecycle in ``DecodePipeline``, timing in
``Pacer`` (doc 002 §1.2 D3 split this apart).

Four concurrent legs, all for the session's lifetime:

* ``_route_inbound``  — ingest → echo guard → avatar
* ``_route_chunks``   — avatar fMP4 → decode pipeline
* ``_route_video``    — decoder video → pacer
* ``_route_audio``    — decoder audio → pacer

The pacer runs its own loops, which is what keeps publishing continuous even when
every one of these legs is idle.

**Direct calls over bounded queues, not an event bus.** Doc 003 §0.1 cut the bus for a
concrete reason: a fan-out bus has no coherent backpressure semantics, so a slow
subscriber would either stall the meeting or silently drop — and the per-stage drop
policy in doc 003 §7.2 cannot be expressed as bus semantics. Events describe; queues
carry.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress

from src.avatar.client import AvatarClient
from src.domain.context import FrameContext
from src.domain.exceptions import AvatarProtocolMismatchError
from src.domain.health import ComponentHealth, HealthReport
from src.domain.media import AudioFrame
from src.domain.meeting import HandRaise
from src.infrastructure.logging import get_logger
from src.infrastructure.metrics import MetricName, MetricsCollector
from src.protocols.audio_source import AudioSource
from src.protocols.chat_source import ChatSource
from src.protocols.hand_raise_source import HandRaiseSource
from src.services.media.clock import MediaClock
from src.services.media.decode_pipeline import DecodePipeline
from src.services.media.echo_guard import EchoGuard
from src.services.media.pacer import Pacer
from src.services.media.speech_detector import SpeechDetector

logger = get_logger(__name__)

COMPONENT_NAME = "media_router"

ANONYMOUS_SPEAKER = "Someone"
"""Who a voice interruption is attributed to when nothing can name them.

The inbound mix carries no attribution — it is summed before it is sampled on every connector —
and the prompt has to name somebody, so this is the same stand-in ``meeting/hand_raise.py`` uses.

A connector that can identify the speaker *beside* the media path may pass ``speaker_provider``
and replace this with a name. Nothing about the audio changes when it does: the provider is a
dictionary lookup into an observer the connector already runs, called on the frame that triggers
the barge-in. See ``connectors/google_meet/meeting/active_speaker.py``."""


class MediaRouter:
    """Routes media between ingest, the avatar agent, and the publisher."""

    __slots__ = (
        "_avatar",
        "_chat",
        "_chat_forwarded",
        "_clock",
        "_ctx",
        "_decode",
        "_echo_guard",
        "_forwarded",
        "_hand_raise_mute_ms",
        "_hands",
        "_hands_forwarded",
        "_metrics",
        "_pacer",
        "_source",
        "_speaker_provider",
        "_speech",
        "_suppressed",
        "_voice_prompt",
        "_voice_prompt_template",
    )

    def __init__(
        self,
        *,
        ctx: FrameContext,
        clock: MediaClock,
        source: AudioSource,
        avatar: AvatarClient,
        decode: DecodePipeline,
        pacer: Pacer,
        echo_guard: EchoGuard,
        metrics: MetricsCollector | None = None,
        chat: ChatSource | None = None,
        hands: HandRaiseSource | None = None,
        hand_raise_mute_ms: int = 0,
        speech: SpeechDetector | None = None,
        voice_prompt: str = "",
        speaker_provider: Callable[[], str | None] | None = None,
        voice_prompt_template: str = "",
    ) -> None:
        self._ctx = ctx
        self._clock = clock
        self._source = source
        self._avatar = avatar
        self._decode = decode
        self._pacer = pacer
        self._echo_guard = echo_guard
        self._metrics = metrics
        # Optional, and last: Zoom and Teams pass nothing, so neither connector changed to
        # gain a feature only Meet implements. Absence means "this platform has no chat",
        # never a fault.
        self._chat = chat
        # Optional for the same reason and on the same terms: only Meet reports raised hands
        # today, and absence means "this platform has no such signal", never a fault.
        self._hands = hands
        self._hand_raise_mute_ms = hand_raise_mute_ms
        # Optional on the same terms as chat and hands: a connector that passes nothing has
        # an inbound leg byte-for-byte identical to what it had before.
        self._speech = speech
        # What the agent is told when a voice takes the floor, already rendered. The same
        # wording a raised hand sends, because it is the same request.
        self._voice_prompt = voice_prompt
        # Optional on the same terms as everything above: a connector that can say who is
        # talking passes these two, and one that cannot passes neither and behaves exactly as
        # it did. The template is the unrendered form of ``voice_prompt``, needed because the
        # name is not known until the moment somebody speaks.
        self._speaker_provider = speaker_provider
        self._voice_prompt_template = voice_prompt_template
        self._forwarded = 0
        self._suppressed = 0
        self._chat_forwarded = 0
        self._hands_forwarded = 0

    @property
    def stats(self) -> dict[str, int]:
        return {
            "forwarded": self._forwarded,
            "suppressed": self._suppressed,
            "chat_forwarded": self._chat_forwarded,
            "hand_raises_forwarded": self._hands_forwarded,
            **self._pacer.stats,
        }

    async def run(self) -> None:
        """Connect the avatar leg, then run every routing leg until cancelled or a failure.

        **The avatar connect is not optional, and it did not used to happen at all.**
        ``AvatarClient.start()`` performs the handshake, and nothing called it — not this
        class, not any connector session, not any test. The result was that every session on
        every platform published idle video and silence forever while the agent heard nothing:
        ``WebSocketAvatarTransport.send_pcm`` only offers to a bounded queue, and the task that
        drains that queue is created inside ``connect()``, so it never existed. Nothing failed
        loudly because every layer was doing exactly what it had been told.

        It belongs **here** rather than in the three session classes for the reason
        ``DecodePipeline.wait_started()`` does: the fault is in shared code, the router already
        owns the avatar for the session's lifetime, and one fix here means all three connectors
        are correct with no connector changing. Regression coverage lives in
        ``tests/unit/test_avatar_leg_startup.py``, which asserts against a real socket — the
        ``AvatarTransport`` double could never catch this, because its ``send_pcm`` appends to a
        list whether or not the transport was connected.

        Before the task group, not inside it: ``_route_inbound`` calls ``avatar.send()`` and
        ``_route_chunks`` iterates ``avatar.chunks()``, so both would touch an unconnected
        transport on their first iteration.

        **A connect failure degrades the session rather than killing it**, which is a
        deliberate choice and the subtler half of this fix. Raising here would be a *new* way
        for a session to die: an avatar-service blip would take live Zoom and Teams meetings
        down, and because every session class creates this as a background task the death would
        be silent. Degrading preserves exactly what an absent avatar did before — idle video
        and silence, reported through ``health()`` — while the reachable case now works. The
        failure is logged at ``error`` and the avatar component reports ``UNHEALTHY``, so it is
        loud where an operator looks rather than fatal where they cannot see it.
        """
        await self._connect_avatar()
        try:
            async with asyncio.TaskGroup() as group:
                group.create_task(self._route_inbound(), name="route-inbound")
                group.create_task(self._route_chunks(), name="route-chunks")
                group.create_task(self._route_video(), name="route-video")
                group.create_task(self._route_audio(), name="route-audio")
                group.create_task(self._pacer.run(), name="pacer")
                if self._chat is not None:
                    group.create_task(self._route_chat(), name="route-chat")
                if self._hands is not None:
                    group.create_task(self._route_hand_raises(), name="route-hand-raises")
        finally:
            # Symmetric with the connect above: whatever opens the socket closes it. The three
            # session classes all document tearing down "the avatar" and none of them actually
            # did — harmless while nothing ever connected, a leaked socket per session now that
            # something does. Best-effort, because teardown must never mask why we are here.
            with suppress(Exception):
                await self._avatar.stop()

    async def _connect_avatar(self) -> None:
        """Complete the avatar handshake, degrading loudly if the agent is unreachable.

        ``AvatarProtocolMismatchError`` is the one failure that still propagates: an
        incompatible major version will not resolve itself, no retry or degraded mode helps,
        and continuing would mean streaming PCM at an agent that cannot answer. Everything else
        — refused connection, timeout, DNS — is a transient the session can outlive.
        """
        try:
            await self._avatar.start()
        except AvatarProtocolMismatchError:
            raise
        except Exception as exc:
            logger.error(
                "router.avatar_unreachable",
                error=str(exc),
                note="the session will publish idle media and the avatar will hear nothing; "
                "check the avatar agent and MC_AVATAR__URL",
            )

    # -- inbound: Zoom → avatar -------------------------------------------

    async def _route_inbound(self) -> None:
        async for frame in self._source.frames():
            await self._forward(frame)

    async def _forward(self, frame: AudioFrame) -> None:
        now_us = self._clock.now_us()

        if not self._echo_guard.should_forward(frame, now_us=now_us):
            self._suppressed += 1
            return

        # Before the send, not after: stopping the avatar is the half of an interruption that
        # has to be immediate, and ``avatar.send`` awaits a transport that can be slow.
        taking_floor = self._note_speech(frame)
        if taking_floor is not None:
            try:
                await self._yield_floor(taking_floor, trigger="voice")
            except Exception as exc:
                # Contained exactly like the hand-raise leg's: an interruption that could not
                # be delivered must not take the meeting's audio down with it.
                logger.warning("router.speech_interrupt_failed", error=str(exc))
        elif self._speech is not None and self._speech.is_speaking:
            # Still talking. Renew the hold so the sentence still arriving from the agent
            # keeps being discarded rather than resuming between their words — a fixed window
            # fits a click, not a question.
            self._pacer.extend_hold(ms=self._hand_raise_mute_ms)

        started = self._clock.now_us()
        await self._avatar.send(frame)
        self._forwarded += 1

        if self._metrics is not None:
            self._metrics.observe(
                MetricName.ROUTER_TO_AVATAR_US, self._clock.now_us() - started, ctx=frame.ctx
            )
            # Ingest→router latency measured against the frame's own PTS, which was
            # stamped when RTMS delivered it.
            self._metrics.observe(
                MetricName.INGEST_TO_ROUTER_US, max(now_us - frame.pts_us, 0), ctx=frame.ctx
            )

    # -- inbound: meeting chat → avatar ------------------------------------

    async def _route_chat(self) -> None:
        """Forward typed messages to the agent, so a chat question gets a spoken answer.

        A separate leg rather than part of ``_route_inbound`` because the two share nothing
        operationally: audio is continuous and dropped when late, chat is rare and must not be
        dropped. Running them together would mean one loop with a branch and two incompatible
        backpressure policies.

        **No echo guard on this path, deliberately.** The guard exists to stop the avatar
        hearing its own *voice* mixed back through the meeting, which cannot happen to text —
        the avatar never types. The one self-reference that does exist, the avatar's own account
        posting a message, is filtered by ``AvatarClient.send_chat`` on the ``is_self`` flag.

        Failures are contained: a chat message that cannot be delivered must not kill the task
        group and take the meeting's audio with it.
        """
        chat = self._chat
        if chat is None:
            return
        async for message in chat.messages():
            try:
                if await self._avatar.send_chat(message):
                    self._chat_forwarded += 1
            except Exception as exc:
                logger.warning("router.chat_forward_failed", error=str(exc))

    # -- inbound: a raised hand → stop talking -----------------------------

    async def _route_hand_raises(self) -> None:
        """Yield the floor when somebody puts their hand up.

        Two actions, in this order and deliberately:

        1. ``Pacer.interrupt`` — local, immediate, and unconditional. It drops the avatar
           media already queued and holds the line for a moment while what is still in flight
           drains, so the voice stops within a frame or two of the hand going up.
        2. ``AvatarClient.send_hand_raise`` — the agent is told, as a chat message, that
           somebody wants to speak. Only it can decide what to say back, and delivering this
           as chat is what makes it work against an agent that was never modified for the
           feature.

        **The local step first, and it is not merely an optimisation.** The round trip to the
        agent is a network hop plus however long that agent takes to react; the queues drain in
        real time regardless. Doing it the other way round means the avatar talks over the
        person for the length of that round trip — the exact failure the feature exists to
        remove. Doing it locally *only* would be worse in the other direction: the sentence
        resumes the moment the hold lapses, because the agent never learned to stop.

        Unconditional on whether the avatar is currently speaking, which is the same request
        either way: a silent avatar drops nothing, mutes nothing audible, and still tells the
        agent that somebody wants the floor — which is what makes it say "go ahead" rather
        than sit there.

        Failures are contained, like the chat leg's: a raised hand that cannot be delivered
        must not kill the task group and take the meeting's audio with it.

        The leg is only a reader. ``_yield_floor`` does both actions, and does them
        identically for a participant who takes the floor by *speaking* — see
        ``_note_speech``.
        """
        hands = self._hands
        if hands is None:
            return
        async for event in hands.events():
            try:
                await self._yield_floor(event, trigger="hand")
            except Exception as exc:
                logger.warning("router.hand_raise_failed", error=str(exc))

    async def _yield_floor(self, event: HandRaise, *, trigger: str) -> None:
        """Stop the avatar and hand the floor over. The two actions above, in that order."""
        speaking = self._pacer.is_speaking
        dropped = self._pacer.interrupt(hold_ms=self._hand_raise_mute_ms)
        # **Open the gate immediately, because yielding the floor means we want to hear
        # them.** The gate withholds inbound audio for ``hangover_ms`` after the avatar
        # last published, which on a connector whose own voice loops back through the
        # conference has to be long enough to cover that round trip — a second or more.
        # Waiting it out here would drop the opening of the very question the interruption
        # was raised for, which is the feature failing in the least visible way possible:
        # the avatar stops, says "go ahead", and never hears the first half of the answer.
        #
        # The cost is the avatar's own in-flight tail arriving unguarded for a moment. That
        # is the trade taken deliberately: during a barge-in, hearing the person beats
        # suppressing an echo, and the pacer has already stopped feeding the loop.
        #
        # A no-op where the gate is disabled (Google Meet) or nothing yields the floor
        # (Zoom SDK, Teams), so no other connector changes behaviour.
        self._echo_guard.reset()
        logger.info(
            "router.floor_yielded",
            trigger=trigger,
            participant=event.participant,
            was_speaking=speaking,
            frames_dropped=dropped,
            hold_ms=self._hand_raise_mute_ms,
        )
        if await self._avatar.send_hand_raise(event):
            self._hands_forwarded += 1

    # -- inbound: a voice → stop talking -----------------------------------

    def _note_speech(self, frame: AudioFrame) -> HandRaise | None:
        """Report a participant starting to speak as the raised hand it amounts to.

        **A voice and a hand are the same request — "stop, I want to speak" — so they get the
        same answer, from the same method, with the same wording.** Nothing here is a second
        interrupt mechanism: this only decides *when*, and returns the event the hand-raise
        path already knows how to act on.

        That matters because the local half is not sufficient on its own. Dropping the pacer's
        queues disposes of speech that already exists; the agent goes on *generating* the rest
        of its sentence and resumes the moment the hold lapses. Only ``send_hand_raise`` stops
        that, and it is what makes the avatar say "ok, go ahead" rather than fall silent for a
        second and carry on. A raised hand has always done both. Now so does a voice.

        Returns None when nobody has just started — which is almost every frame.
        """
        detector = self._speech
        if detector is None or not detector.observe(frame):
            return None
        # Asked at the moment of the interruption, which is the only moment the answer is about.
        # A miss costs the name and never the barge-in — the two are decided independently, so an
        # attribution that is not available cannot delay the avatar falling silent.
        speaker = self._current_speaker()
        logger.info(
            "router.speech_detected",
            participant=speaker or ANONYMOUS_SPEAKER,
            attributed=speaker is not None,
            rms=round(detector.last_rms),
            noise_floor=round(detector.noise_floor),
            trigger_level=round(detector.trigger_level),
        )
        return HandRaise(
            # Named when a connector can identify the speaker beside the media path; otherwise
            # the same stand-in the hand-raise source uses for an unattributed indicator, because
            # the inbound mix itself carries no attribution.
            participant=speaker or ANONYMOUS_SPEAKER,
            prompt=self._voice_prompt_for(speaker),
            raised_at_us=self._clock.now_us(),
        )

    def _current_speaker(self) -> str | None:
        """Who the connector believes is talking, or ``None``.

        Total by construction: this runs on the inbound audio leg, so a provider that raises
        must cost the name rather than the frame — and losing the leg would stop the meeting's
        audio in both directions.
        """
        provider = self._speaker_provider
        if provider is None:
            return None
        try:
            name = provider()
        except Exception as exc:
            logger.warning("router.speaker_lookup_failed", error=str(exc))
            return None
        cleaned = " ".join(str(name or "").split())
        return cleaned or None

    def _voice_prompt_for(self, speaker: str | None) -> str:
        """The interruption prompt, naming the speaker when one is known.

        Falls back to the pre-rendered anonymous wording on anything unexpected. The template is
        operator-supplied (``GoogleMeetSettings.hand_raise_prompt``), so a stray brace in it must
        cost the name rather than the handover — the same trade
        ``connectors/google_meet/meeting/hand_raise.render_prompt`` makes.
        """
        if not speaker or not self._voice_prompt_template:
            return self._voice_prompt
        try:
            return self._voice_prompt_template.format(name=speaker).strip() or self._voice_prompt
        except (IndexError, KeyError, ValueError):
            return self._voice_prompt

    # -- avatar → decoder --------------------------------------------------

    async def _route_chunks(self) -> None:
        first_fragment_seen = False
        async for chunk in self._avatar.chunks():
            await self._decode.feed(chunk)

            if not chunk.is_init_segment and not first_fragment_seen:
                first_fragment_seen = True
                # Time to first fragment is the avatar's contribution to perceived
                # latency, and the number most worth watching (doc 003 §7.5).
                if self._metrics is not None:
                    self._metrics.observe(
                        MetricName.AVATAR_RTT_US,
                        self._clock.now_us() - chunk.received_at_us,
                        ctx=chunk.ctx,
                    )
                logger.info("router.first_fragment", seq=chunk.seq, bytes=chunk.size_bytes)

    # -- decoder → pacer ---------------------------------------------------

    async def _route_video(self) -> None:
        # The decoder does not exist until the avatar streams its first chunk, and
        # ``decoder.video()`` raises before then. Every leg starts at session start, so
        # without this wait the first thing a session did was kill its own task group.
        await self._decode.wait_started()
        async for frame in self._decode.decoder.video():
            self._pacer.submit_video(frame)
            if self._metrics is not None:
                self._metrics.observe(
                    MetricName.VIDEO_DELAY_US,
                    max(self._clock.now_us() - frame.pts_us, 0),
                    ctx=frame.ctx,
                )

    async def _route_audio(self) -> None:
        await self._decode.wait_started()
        async for frame in self._decode.decoder.audio():
            self._pacer.submit_audio(frame)
            if self._metrics is not None:
                self._metrics.observe(
                    MetricName.AUDIO_DELAY_US,
                    max(self._clock.now_us() - frame.pts_us, 0),
                    ctx=frame.ctx,
                )

    # -- health ------------------------------------------------------------

    def health(self) -> HealthReport:
        return HealthReport(
            components=(
                self._source.health(),
                self._avatar.health(),
                self._decode.health(),
                ComponentHealth.healthy(COMPONENT_NAME, f"forwarded={self._forwarded}"),
            )
        )

    def close(self) -> None:
        self._pacer.close()
