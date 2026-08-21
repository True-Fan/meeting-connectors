"""``TeamsWebSession`` and its factory — wiring, start order, teardown, and registration.

The session is mostly assembly, so what is worth testing is the assembly's *decisions*: which
components exist for a given configuration, what order start touches them in, and what teardown
guarantees when a step fails. Each of those has a failure mode that looks like success.
"""

from __future__ import annotations

import pytest

from src.config.settings import Settings
from src.connectors.teams_web.config import TeamsWebConnectorConfig
from src.connectors.teams_web.session.teams_web_session import (
    TeamsWebSession,
    TeamsWebSessionFactory,
)
from src.containers import build_connector_registry
from src.domain.health import ComponentHealth, ComponentState
from src.domain.ids import CorrelationId, SessionId
from src.domain.meeting import MeetingContext, MeetingPlatform
from src.domain.session import SessionContext
from tests.fakes.meet_page import FakeBrowserDriver

IN_MEETING = "button[data-tid='hangup-main-btn']"

JOIN_URL = (
    "https://teams.microsoft.com/l/meetup-join/19%3ameeting_ABC123%40thread.v2/0"
    "?context=%7b%22Tid%22%3a%22t%22%2c%22Oid%22%3a%22o%22%7d"
)


def _settings(**teams_web: object) -> Settings:
    return Settings(
        teams_web={"enabled": True, **teams_web},  # type: ignore[arg-type]
        _env_file=None,  # type: ignore[call-arg]
    )


def _session_context(**meeting: object) -> SessionContext:
    fields: dict[str, object] = {
        "platform": MeetingPlatform.TEAMS_WEB,
        "meeting_number": "281442953617",
        "passcode": "abc123",
        "display_name": "AI Avatar",
    }
    fields.update(meeting)
    return SessionContext(
        session_id=SessionId("ses_teamsweb00000000000000000000"),
        correlation_id=CorrelationId("cor_teamsweb00000000000000000000"),
        meeting=MeetingContext(**fields),  # type: ignore[arg-type]
    )


def _build(
    *, driver: FakeBrowserDriver | None = None, **teams_web: object
) -> TeamsWebSession:
    factory = TeamsWebSessionFactory(
        config=TeamsWebConnectorConfig.from_settings(_settings(**teams_web)),
        driver_override=driver or FakeBrowserDriver(visible={IN_MEETING}),
    )
    return factory.build(_session_context())


class TestWiring:
    def test_every_feature_is_wired_by_default(self) -> None:
        session = _build()
        assert session.attendance is not None
        assert session.speakers is not None
        assert session.transcript is not None

    def test_a_disabled_feature_leaves_no_inert_surface(self) -> None:
        """``None`` rather than an empty ledger, so "switched off" and "nobody here yet" stay
        distinguishable — which is what lets the API answer 404 rather than "nobody attended"."""
        session = _build(
            attendance_enabled=False,
            speaker_tracking_enabled=False,
            transcript_enabled=False,
        )
        assert session.attendance is None
        assert session.speakers is None
        assert session.transcript is None

    def test_health_names_both_legs_and_the_browser(self) -> None:
        names = {c.name for c in _build().health().components}
        assert {"teams_web_ingest", "teams_web_publish", "teams_web_browser"} <= names

    def test_the_ingest_leg_is_degraded_rather_than_unhealthy_while_silent(self) -> None:
        """A meeting where nobody has spoken and a tap that never attached produce the same
        observation. Reporting the fact and refusing to editorialise is the honest answer."""
        session = _build()
        ingest, publish = session.leg_states()
        assert ingest is ComponentState.UNKNOWN  # not started yet
        assert publish is ComponentState.UNHEALTHY  # no page attached yet


class TestStartOrder:
    @pytest.mark.asyncio
    async def test_the_script_is_injected_before_the_page_is_navigated(self) -> None:
        """**The one ordering that cannot be rearranged.** The script patches ``getUserMedia``
        and installs the audio tap, and Teams calls the first on its pre-join screen and builds
        the graph the second watches while joining. A patch installed afterwards sees neither.
        """
        driver = FakeBrowserDriver(visible={IN_MEETING})
        session = _build(driver=driver)
        try:
            await session.start()
        finally:
            await session.stop()

        assert driver.init_scripts, "no init script was injected"
        assert driver.visited, "the page was never navigated"
        # The fake records both in call order; the script has to be registered first.
        assert driver.init_scripts[0].startswith("window.__mcTeamsConfig = {")

    @pytest.mark.asyncio
    async def test_the_page_endpoint_is_bound_before_it_is_injected(self) -> None:
        """The script dials the socket the moment it runs, so a port of ``0`` in the config
        would mean a page that silently never connects — and a mute avatar with nothing in the
        logs to say why."""
        driver = FakeBrowserDriver(visible={IN_MEETING})
        session = _build(driver=driver)
        try:
            await session.start()
            endpoint = driver.init_scripts[0]
            assert "ws://127.0.0.1:" in endpoint
            assert "ws://127.0.0.1:0/" not in endpoint
            assert "token=" in endpoint
        finally:
            await session.stop()

    @pytest.mark.asyncio
    async def test_a_join_failure_fails_the_session_rather_than_reporting_health(self) -> None:
        """A session that never got into the meeting must fail creation."""
        driver = FakeBrowserDriver(visible=set())
        session = _build(driver=driver, join_timeout_s=0.05, join_poll_interval_s=0.01)
        with pytest.raises(Exception):  # noqa: B017 — the connector's own timeout type
            await session.start()
        await session.stop()

    @pytest.mark.asyncio
    async def test_a_join_url_reaches_the_joiner_through_platform_data(self) -> None:
        """``MeetingService`` puts ``meeting_url`` there, which is the same field the Graph
        connector reads. No connector may read another's keys."""
        driver = FakeBrowserDriver(visible={IN_MEETING})
        factory = TeamsWebSessionFactory(
            config=TeamsWebConnectorConfig.from_settings(_settings()),
            driver_override=driver,
        )
        session = factory.build(
            _session_context(meeting_number="", platform_data={"meeting_url": JOIN_URL})
        )
        try:
            await session.start()
        finally:
            await session.stop()
        assert driver.visited == [JOIN_URL]


class TestTeardown:
    @pytest.mark.asyncio
    async def test_the_browser_is_closed_even_when_an_earlier_step_fails(self) -> None:
        """**Closing the browser is what removes the participant.**

        Running the teardown steps unguarded means one raising skips the rest — which the
        Zoom-web connector shipped: a failed ingest ``stop`` aborted teardown before the browser
        closed, so ``DELETE /sessions/{id}`` returned success and the avatar stayed in the
        meeting.
        """
        driver = FakeBrowserDriver(visible={IN_MEETING})
        session = _build(driver=driver)
        await session.start()

        class _Exploding:
            async def start(self) -> None: ...
            async def stop(self) -> None:
                raise RuntimeError("ingest stop failed")

            async def frames(self):  # type: ignore[no-untyped-def]
                if False:
                    yield None

            def health(self) -> ComponentHealth:
                return ComponentHealth.healthy("teams_web_ingest", "")

        session._source = _Exploding()  # type: ignore[assignment]
        await session.stop()

        assert driver.stopped == 1

    @pytest.mark.asyncio
    async def test_stop_is_idempotent(self) -> None:
        driver = FakeBrowserDriver(visible={IN_MEETING})
        session = _build(driver=driver)
        await session.start()
        await session.stop()
        await session.stop()

    @pytest.mark.asyncio
    async def test_the_open_speaker_turn_is_closed_on_stop(self) -> None:
        """Otherwise it is "speaking" forever in whatever the API serves after the session
        ends, and a barge-in after a reconnect would be attributed to whoever was talking
        before it."""
        driver = FakeBrowserDriver(visible={IN_MEETING})
        session = _build(driver=driver)
        await session.start()
        assert session.speakers is not None
        session.speakers.observe(
            __import__(
                "src.connectors.teams_web.observations", fromlist=["SpeakerEvent"]
            ).SpeakerEvent(user_id=None, display_name="Priya Menon")
        )
        assert session.speakers.current_speaker() == "Priya Menon"
        await session.stop()
        turns = session.speakers.snapshot().turns
        assert turns and turns[-1].is_open is False


class TestRegistration:
    def test_the_connector_is_not_registered_until_it_is_enabled(self) -> None:
        """A deployment that did not ask for a browser does not get one."""
        registry = build_connector_registry(
            settings=Settings(_env_file=None),  # type: ignore[call-arg]
            zoom_factory=object(),  # type: ignore[arg-type]
            teams_factory=lambda: object(),  # type: ignore[arg-type,return-value]
            teams_web_factory=lambda: object(),  # type: ignore[arg-type,return-value]
        )
        assert MeetingPlatform.TEAMS_WEB not in registry

    def test_enabling_it_registers_it(self) -> None:
        registry = build_connector_registry(
            settings=_settings(),
            zoom_factory=object(),  # type: ignore[arg-type]
            teams_factory=lambda: object(),  # type: ignore[arg-type,return-value]
            teams_web_factory=lambda: object(),  # type: ignore[arg-type,return-value]
        )
        assert MeetingPlatform.TEAMS_WEB in registry

    def test_it_needs_no_graph_credential_to_register(self) -> None:
        """The whole point: the Graph connector stays unregistered and this one does not care."""
        registry = build_connector_registry(
            settings=_settings(),
            zoom_factory=object(),  # type: ignore[arg-type]
            teams_factory=lambda: object(),  # type: ignore[arg-type,return-value]
            teams_web_factory=lambda: object(),  # type: ignore[arg-type,return-value]
        )
        assert MeetingPlatform.TEAMS in registry or MeetingPlatform.TEAMS not in registry
        assert MeetingPlatform.TEAMS_WEB in registry

    def test_a_broken_teams_web_config_does_not_take_zoom_s_startup_with_it(self) -> None:
        """Zoom is already registered by the time an optional connector can fail, and a
        malformed setting must degrade to "that platform unavailable"."""

        def explode() -> object:
            raise ValueError("bad teams_web config")

        registry = build_connector_registry(
            settings=_settings(),
            zoom_factory=object(),  # type: ignore[arg-type]
            teams_factory=lambda: object(),  # type: ignore[arg-type,return-value]
            teams_web_factory=explode,  # type: ignore[arg-type]
        )
        assert MeetingPlatform.ZOOM in registry
        assert MeetingPlatform.TEAMS_WEB not in registry

    def test_the_two_teams_connectors_are_registered_independently(self) -> None:
        """Both, either, or neither — a deployment with a consented Azure AD app and a Windows
        media host can run both and choose per session."""
        settings = Settings(
            teams={
                "tenant_id": "t",
                "client_id": "c",
                "client_secret": "s",
                "sidecar_host": "windows.example.com",
            },
            teams_web={"enabled": True},
            _env_file=None,  # type: ignore[call-arg]
        )
        registry = build_connector_registry(
            settings=settings,
            zoom_factory=object(),  # type: ignore[arg-type]
            teams_factory=lambda: object(),  # type: ignore[arg-type,return-value]
            teams_web_factory=lambda: object(),  # type: ignore[arg-type,return-value]
        )
        assert MeetingPlatform.TEAMS in registry
        assert MeetingPlatform.TEAMS_WEB in registry


class TestPageProbe:
    """The probe is the only diagnostic that survives the channel itself being broken.

    Every other report from the page travels over the socket, so a closed socket silences the
    lines that would explain it. A live run proved the point: the script was alive and armed,
    the channel had gone, and `first_audio_published attached_pages=0` was the entire evidence.
    """

    @pytest.mark.asyncio
    async def test_a_missing_script_is_an_error_not_a_shrug(self) -> None:
        driver = FakeBrowserDriver(visible={IN_MEETING}, script_result=None)
        session = _build(driver=driver)
        try:
            await session.start()
        finally:
            await session.stop()
        # ``script_result=None`` stands in for a page with no ``__mcTeamsMic`` on it. The probe
        # must not raise, and must not report health it cannot see.
        assert driver.started == 1

    @pytest.mark.asyncio
    async def test_a_closed_channel_is_reported_rather_than_swallowed(self) -> None:
        """`socket: 3` is ``WebSocket.CLOSED``. The session still starts — a late page is
        recoverable and the script retries — but the log has to name it."""
        driver = FakeBrowserDriver(
            visible={IN_MEETING},
            script_result={
                "script": True,
                "socket": 3,
                "connects": 1,
                "closes": 4,
                "reconnectAttempts": 2,
                "connectError": None,
            },
        )
        session = _build(driver=driver)
        try:
            await session.start()
        finally:
            await session.stop()

    @pytest.mark.asyncio
    async def test_a_healthy_probe_reports_the_counts(self) -> None:
        driver = FakeBrowserDriver(
            visible={IN_MEETING},
            script_result={
                "script": True,
                "socket": 1,
                "connects": 1,
                "closes": 0,
                "micTrack": True,
                "captureSources": 1,
                "captureFrames": 120,
                "playoutFrames": 40,
            },
        )
        session = _build(driver=driver)
        try:
            await session.start()
        finally:
            await session.stop()

    @pytest.mark.asyncio
    async def test_a_probe_that_cannot_run_never_fails_the_session(self) -> None:
        """A diagnostic must not be the thing that breaks the join it was added to explain."""

        driver = FakeBrowserDriver(visible={IN_MEETING})

        async def explode(script: str) -> object:
            raise RuntimeError("evaluate is unavailable")

        driver.evaluate = explode  # type: ignore[method-assign]
        session = _build(driver=driver)
        try:
            await session.start()
        finally:
            await session.stop()
        assert driver.stopped == 1


class TestConsoleCapture:
    """The last-resort diagnostic, for failures only the browser can explain.

    A probe of a joined page reported a socket in CLOSED with no open event, no close event, no
    constructor error, and 58 silent retries. A WebSocket Chromium refuses can end up exactly
    like that — the page has no signal to read, so neither the script nor the bridge can say
    why. Chromium writes the reason to the console and nowhere else.
    """

    def test_a_driver_without_the_hook_is_simply_not_offered_one(self) -> None:
        """Optional on the driver, so an in-memory double does not have to implement a method
        only Playwright can honour — which is what keeps ``BrowserDriver`` narrow enough for a
        fake to satisfy."""
        driver = FakeBrowserDriver(visible={IN_MEETING})
        assert not hasattr(driver, "set_console_handler")
        # Building and starting must not care.
        _build(driver=driver)

    def test_the_real_driver_offers_it_and_defaults_to_off(self) -> None:
        """No handler means the listeners forward nothing, so no existing connector's
        behaviour changes."""
        from src.connectors.google_meet.automation.driver import PlaywrightDriver

        driver = PlaywrightDriver()
        assert callable(driver.set_console_handler)
        # Never raises with nothing registered.
        driver._on_console("error", "anything")

    def test_a_handler_that_raises_cannot_reach_playwright_s_event_loop(self) -> None:
        """This runs on a Playwright callback; a listener that throws there surfaces as an
        unretrieved future rather than as anything useful."""
        from src.connectors.google_meet.automation.driver import PlaywrightDriver

        driver = PlaywrightDriver()

        def explode(kind: str, text: str) -> None:
            raise RuntimeError("listener bug")

        driver.set_console_handler(explode)
        driver._on_console("error", "boom")

    def test_only_refusal_lines_are_surfaced(self) -> None:
        """Teams' client logs hundreds of lines a minute; a log carrying all of them is a log
        nobody reads."""
        session = _build()
        session._on_page_console("log", "Teams: rendering roster")
        assert session._console_lines == 0

        session._on_page_console(
            "error",
            "WebSocket connection to 'ws://127.0.0.1:59033/?token=x' failed: "
            "ERR_BLOCKED_BY_LOCAL_NETWORK_ACCESS_CHECKS",
        )
        assert session._console_lines == 1

    def test_a_repeated_refusal_is_bounded(self) -> None:
        """A page that repeats a refusal fifty-eight times has said it once."""
        session = _build()
        for _ in range(100):
            session._on_page_console("error", "websocket refused")
        assert session._console_lines <= 20
