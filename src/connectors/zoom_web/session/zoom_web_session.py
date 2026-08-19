"""``ZoomWebSession`` — one avatar in one Zoom meeting, hearing and speaking.

Two legs, each using the mechanism Zoom actually supports:

* **publish** — Chromium joins the meeting and the avatar speaks through a virtual
  microphone. Measured: Zoom transmits a device, and does not transmit an injected
  ``MediaStreamTrack``.
* **ingest** — RTMS, reused wholesale from ``connectors/zoom/rtms``. It is Zoom's own
  API, it carries the speaker's name alongside the audio, and it needs nothing from
  the host. Tapping the browser for audio is not an option regardless: Zoom's web
  client has no audio transceiver to tap.

**Start order is load-bearing.** The microphone comes up before the join, because
Zoom picks its capture device *while* joining — a device that appears afterwards is
not the one selected, and the avatar ends up holding a microphone nobody listens to.

The legs recover independently, as on the other connectors: RTMS reattaching does not
disturb the browser, and the browser is not torn down because a webhook was late.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from contextlib import suppress
from pathlib import Path

from src.avatar.client import AvatarClient
from src.avatar.ws_transport import WebSocketAvatarTransport

# Imported from the Google Meet connector rather than moved into shared code.
# Moving it is the better end state — Chromium is not a Meet concept — but that
# refactor touches ~10 files in a connector that is in production, and the brief for
# this change is explicitly not to disturb it. So the coupling is deliberate, narrow
# (a driver protocol and a launch-plan builder, neither of which knows what a meeting
# is), and recorded here as the debt it is.
from src.connectors.google_meet.automation.driver import (
    BrowserDriver,
    PlaywrightDriver,
)
from src.connectors.google_meet.browser.launcher import build_launch_plan
from src.connectors.zoom.api.rtms_trigger import RtmsTrigger
from src.connectors.zoom.rtms.audio_source import RtmsAudioSource
from src.connectors.zoom_web.audio_capture.self_filter import SelfAudioFilter
from src.connectors.zoom_web.config import ZoomWebConnectorConfig
from src.connectors.zoom_web.egress.media_sink import ZoomWebMediaSink
from src.connectors.zoom_web.js import inject_script, playout_worklet
from src.connectors.zoom_web.meeting.join import ZoomWebJoiner
from src.connectors.zoom_web.page.server import PageAudioServer
from src.domain.health import ComponentHealth, ComponentState, HealthReport
from src.domain.session import SessionContext
from src.infrastructure.logging import get_logger
from src.infrastructure.metrics import MetricsCollector
from src.infrastructure.reconnect import ReconnectPolicy
from src.protocols.audio_source import AudioSource
from src.protocols.sink import MediaSink
from src.services.media.clock import MediaClock
from src.services.media.decode_pipeline import DecodePipeline
from src.services.media.decoders.ffmpeg import FfmpegDecoder
from src.services.media.echo_guard import EchoGuard
from src.services.media.idle_source import IdleFrameSource
from src.services.media.pacer import Pacer
from src.services.media.router import MediaRouter

logger = get_logger(__name__)

COMPONENT_BROWSER = "zoom_web_browser"


class ZoomWebSession:
    """One avatar participating in one Zoom meeting through a browser."""

    __slots__ = (
        "_clock",
        "_config",
        "_driver",
        "_joined",
        "_joiner",
        "_page_server",
        "_publisher",
        "_router",
        "_session",
        "_source",
        "_task",
        "_temp_profile",
        "_trigger",
        "_trigger_task",
    )

    def __init__(
        self,
        *,
        session: SessionContext,
        config: ZoomWebConnectorConfig,
        clock: MediaClock,
        driver: BrowserDriver,
        joiner: ZoomWebJoiner,
        page_server: PageAudioServer,
        source: AudioSource,
        publisher: MediaSink,
        router: MediaRouter,
        trigger: RtmsTrigger | None = None,
    ) -> None:
        self._session = session
        self._config = config
        self._clock = clock
        self._driver = driver
        self._joiner = joiner
        self._page_server = page_server
        self._source = source
        self._publisher = publisher
        self._router = router
        self._trigger = trigger
        self._joined = False
        self._task: asyncio.Task[None] | None = None
        self._trigger_task: asyncio.Task[None] | None = None
        self._temp_profile: str | None = None

    @property
    def session(self) -> SessionContext:
        return self._session

    @property
    def router(self) -> MediaRouter:
        return self._router

    async def start(self) -> None:
        """Launch the browser, publish a microphone, join, then route.

        A join failure propagates: a session that never got into the meeting must
        fail creation rather than sit there reporting health.
        """
        # **A persistent profile is what makes the injected microphone work.**
        # Chromium stores the per-origin device choice in ``Default/Preferences``.
        # With a throwaway profile Zoom has no microphone selected, so its capture
        # pipeline never starts and it publishes nothing whatever ``getUserMedia``
        # returns — measured, and the reason an earlier device-based design existed.
        # A profile where a microphone has been chosen once makes Zoom request that
        # ``deviceId``, which the page patch then answers.
        if self._config.profile_dir is not None:
            user_data_dir = self._config.profile_dir
        else:
            self._temp_profile = tempfile.mkdtemp(prefix="mc-zoom-web-")
            user_data_dir = Path(self._temp_profile)

        plan = build_launch_plan(
            user_data_dir=user_data_dir,
            headless=self._config.headless,
            no_sandbox=self._config.no_sandbox,
            video_format=self._config.video_format,
        )
        await self._driver.start(plan)

        # The socket must be bound before the script that dials it is injected, and
        # the script must be injected before we navigate: it patches getUserMedia,
        # and Zoom calls that during the join.
        await self._page_server.start()
        await self._driver.add_init_script(self._page_bootstrap())
        await self._publisher.start(self._session.meeting)

        meeting = self._session.meeting
        outcome = await self._joiner.join(
            meeting_number=meeting.meeting_number,
            passcode=meeting.passcode,
            display_name=meeting.display_name,
        )
        self._joined = True
        logger.info(
            "zoom_web.session_joined",
            meeting_number=meeting.meeting_number,
            audio_joined=outcome.audio_joined,
            unmuted=outcome.unmuted,
        )

        # The page has to attach before the avatar speaks, or its first words go
        # nowhere. Not fatal: a late page still works, and health reports the gap.
        if not await self._page_server.wait_connected(timeout_s=10.0):
            logger.warning("zoom_web.page_never_attached")

        # RTMS may not be bound yet; the source waits rather than failing.
        await self._source.start()
        self._task = asyncio.create_task(self._router.run(), name="media-router")

        # Last, and only once ingest is waiting: Zoom stops an RTMS stream nobody
        # attaches to within about a minute, so the webhook this provokes must have
        # somewhere to land the moment it arrives.
        if self._trigger is not None:
            self._trigger_task = asyncio.create_task(
                self._start_rtms(), name="rtms-trigger"
            )

    async def _start_rtms(self) -> None:
        """Ask Zoom to start RTMS. Best-effort — never fails the session.

        RTMS can also be started by an account auto-start rule or by hand, so a
        failure here is not proof that ingest will not arrive. If it genuinely does
        not, the ingest leg times out on its own with a message about the missing
        webhook, which describes the situation better than this call can.
        """
        meeting = self._session.meeting
        if meeting.meeting_uuid is not None:
            logger.info("zoom_web.rtms_trigger_skipped", reason="already bound")
            return
        try:
            await self._trigger.start(meeting.meeting_number)  # type: ignore[union-attr]
        except Exception as exc:
            logger.warning(
                "zoom_web.rtms_trigger_failed",
                meeting_number=meeting.meeting_number,
                error=str(exc),
            )
            return
        logger.info("zoom_web.rtms_trigger_started", meeting_number=meeting.meeting_number)

    async def stop(self) -> None:
        """Tear down in a fixed order. Idempotent.

        Leave the meeting first so the participant disappears promptly rather than
        lingering as a frozen tile, then the media, then the browser.
        """
        for name, task in (
            ("rtms-trigger", self._trigger_task),
            ("media-router", self._task),
        ):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning("zoom_web.task_failed_on_stop", task=name, error=str(exc))
        self._trigger_task = None
        self._task = None

        # Before anything else is torn down, and before the browser is closed:
        # leaving is a UI action that needs a live page, and the participant must be
        # gone from the meeting before the thing hosting it disappears.
        if self._joined:
            self._joined = False
            left = await self._joiner.leave()
            if not left:
                logger.warning(
                    "zoom_web.leaving_unconfirmed",
                    note="closing the browser without a confirmed leave; Zoom may "
                    "show the avatar until it times the participant out",
                )

        # **Every step is guarded, and the browser is closed last and always.**
        # These ran unguarded in sequence, and one raising skipped the rest — which
        # is exactly what happened: a failed ingest ``stop`` aborted teardown before
        # the browser closed, so ``DELETE /sessions/{id}`` returned and the avatar
        # stayed in the meeting. Closing the browser is what actually removes the
        # participant, so nothing earlier is allowed to prevent it.
        for step, action in (
            ("publisher", self._publisher.stop()),
            ("ingest", self._source.stop()),
            ("page_server", self._page_server.stop()),
        ):
            try:
                await action
            except Exception as exc:
                logger.warning("zoom_web.stop_step_failed", step=step, error=str(exc))

        with suppress(Exception):
            self._router.close()
        with suppress(Exception):
            await self._driver.stop()
        logger.info("zoom_web.session_stopped")

        # Only ever a directory this session created.
        temp, self._temp_profile = self._temp_profile, None
        if temp is not None:
            shutil.rmtree(temp, ignore_errors=True)

    def _page_bootstrap(self) -> str:
        """The injection script with its configuration prepended.

        Config travels as a global rather than by template substitution, so the
        JavaScript stays a file that can be linted and read on its own.
        """
        config = {
            "endpoint": self._page_server.endpoint,
            "sampleRateHz": self._config.publish_audio_format.sample_rate_hz,
            "workletSource": playout_worklet(),
        }
        return f"window.__mcZoomConfig = {json.dumps(config)};\n{inject_script()}"

    def health(self) -> HealthReport:
        return HealthReport(
            components=(
                *self._router.health().components,
                self._publisher.health(),
                self._browser_health(),
            )
        )

    def leg_states(self) -> tuple[ComponentState, ComponentState]:
        """``(ingest, publish)`` health, the input to session-state derivation."""
        return self._source.health().state, self._publisher.health().state

    def _browser_health(self) -> ComponentHealth:
        alive = self._driver.is_alive()
        return ComponentHealth(
            name=COMPONENT_BROWSER,
            state=ComponentState.HEALTHY if alive else ComponentState.UNHEALTHY,
            detail=None if alive else "the browser is gone",
        )


class ZoomWebSessionFactory:
    """Builds a fully wired ``ZoomWebSession``.

    All concrete-type knowledge lives here, so ``MeetingService`` composes one
    without naming Chromium, RTMS, or a capture device.
    """

    def __init__(
        self,
        *,
        config: ZoomWebConnectorConfig,
        metrics: MetricsCollector | None = None,
        driver_override: BrowserDriver | None = None,
        sink_override: MediaSink | None = None,
        source_override: AudioSource | None = None,
    ) -> None:
        self._config = config
        self._metrics = metrics
        # The same seams the other factories provide: tests and verification runs
        # substitute fakes without a second code path.
        self._driver_override = driver_override
        self._sink_override = sink_override
        self._source_override = source_override

    def build(self, session: SessionContext) -> ZoomWebSession:
        config = self._config
        ctx = session.frame_context()
        clock = MediaClock()

        driver = self._driver_override or PlaywrightDriver()
        joiner = ZoomWebJoiner(
            driver=driver,
            timeout_s=config.join_timeout_s,
            poll_interval_s=config.join_poll_interval_s,
        )

        page_server = PageAudioServer()
        publisher = self._sink_override or ZoomWebMediaSink(server=page_server)
        source = self._source_override or self._build_source(session, ctx, clock)

        echo_guard = EchoGuard(
            per_participant_audio=config.per_participant_audio,
            hangover_ms=config.echo_gate_hangover_ms,
            metrics=self._metrics,
        )
        echo_guard.set_own_participant(publisher.own_participant())

        avatar = AvatarClient(
            transport=WebSocketAvatarTransport(
                url=config.avatar_url,
                ctx=ctx,
                    clock=clock,
                send_queue_size=config.avatar_send_queue_size,
                open_timeout_s=config.avatar_connect_timeout_s,
                metrics=self._metrics,
            ),
            ctx=ctx,
            policy=ReconnectPolicy(
                initial_delay_s=config.avatar_reconnect_initial_delay_s,
                max_delay_s=config.avatar_reconnect_max_delay_s,
                max_attempts=config.avatar_reconnect_max_attempts,
            ),
            metrics=self._metrics,
        )

        decode = DecodePipeline(
            decoder=FfmpegDecoder(
                ctx=ctx,
                    clock=clock,
                video_format=config.video_format,
                audio_format=config.publish_audio_format,
                metrics=self._metrics,
            ),
            ctx=ctx,
            metrics=self._metrics,
        )

        pacer = Pacer(
            ctx=ctx,
                clock=clock,
            sink=publisher,
            idle=IdleFrameSource(
                ctx=ctx,
                video_format=config.video_format,
                audio_format=config.publish_audio_format,
            ),
            video_format=config.video_format,
            audio_format=config.publish_audio_format,
            echo_guard=echo_guard,
            video_queue_size=config.video_queue_size,
            audio_queue_size=config.audio_queue_size,
            metrics=self._metrics,
        )

        router = MediaRouter(
            ctx=ctx,
                clock=clock,
            source=source,
            avatar=avatar,
            decode=decode,
            pacer=pacer,
            echo_guard=echo_guard,
            metrics=self._metrics,
        )

        return ZoomWebSession(
            trigger=self._build_trigger(),
            session=session,
            config=config,
                clock=clock,
            driver=driver,
            joiner=joiner,
            page_server=page_server,
            source=source,
            publisher=publisher,
            router=router,
        )

    def _build_trigger(self) -> RtmsTrigger | None:
        """The RTMS starter, or ``None`` when it is not fully configured.

        ``rtms_auto_start`` already folds in the credential check, so an
        unconfigured deployment — or a test — silently gets no trigger and makes no
        outbound call.
        """
        config = self._config
        if not config.rtms_auto_start:
            return None
        return RtmsTrigger(
            account_id=config.account_id,
            client_id=config.s2s_client_id,
            client_secret=config.s2s_client_secret,
            app_client_id=config.client_id,
            api_base_url=config.api_base_url,
            oauth_base_url=config.oauth_base_url,
            timeout_s=config.api_timeout_s,
        )

    def _build_source(
        self, session: SessionContext, ctx: object, clock: MediaClock
    ) -> AudioSource:
        """RTMS ingest, unchanged from the SDK connector.

        No attachment is read here: it may not exist yet. The source is handed the
        live ``session`` and resolves the binding itself once
        ``meeting.rtms_started`` has arrived — which is why a browser join and a
        webhook can land in either order without either failing the session.
        """
        # Wrapped because RTMS returns every participant — us included — and a
        # mixed stream carries no names, so this filter only bites when
        # ``per_participant_audio`` is turned back on. It is kept rather than
        # deleted because that setting is the one knob that reintroduces the echo,
        # and identity beats timing whenever identity is available.
        #
        # On the mixed stream that is now the default, the avatar's own voice is
        # suppressed by ``EchoGuard``'s strict gate instead.
        return SelfAudioFilter(
            display_name=session.meeting.display_name,
            inner=RtmsAudioSource(
                session=session,
                client_id=self._config.client_id,
                client_secret=self._config.client_secret,
                ctx=ctx,  # type: ignore[arg-type]
                clock=clock,
                queue_size=self._config.inbound_queue_size,
                send_rate_ms=self._config.rtms_send_rate_ms,
                per_participant_audio=self._config.per_participant_audio,
                metrics=self._metrics,
            ),
        )
