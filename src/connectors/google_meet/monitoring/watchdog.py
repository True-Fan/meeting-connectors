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

_PUBLISH_KEYS = ("micClonesIssued", "audioSendersForced")


def _has_publish_facts(page: dict) -> bool:
    """Whether the page reported on publishing at all.

    An older injected script, or a page mid-navigation, returns a stats object without these
    keys. Absent is not zero: treating a missing key as "nothing published" would invent a
    fault for a session nobody can prove anything about.
    """
    return any(key in page for key in _PUBLISH_KEYS)

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
        "_last_publish_signature",
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
        self._last_publish_signature: tuple[object, ...] | None = None
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
            # Fetched here rather than inside ``_assess`` so that method stays a pure
            # function of observable facts and remains drivable from a test without a browser.
            page = await self._page_stats()
            self._report_publish_state(page)
            self._verdict = self._assess(page)

    async def _page_stats(self) -> dict | None:
        """The page's own account of what Meet took, or ``None``.

        Swallows everything. This watchdog exists to *report* faults, so a diagnostic that
        could raise inside its own loop would silence the component meant to notice silence.
        """
        try:
            return await self._bridge.page_stats()
        except Exception as exc:
            logger.debug("meet_watchdog.page_stats_failed", error=str(exc))
            return None

    def _report_publish_state(self, page: dict | None) -> None:
        """Log the outbound half of the media path, once and then only when it changes.

        Third bullet of this module's docstring — ``getUserMedia`` patched after Meet already
        acquired its tracks — was listed as a known failure mode and was nonetheless
        undiagnosable, because no code read the page's own account of what Meet took. Silence
        in the meeting looked identical whether the worklet was starved, the context was
        suspended, or Meet was publishing a real empty device.

        ``micClonesIssued`` separates them: it is the number of microphone tracks Meet was
        handed. Zero, while joined, means the avatar cannot be heard no matter how perfect
        every upstream counter looks.
        """
        if page is None:
            return

        mic_clones = page.get("micClonesIssued")
        camera_clones = page.get("cameraClonesIssued")
        forced = page.get("audioSendersForced")
        seen = page.get("audioSendersSeen")
        playout = page.get("playout") or {}
        signature = (mic_clones, camera_clones, forced, seen, playout.get("underruns"))
        if signature == self._last_publish_signature:
            return
        self._last_publish_signature = signature

        if not mic_clones and not forced:
            logger.warning(
                "meet_watchdog.microphone_not_published",
                mic_clones=mic_clones,
                camera_clones=camera_clones,
                audio_senders_seen=seen,
                audio_senders_forced=forced,
                mic_track=page.get("micTrack"),
                peer_connections=page.get("peerConnections"),
                force_errors=page.get("forceErrors"),
                note="the avatar's audio is on no outbound sender, so nothing it says can be "
                "heard. Meet neither asked us for a microphone nor exposed an audio sender to "
                "put one on — check that the 'rtc' inject stage is enabled.",
            )
            return

        logger.info(
            "meet_watchdog.publish_state",
            mic_clones=mic_clones,
            camera_clones=camera_clones,
            audio_senders_seen=seen,
            audio_senders_forced=forced,
            mic_track=page.get("micTrack"),
            playout=playout,
            note="how the avatar's audio reached the wire: via getUserMedia (mic_clones) "
            "and/or by being forced onto Meet's sender (audio_senders_forced)",
        )

    # -- assessment --------------------------------------------------------

    def _assess(self, page: dict | None = None) -> WatchdogVerdict:
        """Decide whether audio has stopped arriving when it should not have.

        Deliberately a pure function of observable counters and the roster, with the clock
        as its only side channel — so it can be driven directly in a test without waiting on
        real time. ``page`` is optional for the same reason: the browser's own account is
        evidence when present, never a prerequisite.
        """
        loop = asyncio.get_running_loop()
        now = loop.time()

        if not self._bridge.is_joined or self._bridge.meet_state is not MeetState.JOINED:
            # Not in the call. Whatever is wrong, it is not this component's finding, and
            # the bridge is already reporting it.
            self._last_progress_at = None
            self._last_frames = -1
            return WatchdogVerdict(state=ComponentState.UNKNOWN, detail="not in the call")

        # An unpublished microphone is a *fact* the page reported, not an inference from
        # silence, so it is reported without waiting for a grace window and regardless of who
        # else is in the meeting. DEGRADED rather than UNHEALTHY for this component's usual
        # reason: the browser is genuinely in the call and the supervisor should not kill it.
        #
        # Both routes count. ``micClonesIssued`` is Meet asking us for a microphone;
        # ``audioSendersForced`` is us putting the track on Meet's sender regardless. Either
        # means the avatar is on the wire, and only neither is a fault — checking just the
        # first would report a fault for a session the enforcement had already rescued.
        if (
            page is not None
            and _has_publish_facts(page)
            and not page.get("micClonesIssued")
            and not page.get("audioSendersForced")
        ):
            return WatchdogVerdict(
                state=ComponentState.DEGRADED,
                detail="the avatar's audio is on no outbound sender, so it cannot be "
                "heard: Meet never requested a microphone and exposed no audio sender",
                others_present=len(self._bridge.roster.others),
            )

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
