"""MediaWatchdog — catching the failure mode that reports as healthy.

**The gap this exists to close.** Every *structural* failure on this connector is already
visible. A crashed renderer closes the page channel, so the read loop raises and the bridge
rejoins. A denied join arrives as a ``MEET_STATE``. A dropped socket surfaces on the next
write. All of that is covered.

What is not covered is the browser staying perfectly alive while the audio stops. It happens
for real, in several ways:

* the remote tracks all ended and Meet never renegotiated, so the capture graph has no input;
* the capture ``AudioContext`` was suspended by Chromium under memory pressure, and a
  suspended context renders nothing, silently;
* ``getUserMedia`` was patched but Meet acquired its tracks before the patch installed, so
  the avatar publishes nothing while looking joined;
* Meet muted the avatar server-side and the UI state we read did not update.

In all four the tab is running, the channel is connected, the pacer is publishing, and every
health check is green. The only observable is that no audio has arrived for a while. So that
is what this watches.

**Why silence alone is not the trigger.** An avatar alone in a meeting, or in a meeting where
nobody is speaking, legitimately receives nothing — and a candidate thinking for thirty
seconds must not look like a fault. The watchdog therefore requires *both* a silence window
**and** at least one other participant in the roster before it says anything. That is what
separates "nobody is talking" from "we have stopped hearing", and it is the whole reason the
roster is collected despite playing no part in echo suppression.

**Why it degrades rather than fails.** It reports impairment and lets ``SessionSupervisor``'s
grace window decide. The signal is inferential — the roster comes from a machine-generated
DOM, and a roster misread would otherwise be enough to kill a working session.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass

from src.connectors.google_meet.bridge.chromium_bridge import ChromiumBridge
from src.connectors.google_meet.websocket.protocol import MeetState
from src.domain.health import ComponentHealth, ComponentState
from src.infrastructure.logging import get_logger

logger = get_logger(__name__)

COMPONENT_NAME = "google_meet_watchdog"

DEFAULT_SILENCE_GRACE_S = 45.0
"""How long inbound audio may be absent, with others present, before it is called a fault.

Generous on purpose. The cost of a false positive is a healthy session marked degraded and
possibly rejoined — an avatar visibly leaving and re-entering a customer's meeting — where the
cost of being slow is 45 seconds of an avatar that cannot hear. The second is much cheaper,
and a genuine fault does not heal on its own, so waiting loses nothing."""


@dataclass(frozen=True, slots=True)
class WatchdogVerdict:
    """What the watchdog concluded on one pass."""

    state: ComponentState
    detail: str | None = None
    silent_for_s: float = 0.0
    others_present: int = 0


class MediaWatchdog:
    """Watches whether conference audio is still arriving."""

    __slots__ = (
        "_bridge",
        "_grace_s",
        "_interval_s",
        "_last_frames",
        "_last_progress_at",
        "_source",
        "_task",
        "_verdict",
    )

    def __init__(
        self,
        *,
        bridge: ChromiumBridge,
        source,
        interval_s: float = 5.0,
        silence_grace_s: float = DEFAULT_SILENCE_GRACE_S,
    ) -> None:
        self._bridge = bridge
        self._source = source
        self._interval_s = interval_s
        self._grace_s = silence_grace_s
        self._last_frames = -1
        self._last_progress_at: float | None = None
        self._task: asyncio.Task[None] | None = None
        self._verdict = WatchdogVerdict(state=ComponentState.UNKNOWN)

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Begin watching. Idempotent."""
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="meet-watchdog")

    async def stop(self) -> None:
        """Stop watching. Idempotent."""
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._interval_s)
            self._verdict = self._assess()

    # -- assessment --------------------------------------------------------

    def _assess(self) -> WatchdogVerdict:
        """Decide whether audio has stopped arriving when it should not have.

        Deliberately a pure function of observable counters and the roster, with the clock
        as its only side channel — so it can be driven directly in a test without waiting on
        real time.
        """
        loop = asyncio.get_running_loop()
        now = loop.time()

        if not self._bridge.is_joined or self._bridge.meet_state is not MeetState.JOINED:
            # Not in the call. Whatever is wrong, it is not this component's finding, and
            # the bridge is already reporting it.
            self._last_progress_at = None
            self._last_frames = -1
            return WatchdogVerdict(state=ComponentState.UNKNOWN, detail="not in the call")

        frames = int(getattr(self._source, "frames_received", 0))
        if frames != self._last_frames:
            self._last_frames = frames
            self._last_progress_at = now
            return WatchdogVerdict(
                state=ComponentState.HEALTHY,
                others_present=len(self._bridge.roster.others),
            )

        started = self._last_progress_at
        if started is None:
            # First pass after joining. No baseline yet, so there is nothing to compare
            # against — claiming a fault here would flag every session at startup.
            self._last_progress_at = now
            return WatchdogVerdict(state=ComponentState.UNKNOWN, detail="no baseline yet")

        silent_for = now - started
        others = len(self._bridge.roster.others)

        if others == 0:
            # Alone in the meeting. Silence is the correct and expected observation, and the
            # clock is held rather than advanced so that the grace window starts from the
            # moment someone actually arrives.
            self._last_progress_at = now
            return WatchdogVerdict(
                state=ComponentState.HEALTHY,
                detail="alone in the meeting",
                silent_for_s=silent_for,
            )

        if silent_for < self._grace_s:
            return WatchdogVerdict(
                state=ComponentState.HEALTHY,
                silent_for_s=silent_for,
                others_present=others,
            )

        detail = (
            f"no conference audio for {silent_for:.0f}s with {others} other "
            "participant(s) present; the capture graph may have lost its inputs or been "
            "suspended"
        )
        logger.warning(
            "meet_watchdog.audio_stalled",
            silent_for_s=round(silent_for, 1),
            others=others,
        )
        return WatchdogVerdict(
            state=ComponentState.DEGRADED,
            detail=detail,
            silent_for_s=silent_for,
            others_present=others,
        )

    # -- observation -------------------------------------------------------

    @property
    def verdict(self) -> WatchdogVerdict:
        return self._verdict

    def health(self) -> ComponentHealth:
        """Current verdict as a health reading. Must not block."""
        return ComponentHealth(
            name=COMPONENT_NAME, state=self._verdict.state, detail=self._verdict.detail
        )
