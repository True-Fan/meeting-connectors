"""EchoGuard — stops the avatar hearing itself.

**The failure this prevents happens on the happy path**, which is what makes it worth
a dedicated component. The bot publishes avatar audio into the meeting; Zoom mixes it;
RTMS delivers it back to us; we forward it to the avatar; the avatar responds to
itself. An infinite feedback loop with nothing broken (doc 001 §6.3).

Two layers, because either alone has a hole:

1. **Own-participant filter.** With ``AUDIO_MULTI_STREAMS``, drop frames whose
   ``user_id`` is the bot's. Structural and precise — but only once the publisher has
   joined and reported its id, and only if the SDK's id space matches RTMS's, which
   is unverified (doc 002 §12.2 B3).

2. **Speaking gate.** While the avatar is publishing, and for a hangover afterwards,
   drop inbound audio regardless of attribution. Covers the window before the id is
   known, a mixed-stream fallback, and any id-space mismatch.

The hangover matters: Zoom's own pipeline delays our audio on the way back, so the
echo of a frame we published arrives *after* we stopped publishing it. Closing the
gate the instant publishing stops would let the tail through.

Deliberately **not** a voice activity detector. The bridge runs no AI and makes no
speech decisions — it only knows what it published and when.
"""

from __future__ import annotations

from src.domain.media import AudioFrame
from src.domain.meeting import ParticipantRef
from src.infrastructure.logging import get_logger
from src.infrastructure.metrics import MetricName, MetricsCollector

logger = get_logger(__name__)

DEFAULT_HANGOVER_MS = 200


class EchoGuard:
    """Decides whether an inbound audio frame should reach the avatar."""

    __slots__ = (
        "_hangover_us",
        "_last_publish_pts_us",
        "_metrics",
        "_own_user_id",
        "_per_participant",
        "_strict",
        "_suppressed",
    )

    def __init__(
        self,
        *,
        per_participant_audio: bool,
        hangover_ms: int = DEFAULT_HANGOVER_MS,
        metrics: MetricsCollector | None = None,
    ) -> None:
        self._per_participant = per_participant_audio
        self._hangover_us = max(hangover_ms, 0) * 1_000
        self._metrics = metrics
        self._own_user_id: int | None = None
        self._last_publish_pts_us: int | None = None
        self._suppressed = 0

        # Without per-participant attribution the gate is the only defence, so it
        # must run in strict mode rather than as a backstop.
        self._strict = not per_participant_audio
        if self._strict:
            logger.warning(
                "echo_guard.strict_mode",
                reason="per-participant audio unavailable; relying on the speaking gate alone",
            )

    @property
    def suppressed(self) -> int:
        """Lifetime count of frames withheld from the avatar."""
        return self._suppressed

    @property
    def own_user_id(self) -> int | None:
        return self._own_user_id

    @property
    def is_strict(self) -> bool:
        return self._strict

    def set_own_participant(self, participant: ParticipantRef | None) -> None:
        """Record the identity the publisher joined as.

        Called once the sink reports it. Until then only the gate protects us.
        """
        if participant is None:
            return
        self._own_user_id = participant.user_id
        logger.info("echo_guard.own_participant_known", user_id=participant.user_id)

    def note_publishing(self, pts_us: int) -> None:
        """Record that avatar audio was published at ``pts_us``.

        Called by the pacer. This is what arms the gate — the loop is only closed if
        the publish path reports back.
        """
        if self._last_publish_pts_us is None or pts_us > self._last_publish_pts_us:
            self._last_publish_pts_us = pts_us

    def is_gate_open(self, now_us: int) -> bool:
        """True when the gate is currently withholding audio."""
        if self._last_publish_pts_us is None:
            return False
        return now_us - self._last_publish_pts_us <= self._hangover_us

    def should_forward(self, frame: AudioFrame, *, now_us: int) -> bool:
        """Decide whether ``frame`` reaches the avatar."""
        reason: str | None = None

        if (
            self._own_user_id is not None
            and frame.participant is not None
            and frame.participant.user_id == self._own_user_id
        ):
            reason = "own_participant"
        elif self.is_gate_open(now_us):
            reason = "speaking_gate"

        if reason is None:
            return True

        self._suppressed += 1
        if self._metrics is not None:
            self._metrics.increment(
                MetricName.ECHO_FRAMES_SUPPRESSED_TOTAL, ctx=frame.ctx, reason=reason
            )
        return False

    def reset(self) -> None:
        """Clear gate state. Call when publishing stops for good."""
        self._last_publish_pts_us = None
