"""Keeping the avatar from hearing itself.

**RTMS delivers every participant, including us.** That is the difference from the
Google Meet connector, where capture taps only *remote* WebRTC tracks and the
avatar's own audio structurally cannot re-enter. Here it can, and it does: the avatar
speaks into the meeting, Zoom mixes it, and RTMS hands it straight back.

What that produced, live: the speech-to-text transcribed the avatar's own voice as
fragments of Portuguese and Hungarian, the agent answered them, and the interruption
detector fired on its own speech. A conversation with nobody.

``EchoGuard`` did not stop it, and could not have. It filters by ``user_id``, and the
browser never tells us our own Zoom participant id — so its identity filter was inert
and it fell through to the speaking gate, whose ~200 ms hangover exists to allow
barge-in, not to mask seconds of the avatar talking.

**The name is the identity we do have.** The avatar joins with a display name we
chose, and RTMS labels every frame with the speaker's name, so matching on that is
exact rather than timing-based — it suppresses the avatar's audio completely while
leaving a real participant free to interrupt at any moment, which is the property the
gate is protecting.

The obvious alternative — widening ``EchoGuard`` to match on names — was rejected:
it is shared by three connectors, and a name is a weaker identity than a user id
(two participants may share one). Confining the weaker rule to the connector that
needs it keeps the other two on the stronger one.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from src.domain.health import ComponentHealth
from src.domain.media import AudioFrame
from src.infrastructure.logging import get_logger
from src.protocols.audio_source import AudioSource

logger = get_logger(__name__)


class SelfAudioFilter:
    """An ``AudioSource`` that drops the avatar's own voice coming back."""

    __slots__ = ("_display_name", "_forwarded", "_inner", "_suppressed")

    def __init__(self, *, inner: AudioSource, display_name: str) -> None:
        self._inner = inner
        self._display_name = display_name.strip().casefold()
        self._suppressed = 0
        self._forwarded = 0

    @property
    def suppressed(self) -> int:
        return self._suppressed

    async def start(self) -> None:
        await self._inner.start()

    async def stop(self) -> None:
        await self._inner.stop()

    async def frames(self) -> AsyncIterator[AudioFrame]:
        async for frame in self._inner.frames():
            if self._is_own(frame):
                self._suppressed += 1
                if self._suppressed == 1:
                    logger.info(
                        "zoom_web.self_audio_suppressed",
                        display_name=self._display_name,
                        note="the avatar's own voice is arriving back over RTMS and "
                        "is being dropped so it cannot talk to itself",
                    )
                continue
            self._forwarded += 1
            yield frame

    def health(self) -> ComponentHealth:
        """The wrapped source's health, with what this filter did as detail.

        Worth surfacing: a suppressed count that stays at zero while the avatar is
        speaking means the name did not match, and the echo loop is live again.
        """
        inner = self._inner.health()
        detail = f"forwarded={self._forwarded} self_suppressed={self._suppressed}"
        if inner.detail:
            detail = f"{inner.detail}; {detail}"
        return ComponentHealth(name=inner.name, state=inner.state, detail=detail)

    def _is_own(self, frame: AudioFrame) -> bool:
        """True when this frame is the avatar hearing itself.

        Unattributed frames are forwarded rather than dropped: a mixed stream
        carries no name, and dropping everything nameless would make the agent deaf
        rather than merely echo-free.
        """
        participant = frame.participant
        if participant is None or not participant.display_name:
            return False
        return participant.display_name.strip().casefold() == self._display_name
