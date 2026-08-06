"""The media watchdog, and the reconnect classifier.

The watchdog exists for one failure mode: **the browser stays alive while the audio stops.**
A crashed renderer closes the channel and the bridge rejoins; a denied join arrives as a
state message. But a suspended ``AudioContext``, or remote tracks that all ended without a
renegotiation, leave a tab that is running, a channel that is connected, a pacer that is
publishing — and every health check green.

So the tests here are mostly about **not** crying wolf. Silence alone must not trigger it: an
avatar alone in a meeting, or a candidate thinking for thirty seconds, is not a fault. The
trigger is silence *plus* someone else present, and that conjunction is the whole design.
"""

from __future__ import annotations

import asyncio

import pytest

from src.connectors.google_meet.exceptions import (
    BridgeAuthError,
    BridgeProtocolError,
    BridgeUnavailableError,
    BrowserCrashedError,
    BrowserLaunchError,
    GoogleAuthError,
    JoinTimeoutError,
    MeetConfigurationError,
    MeetingAdmissionError,
    MeetingEndedError,
    MeetUrlError,
    PlaywrightUnavailableError,
)
from src.connectors.google_meet.meeting.participants import MeetParticipant, MeetRoster
from src.connectors.google_meet.monitoring.watchdog import MediaWatchdog
from src.connectors.google_meet.reconnect.classify import build_policy, is_fatal
from src.connectors.google_meet.websocket.protocol import MeetState
from src.domain.health import ComponentState


class StubBridge:
    """The minimum surface the watchdog reads."""

    def __init__(self, *, joined: bool = True, others: int = 1) -> None:
        self.is_joined = joined
        self.meet_state = MeetState.JOINED if joined else None
        self.roster = MeetRoster(
            participants=(
                *(
                    MeetParticipant(page_id=f"p{i}", display_name=f"Person {i}")
                    for i in range(others)
                ),
                MeetParticipant(page_id="self", display_name="AI Avatar", is_self=True),
            ),
            self_name="AI Avatar",
        )


class StubSource:
    """A frame counter, which is the only thing the watchdog needs from ingest."""

    def __init__(self) -> None:
        self.frames_received = 0

    def receive(self, count: int = 1) -> None:
        self.frames_received += count


def _watchdog(bridge: StubBridge, source: StubSource, *, grace_s: float = 0.05) -> MediaWatchdog:
    return MediaWatchdog(bridge=bridge, source=source, interval_s=0.01, silence_grace_s=grace_s)


class TestNotCryingWolf:
    async def test_a_bridge_that_is_not_in_the_call_is_not_this_components_finding(self) -> None:
        """The bridge is already reporting whatever is wrong."""
        watchdog = _watchdog(StubBridge(joined=False), StubSource())
        assert watchdog._assess().state is ComponentState.UNKNOWN

    async def test_the_first_pass_has_no_baseline(self) -> None:
        """Claiming a fault here would flag every session at startup."""
        source = StubSource()
        watchdog = _watchdog(StubBridge(), source)

        first = watchdog._assess()  # establishes the frame count
        assert first.state is not ComponentState.DEGRADED

    async def test_arriving_audio_is_healthy(self) -> None:
        source = StubSource()
        watchdog = _watchdog(StubBridge(), source)
        watchdog._assess()

        source.receive()
        assert watchdog._assess().state is ComponentState.HEALTHY

    async def test_being_alone_makes_silence_correct(self) -> None:
        """An avatar that arrived before the candidate legitimately hears nothing."""
        watchdog = _watchdog(StubBridge(others=0), StubSource(), grace_s=0.0)
        watchdog._assess()
        await asyncio.sleep(0.02)

        verdict = watchdog._assess()
        assert verdict.state is ComponentState.HEALTHY
        assert verdict.detail == "alone in the meeting"

    async def test_the_grace_window_is_respected(self) -> None:
        """A candidate thinking for a moment must not look like a fault."""
        watchdog = _watchdog(StubBridge(), StubSource(), grace_s=60.0)
        watchdog._assess()
        await asyncio.sleep(0.02)
        assert watchdog._assess().state is ComponentState.HEALTHY

    async def test_the_clock_restarts_when_someone_arrives(self) -> None:
        """The grace window should start from when there was someone to hear, not from the
        join — otherwise an avatar that waited alone for a minute is flagged instantly."""
        bridge = StubBridge(others=0)
        watchdog = _watchdog(bridge, StubSource(), grace_s=0.05)
        watchdog._assess()
        await asyncio.sleep(0.06)
        watchdog._assess()  # still alone: clock held

        bridge.roster = StubBridge(others=1).roster
        assert watchdog._assess().state is ComponentState.HEALTHY


class TestCatchingTheRealFault:
    async def test_silence_with_others_present_degrades(self) -> None:
        watchdog = _watchdog(StubBridge(others=2), StubSource(), grace_s=0.02)
        watchdog._assess()
        await asyncio.sleep(0.05)

        verdict = watchdog._assess()
        assert verdict.state is ComponentState.DEGRADED
        assert verdict.others_present == 2
        assert "no conference audio" in (verdict.detail or "")

    async def test_it_degrades_rather_than_fails(self) -> None:
        """The signal is inferential — the roster comes from a machine-generated DOM — so a
        misread must not be able to kill a working session."""
        watchdog = _watchdog(StubBridge(), StubSource(), grace_s=0.02)
        watchdog._assess()
        await asyncio.sleep(0.05)

        verdict = watchdog._assess()
        assert verdict.state is ComponentState.DEGRADED
        assert verdict.state.is_serving

    async def test_recovery_is_reported(self) -> None:
        source = StubSource()
        watchdog = _watchdog(StubBridge(), source, grace_s=0.02)
        watchdog._assess()
        await asyncio.sleep(0.05)
        assert watchdog._assess().state is ComponentState.DEGRADED

        source.receive()
        assert watchdog._assess().state is ComponentState.HEALTHY


class TestWatchdogLifecycle:
    async def test_it_runs_and_stops(self) -> None:
        watchdog = _watchdog(StubBridge(), StubSource())
        await watchdog.start()
        await watchdog.start()  # idempotent
        await asyncio.sleep(0.03)
        await watchdog.stop()
        await watchdog.stop()  # idempotent

    async def test_health_reports_the_current_verdict(self) -> None:
        watchdog = _watchdog(StubBridge(), StubSource(), grace_s=0.01)
        await watchdog.start()
        await asyncio.sleep(0.05)
        try:
            assert watchdog.health().name == "google_meet_watchdog"
        finally:
            await watchdog.stop()


class TestReconnectClassification:
    """The decision that must be identical in both branches of the bridge's supervisor."""

    @pytest.mark.parametrize(
        "error",
        [
            PlaywrightUnavailableError("no browser"),
            MeetConfigurationError("no profile"),
            GoogleAuthError("no session"),
            MeetUrlError("bad code"),
            BridgeAuthError("bad token"),
        ],
    )
    def test_deployment_faults_are_fatal(self, error: Exception) -> None:
        """Every attempt reproduces these identically; retrying only delays the diagnosis."""
        assert is_fatal(error) is True

    @pytest.mark.parametrize(
        "error",
        [MeetingAdmissionError("denied"), MeetingEndedError("over")],
    )
    def test_decisions_made_by_someone_else_are_fatal(self, error: Exception) -> None:
        """A retry *could* succeed if a host relented — and it is still classed fatal, because
        an account that repeatedly asks to enter a meeting it was thrown out of looks like
        abuse, and losing the account breaks every session rather than one."""
        assert is_fatal(error) is True

    @pytest.mark.parametrize(
        "error",
        [
            BrowserCrashedError("renderer died"),
            BrowserLaunchError("out of memory"),
            BridgeUnavailableError("channel dropped"),
            BridgeProtocolError("bad frame"),
            JoinTimeoutError("nobody admitted us"),
            OSError("connection reset"),
        ],
    )
    def test_transient_faults_are_recoverable(self, error: Exception) -> None:
        """The state that caused these is gone once a fresh browser starts."""
        assert is_fatal(error) is False

    def test_the_backoff_is_slower_than_the_other_connectors(self) -> None:
        """A rejoin here closes a browser, clones a profile, launches Chromium, signs in,
        navigates, and may wait in a lobby. A 500 ms initial delay would spend the whole
        budget inside the time one attempt needs."""
        from src.infrastructure.reconnect import ReconnectPolicy

        policy = build_policy(max_attempts=5)
        assert policy.initial_delay_s > ReconnectPolicy().initial_delay_s
        assert policy.max_delay_s > ReconnectPolicy().max_delay_s

    def test_the_budget_is_finite(self) -> None:
        policy = build_policy(max_attempts=3)
        assert policy.exhausted(4) is True
        assert policy.exhausted(3) is False
