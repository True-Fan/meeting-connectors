"""M7 — a full Google Meet conversation, through the real shared pipeline.

**What is real here, and what is not.** Everything except Chromium and the avatar service:

* the ``PageBridgeServer``, its token check, and a real loopback WebSocket;
* the real wire codec, in both directions;
* the real ``MediaRouter``, ``AvatarClient``, ``DecodePipeline``, ``EchoGuard``, ``Pacer``,
  ``IdleFrameSource`` and ``MediaClock`` — the shared pipeline, unmodified, reused;
* the real ``MeetAudioSource``, ``ChromiumMediaSink``, ``VirtualCameraAdapter`` and
  ``VirtualMicrophoneAdapter``;
* the real ``GoogleMeetSessionFactory``, and the real ``MeetingService`` in the API test.

Faked: the browser (``FakeBrowserDriver``), the avatar agent (``FakeAvatarTransport``, which
streams canned fMP4), and the decoder (``FakeDecoder``, so no ffmpeg binary is needed).

The flow being proved is the one the connector exists for:

    page PCM ──▶ MeetAudioSource ──▶ EchoGuard ──▶ AvatarClient ──▶ fMP4
                                                                    │
                                                        DecodePipeline
                                                                    ▼
                                            I420 + PCM ──▶ Pacer ──▶ ChromiumMediaSink
                                                                    │
                                        VirtualCamera + VirtualMicrophone ──▶ page
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.config.settings import GoogleMeetSettings, Settings
from src.connectors.google_meet.browser.profile import ProfileManager
from src.connectors.google_meet.config import GoogleMeetConnectorConfig
from src.connectors.google_meet.session.google_meet_session import (
    GoogleMeetSession,
    GoogleMeetSessionFactory,
)
from src.connectors.google_meet.websocket.protocol import MeetState
from src.domain.health import ComponentState
from src.domain.ids import new_correlation_id, new_session_id
from src.domain.meeting import MeetingContext, MeetingPlatform
from src.domain.session import SessionContext, SessionState
from src.services.media.decode_pipeline import DecodePipeline
from src.services.media.router import MediaRouter
from tests.fakes.avatar import FakeAvatarTransport
from tests.fakes.decoder import FakeDecoder
from tests.fakes.meet_page import FakeBrowserDriver, joined_driver

CODE = "abc-defg-hij"
VIDEO = (320, 180)
FRAME_SAMPLES = 320  # 20 ms at 16 kHz
PCM_FRAME = b"\x11\x22" * FRAME_SAMPLES


@pytest.fixture
def meet_settings(tmp_path: Path) -> Settings:
    template = tmp_path / "profile"
    (template / "Default").mkdir(parents=True)
    (template / "Default" / "Cookies").write_bytes(b"cookie")
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        google_meet=GoogleMeetSettings(
            profile_dir=template,
            video_width=VIDEO[0],
            video_height=VIDEO[1],
            video_fps=25,
            publish_sample_rate_hz=48_000,
            bridge_ready_timeout_s=5.0,
            join_timeout_s=2.0,
            lobby_timeout_s=2.0,
            watchdog_interval_s=0.05,
        ),
    )


@pytest.fixture
def session_context() -> SessionContext:
    return SessionContext(
        session_id=new_session_id(),
        correlation_id=new_correlation_id(),
        meeting=MeetingContext(
            meeting_number=CODE,
            display_name="AI Avatar",
            platform=MeetingPlatform.GOOGLE_MEET,
        ),
    )


def _build_session(
    settings: Settings,
    driver: FakeBrowserDriver,
    session_context: SessionContext,
    *,
    avatar_response: bytes | None = None,
) -> tuple[GoogleMeetSession, FakeAvatarTransport, FakeDecoder]:
    """Wire a real session, substituting only the browser, the avatar and the decoder.

    Reaches into the built session to swap the avatar transport and decoder rather than
    threading two more overrides through the factory. That is deliberate: the factory's
    production wiring is what this test is verifying, so it must run unchanged — the point is
    that ``GoogleMeetSessionFactory.build`` composes the real shared pipeline, and adding
    override hooks for this test would make the assertion weaker rather than stronger.
    """
    config = GoogleMeetConnectorConfig.from_settings(settings)
    factory = GoogleMeetSessionFactory(
        config=config,
        driver_factory=lambda: driver,
        profiles=ProfileManager(template=config.require_configured()),
    )
    session = factory.build(session_context)

    ctx = session_context.frame_context()
    transport = FakeAvatarTransport(ctx=ctx, response=avatar_response)
    decoder = FakeDecoder(
        ctx=ctx,
        video_format=config.video_format,
        audio_format=config.publish_audio_format,
        video_per_chunk=2,
        audio_per_chunk=2,
    )

    router: MediaRouter = session.router
    avatar_client = router._avatar
    avatar_client._transport = transport
    router._decode = DecodePipeline(decoder=decoder, ctx=ctx)

    return session, transport, decoder


async def _wait_for(predicate, *, timeout_s: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition was never met")
        await asyncio.sleep(0.01)


# --------------------------------------------------------------------------- #
# The full round trip
# --------------------------------------------------------------------------- #


class TestFullConversation:
    async def test_conference_audio_reaches_the_avatar(
        self, meet_settings, session_context
    ) -> None:
        driver = joined_driver(auto_page=True)
        session, transport, _ = _build_session(meet_settings, driver, session_context)
        try:
            await session.start()
            for _ in range(3):
                await driver.page.send_audio(PCM_FRAME)  # type: ignore[union-attr]

            await _wait_for(lambda: len(transport.sent_pcm) >= 3)

            # Byte-for-byte: no resample, no reframe, no conversion anywhere between the
            # browser's AudioWorklet and the avatar agent.
            assert transport.sent_pcm[0] == PCM_FRAME
            assert all(len(pcm) == FRAME_SAMPLES * 2 for pcm in transport.sent_pcm[:3])
        finally:
            await session.stop()

    async def test_the_avatar_receives_the_fixed_contract_format(
        self, meet_settings, session_context
    ) -> None:
        """``AvatarClient.send`` asserts the format rather than converting, so a frame that
        reaches it at all is proof the contract held end to end.

        Asserted through the client rather than through a handshake: no connector in this
        repository calls ``AvatarClient.start()``, so the hello is never exchanged during a
        session — see the note in the changeset summary. That is pre-existing shared
        behaviour, identical for Zoom and Teams, and not something this connector changes.
        """
        driver = joined_driver(auto_page=True)
        session, transport, _ = _build_session(meet_settings, driver, session_context)
        try:
            await session.start()
            await driver.page.send_audio(PCM_FRAME)  # type: ignore[union-attr]
            await _wait_for(lambda: len(transport.sent_pcm) >= 1)

            source_format = session._source.audio_format
            assert source_format.sample_rate_hz == 16_000
            assert source_format.channels == 1
            assert str(source_format.sample_format) == "s16le"
        finally:
            await session.stop()

    async def test_the_avatars_video_reaches_the_page_as_the_synthetic_camera(
        self, meet_settings, session_context
    ) -> None:
        driver = joined_driver(auto_page=True)
        session, _, _ = _build_session(meet_settings, driver, session_context)
        try:
            await session.start()
            await driver.page.send_audio(PCM_FRAME)  # type: ignore[union-attr]

            page = driver.page
            await _wait_for(lambda: len(page.received_video) > 0, timeout_s=5.0)  # type: ignore[union-attr]

            width, height, size = page.received_video[0]  # type: ignore[union-attr]
            assert (width, height) == VIDEO
            assert size == VIDEO[0] * VIDEO[1] * 3 // 2
        finally:
            await session.stop()

    async def test_the_avatars_audio_reaches_the_page_as_the_synthetic_microphone(
        self, meet_settings, session_context
    ) -> None:
        driver = joined_driver(auto_page=True)
        session, _, _ = _build_session(meet_settings, driver, session_context)
        try:
            await session.start()
            await driver.page.send_audio(PCM_FRAME)  # type: ignore[union-attr]

            page = driver.page
            await _wait_for(lambda: len(page.received_audio) > 0, timeout_s=5.0)  # type: ignore[union-attr]

            # 20 ms at the configured 48 kHz publish rate, mono s16le.
            assert len(page.received_audio[0]) == 48_000 // 50 * 2  # type: ignore[union-attr]
        finally:
            await session.stop()

    async def test_the_camera_publishes_continuously_even_while_the_avatar_is_silent(
        self, meet_settings, session_context
    ) -> None:
        """The Pacer's idle continuity, unmodified and reused.

        It matters more here than on the other connectors: the synthetic camera track is
        driven frame by frame, so if frames stop the canvas stops changing and Meet publishes
        a still image — which reads as a broken connection rather than as someone listening.
        """
        driver = joined_driver(auto_page=True)
        session, _, _ = _build_session(meet_settings, driver, session_context)
        try:
            await session.start()
            # No inbound audio at all: the avatar has nothing to say.
            page = driver.page
            await _wait_for(lambda: len(page.received_video) >= 3, timeout_s=5.0)  # type: ignore[union-attr]

            assert all(size > 0 for _, _, size in page.received_video)  # type: ignore[union-attr]
        finally:
            await session.stop()


# --------------------------------------------------------------------------- #
# Session semantics
# --------------------------------------------------------------------------- #


class TestAttendance:
    """The ledger, through the real factory wiring and the real page bridge.

    What these prove that the unit tests cannot: that ``GoogleMeetSessionFactory`` actually
    registers the ledger on the roster listener, and that a ``PARTICIPANTS`` message crossing a
    real socket reaches it. The unit tests own the diffing logic; this owns the wiring.
    """

    async def test_the_factory_wires_the_ledger_to_the_roster_stream(
        self, meet_settings, session_context
    ) -> None:
        driver = joined_driver(auto_page=True)
        session, _, _ = _build_session(meet_settings, driver, session_context)
        try:
            await session.start()
            page = driver.page
            assert page is not None

            await page.send_participants(["Aarav Sharma", "Priya Menon", "AI Avatar"])
            await _wait_for(lambda: session.attendance.snapshot().scans > 0)

            snapshot = session.attendance.snapshot()
            assert {r.label for r in snapshot.present} == {"Aarav Sharma", "Priya Menon"}, (
                "the avatar's own entry must not be counted as an attendee"
            )
        finally:
            await session.stop()

    async def test_somebody_leaving_is_remembered(self, meet_settings, session_context) -> None:
        driver = joined_driver(auto_page=True)
        session, _, _ = _build_session(meet_settings, driver, session_context)
        try:
            await session.start()
            page = driver.page
            assert page is not None

            await page.send_participants(["Aarav Sharma", "Priya Menon", "AI Avatar"])
            await _wait_for(lambda: len(session.attendance.snapshot().present) == 2)

            await page.send_participants(["Aarav Sharma", "AI Avatar"])
            await _wait_for(lambda: len(session.attendance.snapshot().departed) == 1)

            snapshot = session.attendance.snapshot()
            assert {r.label for r in snapshot.present} == {"Aarav Sharma"}
            assert {r.label for r in snapshot.departed} == {"Priya Menon"}
        finally:
            await session.stop()

    async def test_the_agent_is_told_who_is_in_the_meeting(
        self, meet_settings, session_context
    ) -> None:
        """The live failure this feature exists for.

        The bridge held the right names, the endpoint served them, and the agent still answered
        "I don't have access to your meeting details or a list of participants" — because nothing
        crossed the avatar socket. This asserts the frame arrives, through the real factory
        wiring, on the real page bridge.
        """
        import json

        driver = joined_driver(auto_page=True)
        session, transport, _ = _build_session(meet_settings, driver, session_context)
        try:
            await session.start()
            page = driver.page
            assert page is not None

            await page.send_participants(["dev Choudhary", "AI Avatar"])

            def briefed() -> bool:
                return any(
                    json.loads(p).get("kind") == "meeting_context"
                    for p in transport.sent_control
                )

            await _wait_for(briefed, timeout_s=6.0)

            brief = next(
                json.loads(p)
                for p in transport.sent_control
                if json.loads(p).get("kind") == "meeting_context"
            )
            assert "dev Choudhary" in brief["text"]
            assert brief["topic"] == "attendance"
        finally:
            await session.stop()

    async def test_the_ledger_is_absent_when_the_feature_is_off(
        self, meet_settings, session_context
    ) -> None:
        """Off means no ledger at all, not an empty one — so the API can say "disabled"."""
        meet_settings.google_meet.attendance_enabled = False
        driver = joined_driver(auto_page=True)
        session, _, _ = _build_session(meet_settings, driver, session_context)
        try:
            await session.start()
            assert session.attendance is None
        finally:
            await session.stop()


class TestSessionSemantics:
    async def test_both_legs_move_together(self, meet_settings, session_context) -> None:
        """One browser tab is the participant: there is no state in which Meet ingest works
        while Meet egress does not."""
        driver = joined_driver(auto_page=True)
        session, _, _ = _build_session(meet_settings, driver, session_context)
        try:
            await session.start()
            ingest, publish = session.leg_states()
            assert ingest is publish is ComponentState.HEALTHY
        finally:
            await session.stop()

    async def test_the_session_derives_active_from_its_legs(
        self, meet_settings, session_context
    ) -> None:
        """Reusing the shared derivation, with no new state and no new supervisor."""
        from src.domain.session import derive_state

        driver = joined_driver(auto_page=True)
        session, _, _ = _build_session(meet_settings, driver, session_context)
        try:
            await session.start()
            assert derive_state(*session.leg_states()) is SessionState.ACTIVE
        finally:
            await session.stop()

    async def test_health_names_the_bridge_the_publisher_and_the_watchdog_separately(
        self, meet_settings, session_context
    ) -> None:
        """``leg_states`` collapses to a pair because ``derive_state`` takes one, but an
        operator debugging a silent avatar needs to know *which* component is unhappy."""
        driver = joined_driver(auto_page=True)
        session, _, _ = _build_session(meet_settings, driver, session_context)
        try:
            await session.start()
            names = {c.name for c in session.health().components}

            assert "google_meet_ingest" in names
            assert "google_meet_publisher" in names
            assert "google_meet_watchdog" in names
            assert "avatar_client" in names
        finally:
            await session.stop()

    async def test_the_publisher_reports_its_publish_counts(
        self, meet_settings, session_context
    ) -> None:
        """A count stuck at zero while the bridge is healthy is the only observable for Meet
        holding a track it was never told to publish."""
        driver = joined_driver(auto_page=True)
        session, _, _ = _build_session(meet_settings, driver, session_context)
        try:
            await session.start()
            # Parsed rather than substring-matched: "video=0" is also a substring of
            # "dropped_video=0", so a naive check passes on a session that published nothing.
            await _wait_for(lambda: _publish_counts(session).get("video", 0) > 0, timeout_s=5.0)

            counts = _publish_counts(session)
            assert counts["video"] > 0
            assert "audio" in counts
            assert counts["dropped_video"] == 0
        finally:
            await session.stop()

    async def test_the_watchdog_downgrades_a_healthy_pair_but_cannot_upgrade_a_broken_one(
        self, meet_settings, session_context
    ) -> None:
        """An inference must not be able to declare a broken bridge healthy."""
        driver = joined_driver(auto_page=True)
        session, _, _ = _build_session(meet_settings, driver, session_context)
        try:
            await session.start()
            await driver.page.send_state(MeetState.EJECTED)  # type: ignore[union-attr]
            await _wait_for(lambda: session.leg_states()[0] is ComponentState.UNHEALTHY)

            assert session.leg_states() == (
                ComponentState.UNHEALTHY,
                ComponentState.UNHEALTHY,
            )
        finally:
            await session.stop()

    async def test_stop_leaves_the_meeting_and_closes_the_browser(
        self, meet_settings, session_context
    ) -> None:
        driver = joined_driver(auto_page=True)
        session, _, _ = _build_session(meet_settings, driver, session_context)
        await session.start()
        await session.stop()

        assert any("Leave call" in selector for selector in driver.clicked)
        assert driver.stopped == 1

    async def test_stop_is_idempotent(self, meet_settings, session_context) -> None:
        driver = joined_driver(auto_page=True)
        session, _, _ = _build_session(meet_settings, driver, session_context)
        await session.start()
        await session.stop()
        await session.stop()


# --------------------------------------------------------------------------- #
# Echo suppression
# --------------------------------------------------------------------------- #


class TestEchoSuppression:
    async def test_the_speaking_gate_is_open_so_the_avatar_can_be_interrupted(
        self, meet_settings, session_context
    ) -> None:
        """The gate would suppress the interruption along with the echo, and there is no echo.

        It drops *every* inbound frame while the avatar publishes and cannot tell its own echo
        from a person talking over it. On this connector the WebRTC tap is inbound-only, so
        the avatar's audio never enters it — the gate is catching nothing and would cost the
        interruption. Not strict either: strict means "the gate is the only defence", which an
        open gate is not.
        """
        driver = joined_driver(auto_page=True)
        session, _, _ = _build_session(meet_settings, driver, session_context)
        guard = session.router._echo_guard

        assert guard.is_strict is False
        guard.note_publishing(1_000_000)
        assert guard.is_gate_open(1_000_000) is False

    async def test_no_own_participant_is_ever_set(
        self, meet_settings, session_context
    ) -> None:
        """There is nothing to set: the WebRTC tap is inbound-only, so the avatar's own audio
        never enters it. Arming the identity filter would only invite a false suppression."""
        driver = joined_driver(auto_page=True)
        session, _, _ = _build_session(meet_settings, driver, session_context)
        try:
            await session.start()
            assert session.router._echo_guard.own_user_id is None
        finally:
            await session.stop()


# --------------------------------------------------------------------------- #
# Through the service layer
# --------------------------------------------------------------------------- #


class TestThroughMeetingService:
    async def test_a_session_is_created_and_supervised_platform_blind(
        self, meet_settings
    ) -> None:
        """``MeetingService`` and ``SessionSupervisor`` are reused with no Meet knowledge."""
        from src.infrastructure.metrics import MetricsCollector
        from src.services.meeting.connector_registry import ConnectorRegistry
        from src.services.meeting.service import CreateSessionCommand, MeetingService
        from src.services.session.lifecycle import SessionLifecycle
        from src.services.session.registry import SessionRegistry
        from src.services.session.supervisor import SessionSupervisor

        config = GoogleMeetConnectorConfig.from_settings(meet_settings)
        driver = joined_driver(auto_page=True)
        factory = GoogleMeetSessionFactory(
            config=config,
            driver_factory=lambda: driver,
            profiles=ProfileManager(template=config.require_configured()),
        )

        registry = SessionRegistry()
        lifecycle = SessionLifecycle()
        metrics = MetricsCollector(histogram_capacity=64)
        supervisor = SessionSupervisor(
            registry=registry, lifecycle=lifecycle, metrics=metrics, poll_interval_s=0.05
        )
        service = MeetingService(
            registry=registry,
            lifecycle=lifecycle,
            supervisor=supervisor,
            connectors=ConnectorRegistry().register(MeetingPlatform.GOOGLE_MEET, factory),
            metrics=metrics,
        )

        session = await service.create_session(
            CreateSessionCommand(meeting_number=CODE, platform=MeetingPlatform.GOOGLE_MEET)
        )
        try:
            assert session.meeting.platform is MeetingPlatform.GOOGLE_MEET
            # The supervisor derives ACTIVE from the leg pair with no platform knowledge.
            await _wait_for(lambda: session.state is SessionState.ACTIVE)
        finally:
            await service.stop_session(session.session_id)

        assert session.state is SessionState.STOPPED

    async def test_the_api_reports_meet_audio_as_attached_once_running(
        self, meet_settings
    ) -> None:
        """``_audio_attached`` needed no change: its non-Zoom branch already answers this
        correctly, because one join covers both directions."""
        from src.api.dto import SessionResponse

        context = SessionContext(
            session_id=new_session_id(),
            correlation_id=new_correlation_id(),
            meeting=MeetingContext(
                meeting_number=CODE,
                display_name="AI Avatar",
                platform=MeetingPlatform.GOOGLE_MEET,
            ),
            state=SessionState.ACTIVE,
        )
        response = SessionResponse.from_domain(context)

        assert response.platform is MeetingPlatform.GOOGLE_MEET
        assert response.audio_attached is True
        # And a Meet session has no meeting UUID, which is what the Zoom branch keys on.
        assert context.meeting.meeting_uuid is None


def _publisher_detail(session: GoogleMeetSession) -> str | None:
    component = session.health().component("google_meet_publisher")
    return component.detail if component is not None else None


def _publish_counts(session: GoogleMeetSession) -> dict[str, int]:
    """Parse the publisher's ``key=value`` health detail into numbers."""
    detail = _publisher_detail(session) or ""
    return {
        key: int(value)
        for key, _, value in (part.partition("=") for part in detail.split())
        if value.isdigit()
    }


class TestADeadAvatarIsNotAHealthySession:
    """Regression for a silent failure seen in a live run.

    The log read:

        router.avatar_unreachable   error='avatar handshake reply was not JSON'
        session.transition          from_state=joining to_state=active

    The avatar agent was unreachable — no audio in, and only grey idle frames out — and the
    session reported ACTIVE four lines later, because ``leg_states`` looked only at the browser.
    """

    async def test_an_unhealthy_avatar_degrades_the_session(
        self, meet_settings, session_context
    ) -> None:
        driver = joined_driver(auto_page=True)
        session, transport, _ = _build_session(meet_settings, driver, session_context)
        try:
            await session.start()
            await _wait_for(lambda: session.leg_states()[0] is ComponentState.HEALTHY)

            # The avatar dies the way an unreachable agent does: the transport goes unhealthy.
            transport.fail("handshake reply was not JSON")
            session_context.state = SessionState.ACTIVE

            assert session.leg_states() == (
                ComponentState.DEGRADED,
                ComponentState.DEGRADED,
            )
            from src.domain.session import derive_state

            assert derive_state(*session.leg_states()) is SessionState.DEGRADED
        finally:
            await session.stop()

    async def test_a_not_yet_started_avatar_is_not_treated_as_a_failure(
        self, meet_settings, session_context
    ) -> None:
        """``UNKNOWN`` is the transport's "not started yet", which every session passes through
        between ``start()`` creating the router task and that task completing its handshake.
        Reading it as a fault would degrade the first tick of every healthy session."""
        driver = joined_driver(auto_page=True)
        session, _, _ = _build_session(meet_settings, driver, session_context)
        try:
            session_context.state = SessionState.ACTIVE
            # Deliberately before start(): the avatar has not connected, so it reports UNKNOWN.
            assert session.leg_states()[0] is not ComponentState.DEGRADED
        finally:
            await session.stop()

    async def test_the_degrade_waits_until_the_session_leaves_joining(
        self, meet_settings, session_context
    ) -> None:
        """``domain.session`` permits ``JOINING -> ACTIVE`` but not ``JOINING -> DEGRADED``, so
        reporting a degraded pair too early raises inside the supervisor's poll loop."""
        from src.domain.session import allowed_transitions

        assert SessionState.DEGRADED not in allowed_transitions(SessionState.JOINING)

        driver = joined_driver(auto_page=True)
        session, transport, _ = _build_session(meet_settings, driver, session_context)
        try:
            await session.start()
            transport.fail("dead")
            session_context.state = SessionState.JOINING

            assert session.leg_states()[0] is not ComponentState.DEGRADED
        finally:
            await session.stop()


class TestSpeakerAttribution:
    """Who is speaking, through the real factory wiring and the real page bridge.

    The unit tests own the bookkeeping. What these own is the wiring — and specifically the two
    things a live run showed the unit tests could not see: that a speaking edge crossing a real
    socket reaches the tracker, and that a session ends up with **exactly one** thing pushing
    standing context to the agent.
    """

    async def test_a_speaking_edge_reaches_the_tracker(
        self, meet_settings, session_context
    ) -> None:
        driver = joined_driver(auto_page=True)
        session, _, _ = _build_session(meet_settings, driver, session_context)
        try:
            await session.start()
            page = driver.page
            assert page is not None

            await page.send_speaker(name="Priya Menon", speaking=True)
            await _wait_for(lambda: session.speakers.current_speaker() == "Priya Menon")
        finally:
            await session.stop()

    async def test_the_speaker_is_named_in_a_two_person_call_with_no_page_attribution(
        self, meet_settings, session_context
    ) -> None:
        """**The case the first live run failed.** Meet renders remote *audio* on elements outside
        the participant tile, so an audio stream's id never appears there — every turn came back
        "Someone" while the levels were measured perfectly. With one other person in the room there
        is exactly one person it can have been, and no markup is consulted to say so.
        """
        driver = joined_driver(auto_page=True)
        session, _, _ = _build_session(meet_settings, driver, session_context)
        try:
            await session.start()
            page = driver.page
            assert page is not None

            await page.send_participants(["dev Choudhary", "AI Avatar"])
            await _wait_for(lambda: len(session.attendance.snapshot().present) == 1)

            # Exactly what the page sends when it can hear somebody and cannot name them.
            await page.send_speaker(name=None, speaking=True)
            await _wait_for(lambda: session.speakers.current_speaker() == "dev Choudhary")

            (turn,) = session.speakers.snapshot().turns
            assert turn.inferred is True, "reached by elimination, and recorded as such"
        finally:
            await session.stop()

    async def test_a_session_has_exactly_one_context_pusher(
        self, meet_settings, session_context
    ) -> None:
        """**The regression this prevents was reported as attendance breaking.**

        An agent keeps one slot for standing context. With both features on there were two
        announcers writing to it, and the speaker one — pushing every few seconds against a roster
        that changes once a meeting — evicted the other: asked who was in the meeting, the avatar
        answered "Someone is present in the meeting". So the speaker brief travels *inside* the
        attendance brief, and the second pusher exists only when the first does not.
        """
        driver = joined_driver(auto_page=True)
        session, _, _ = _build_session(meet_settings, driver, session_context)

        # Reaching into the built session, because the wiring *is* the property under test —
        # the same argument ``_build_session`` makes for swapping the transport in place.
        announcer = session._announcer
        speaker_announcer = session._speaker_announcer

        assert announcer is not None, "attendance still pushes"
        assert speaker_announcer is None, "and it is the only pusher"

    async def test_the_brief_names_both_who_is_here_and_who_is_talking(
        self, meet_settings, session_context
    ) -> None:
        import json

        driver = joined_driver(auto_page=True)
        session, transport, _ = _build_session(meet_settings, driver, session_context)
        try:
            await session.start()
            page = driver.page
            assert page is not None

            await page.send_participants(["dev Choudhary", "AI Avatar"])
            await page.send_speaker(name="dev Choudhary", speaking=True)

            def briefed() -> bool:
                return any(
                    "is speaking right now" in json.loads(p).get("text", "")
                    for p in transport.sent_control
                    if json.loads(p).get("kind") == "meeting_context"
                )

            await _wait_for(briefed, timeout_s=8.0)

            brief = next(
                json.loads(p)["text"]
                for p in reversed(transport.sent_control)
                if json.loads(p).get("kind") == "meeting_context"
            )
            assert "Currently in the meeting" in brief
            assert "dev Choudhary is speaking right now" in brief
        finally:
            await session.stop()


class TestTranscript:
    """Who said what, through the real factory wiring and the real page bridge.

    The gap this closes is the one a live run made obvious: asked "what did they ask you?", the
    avatar could only say it did not know. Attribution knows who is talking and not what was said;
    the agent's transcription knows what was said and receives one mixed stream, so it can never
    attribute it. Meet's captions have both, and these prove the path from that panel to the brief.
    """

    async def test_a_caption_line_reaches_the_transcript(
        self, meet_settings, session_context
    ) -> None:
        driver = joined_driver(auto_page=True)
        session, _, _ = _build_session(meet_settings, driver, session_context)
        try:
            await session.start()
            page = driver.page
            assert page is not None

            await page.send_caption(speaker="Dev Choudhary", text="Tell me about Delhi")
            await _wait_for(lambda: session.transcript.count == 1)

            (line,) = session.transcript.snapshot().lines
            assert line.speaker == "Dev Choudhary"
            assert line.text == "Tell me about Delhi"
        finally:
            await session.stop()

    async def test_the_brief_tells_the_agent_who_asked_what(
        self, meet_settings, session_context
    ) -> None:
        """**The answer to "what did they ask you?" arriving where the agent can use it.**

        One frame carries who is here, who is talking, and what each person said — because an agent
        has one slot for standing context, and three pushers would evict each other exactly as two
        already did.
        """
        import json

        driver = joined_driver(auto_page=True)
        session, transport, _ = _build_session(meet_settings, driver, session_context)
        try:
            await session.start()
            page = driver.page
            assert page is not None

            await page.send_participants(["Dev Choudhary", "AI Avatar"])
            await page.send_caption(speaker="Dev Choudhary", text="Tell me about India Gate")

            def briefed() -> bool:
                return any(
                    "Tell me about India Gate" in json.loads(p).get("text", "")
                    for p in transport.sent_control
                    if json.loads(p).get("kind") == "meeting_context"
                )

            await _wait_for(briefed, timeout_s=8.0)

            brief = next(
                json.loads(p)["text"]
                for p in reversed(transport.sent_control)
                if json.loads(p).get("kind") == "meeting_context"
            )
            assert "Currently in the meeting" in brief, "attendance is still there"
            assert "Dev Choudhary: Tell me about India Gate" in brief
        finally:
            await session.stop()
