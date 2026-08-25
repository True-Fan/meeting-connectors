"""MeetAudioSource — the ``AudioSource`` port over the Chromium bridge.

A view, not an owner. ``ChromiumBridge`` owns the browser, the join, the page channel and
recovery, because one browser tab carries both directions; this adapter exists so that
``MediaRouter`` — shared, platform-blind, already shipped for Zoom and reused unchanged by
Teams — can consume Google Meet audio through the identical port it consumes RTMS audio
through.

Contrast the three, because the asymmetry is the architecture rather than an
inconsistency:

* ``RtmsAudioSource`` is a large object: it owns its own WebSocket, handshake, keep-alive
  and reconnect loop, because Zoom's ingest genuinely is an independent integration that can
  fail and heal on its own.
* ``TeamsAudioSource`` is thin, because one ``LocalMediaSession`` carries both directions.
* This is thin for the same reason and more strongly: ingest and egress are the *same
  browser tab*. Giving this adapter its own lifecycle would mean inventing an independence
  Meet does not have, and the health report would then be telling the operator something
  untrue.

This adapter is also where inbound frames are **counted**. ``ChromiumBridge`` deliberately
records no metrics — it is the browser-control layer — so frame accounting lives at the
three points where frames actually cross a boundary, and this is the inbound one.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from src.connectors.google_meet.bridge.chromium_bridge import ChromiumBridge
from src.domain.avatar import AVATAR_INPUT_FORMAT
from src.domain.health import ComponentHealth
from src.domain.media import AudioFormat, AudioFrame
from src.infrastructure.logging import get_logger
from src.infrastructure.metrics import MetricName, MetricsCollector
from src.services.media.queues import QueueClosedError

logger = get_logger(__name__)

COMPONENT_NAME = "google_meet_ingest"


class MeetAudioSource:
    """Live Google Meet conference audio, delivered over the page bridge."""

    __slots__ = ("_bridge", "_frames", "_metrics")

    def __init__(
        self, *, bridge: ChromiumBridge, metrics: MetricsCollector | None = None
    ) -> None:
        self._bridge = bridge
        self._metrics = metrics
        self._frames = 0

    async def start(self) -> None:
        """No-op: the bridge's join already started the audio flow.

        The port requires it, and honouring it as a no-op is the honest implementation —
        there is nothing for ingest to start on its own. Opening a second capture path here
        would mean two ``AudioContext`` graphs sampling the same tracks.
        """

    async def stop(self) -> None:
        """No-op: ``GoogleMeetSession`` stops the bridge once, for both legs.

        Idempotent by construction. Stopping the bridge from here would close the browser
        as a side effect of stopping ingest, taking egress with it.
        """

    async def frames(self) -> AsyncIterator[AudioFrame]:
        """Yield conference audio until the bridge closes."""
        queue = self._bridge.audio_queue()
        while True:
            try:
                frame = await queue.get()
            except QueueClosedError:
                return
            self._frames += 1
            if self._metrics is not None:
                self._metrics.increment(
                    MetricName.FRAMES_RECEIVED_TOTAL, ctx=frame.ctx, kind="audio"
                )
            yield frame

    def health(self) -> ComponentHealth:
        """The bridge's health, renamed for this leg.

        Both legs report the same underlying state, which is the truth for a single browser
        tab: when the tab is gone, neither direction is working and the avatar is not in the
        meeting at all.
        """
        bridge_health = self._bridge.health()
        return ComponentHealth(
            name=COMPONENT_NAME, state=bridge_health.state, detail=bridge_health.detail
        )

    @property
    def frames_received(self) -> int:
        return self._frames

    @property
    def audio_format(self) -> AudioFormat:
        """What this source yields.

        Always ``AVATAR_INPUT_FORMAT``. The capture ``AudioContext`` is constructed at
        16 kHz so Web Audio does the downsample inside the browser, and
        ``audio_capture/mapping.py`` rejects anything else at the boundary — so this is a
        checked property rather than an assumption.
        """
        return AVATAR_INPUT_FORMAT
