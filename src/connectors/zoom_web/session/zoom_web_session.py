"""``ZoomWebSession`` — one avatar in one Zoom meeting, hearing and speaking.

Two legs, each using the mechanism Zoom actually supports:

* **publish** — Chromium joins the meeting and the avatar speaks through a virtual
  microphone. Measured: Zoom transmits a device, and does not transmit an injected
  ``MediaStreamTrack``.
* **ingest** — one of two, selected by ``ingest_mode``. See doc 009.

**Ingest was RTMS-only, and this file used to say tapping the browser for audio was not an
option "regardless, because Zoom's web client has no audio transceiver to tap."** The second
half of that is still true and the conclusion did not follow. A peer-connection tap finds
nothing because Zoom decodes audio in WebAssembly and renders it through Web Audio — so the
tap belongs at *playout*, where every transport has to arrive, rather than at the transport.
That is what ``js/inject.js`` does, and a live meeting confirmed it attaches by the Web Audio
path on the first try.

It matters because RTMS requires the meeting to be hosted on an account with RTMS enabled,
which is not something a deployment can arrange for meetings other people book. ``browser``
mode is what makes the connector usable in an ordinary meeting; ``rtms`` remains the better
leg wherever an account can serve it.

**Start order is load-bearing.** The microphone comes up before the join, because
Zoom picks its capture device *while* joining — a device that appears afterwards is
not the one selected, and the avatar ends up holding a microphone nobody listens to.

The legs recover independently, as on the other connectors: RTMS reattaching does not
disturb the browser, and the browser is not torn down because a webhook was late.

**Where the avatar's knowledge of the meeting comes from follows the ingest mode.** Under
``rtms`` it comes down the ingest leg — Zoom reports who joined, who is speaking, what was
said and what was typed, each with a name attached, so those features are ledgers over an
event stream. Under ``browser`` there is no such stream and the page reads them, which makes
this connector look much more like the Google Meet one. Both produce the same observation
types, so everything downstream is shared.

A **raised hand** is read from the page in both modes, because RTMS does not report it at
all — which is why this session has a page event handler as well as a page audio sender.
See ``meeting/hand_raise.py``.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from collections.abc import Callable
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
from src.connectors.zoom.rtms.observations import MeetingObserver
from src.connectors.zoom_web.audio_capture.self_filter import SelfAudioFilter
from src.connectors.zoom_web.automation.selectors import (
    DEFAULT_HAND_SELECTORS,
    DEFAULT_OBSERVER_SELECTORS,
)
from src.connectors.zoom_web.config import ZoomWebConnectorConfig
from src.connectors.zoom_web.egress.media_sink import ZoomWebMediaSink
from src.connectors.zoom_web.ingest.page_audio_source import PageAudioSource
from src.connectors.zoom_web.js import capture_worklet, inject_script, playout_worklet
from src.connectors.zoom_web.meeting.active_speaker import ZoomSpeakerTracker
from src.connectors.zoom_web.meeting.announcer import ZoomMeetingAnnouncer
from src.connectors.zoom_web.meeting.attendance import ZoomAttendanceLedger
from src.connectors.zoom_web.meeting.chat import ZoomChatSource
from src.connectors.zoom_web.meeting.hand_raise import (
    DEFAULT_PROMPT as DEFAULT_INTERRUPT_PROMPT,
)
from src.connectors.zoom_web.meeting.hand_raise import ZoomInterruptSource, render_prompt
from src.connectors.zoom_web.meeting.join import ZoomWebJoiner
from src.connectors.zoom_web.meeting.observer import ZoomMeetingObserver
from src.connectors.zoom_web.meeting.transcript import ZoomTranscript
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
from src.services.media.speech_detector import SpeechDetector

logger = get_logger(__name__)

COMPONENT_BROWSER = "zoom_web_browser"


class ZoomWebSession:
    """One avatar participating in one Zoom meeting through a browser."""

    __slots__ = (
        "_announcer",
        "_attendance",
        "_chat",
        "_clock",
        "_config",
        "_driver",
        "_interrupts",
        "_joined",
        "_joiner",
        "_page_server",
        "_publisher",
        "_router",
        "_session",
        "_source",
        "_speakers",
        "_task",
        "_temp_profile",
        "_transcript",
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
        chat: ZoomChatSource | None = None,
        interrupts: ZoomInterruptSource | None = None,
        attendance: ZoomAttendanceLedger | None = None,
        speakers: ZoomSpeakerTracker | None = None,
        transcript: ZoomTranscript | None = None,
        announcer: ZoomMeetingAnnouncer | None = None,
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
        # Every one of these is optional, and ``None`` means the feature is switched off
        # rather than broken — the same terms the Google Meet session holds its equivalents
        # on, so a disabled session carries no inert surface at all.
        self._chat = chat
        self._interrupts = interrupts
        self._attendance = attendance
        self._speakers = speakers
        self._transcript = transcript
        self._announcer = announcer
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

    @property
    def attendance(self) -> ZoomAttendanceLedger | None:
        """Who has been in this meeting, or ``None`` when the ledger is disabled.

        Read by ``MeetingService.attendance_snapshot`` through a structural check, which is
        how ``GET /sessions/{id}/participants`` serves this session without
        ``MeetingService`` learning that Zoom exists — the same duck-typing the Google Meet
        connector already relies on, and the reason its snapshot types and this one are
        field-for-field compatible. ``None`` rather than an empty ledger when disabled, so
        "switched off" and "nobody here yet" stay distinguishable.
        """
        return self._attendance

    @property
    def speakers(self) -> ZoomSpeakerTracker | None:
        """Who is speaking and who has spoken, or ``None`` when tracking is disabled."""
        return self._speakers

    @property
    def transcript(self) -> ZoomTranscript | None:
        """What each participant said, or ``None`` when nothing records it."""
        return self._transcript

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

        # Before the source, because starting it is what lets observations begin arriving
        # and a queue nobody has opened yet would drop the first of them.
        if self._chat is not None:
            await self._chat.start()
        if self._interrupts is not None:
            await self._interrupts.start()

        # RTMS may not be bound yet; the source waits rather than failing.
        await self._source.start()
        self._task = asyncio.create_task(self._router.run(), name="media-router")

        # After the router, because the announcer has nothing to send until the avatar
        # client has completed its handshake — its first push happens one settle interval
        # later, by which time the negotiated version is known.
        if self._announcer is not None:
            await self._announcer.start()

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
        # Before the router, so it cannot try to send on a transport that is being closed.
        if self._announcer is not None:
            await self._announcer.stop()

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
            ("chat", self._chat.stop() if self._chat is not None else None),
            (
                "interrupts",
                self._interrupts.stop() if self._interrupts is not None else None,
            ),
            ("page_server", self._page_server.stop()),
        ):
            if action is None:
                continue
            try:
                await action
            except Exception as exc:
                logger.warning("zoom_web.stop_step_failed", step=step, error=str(exc))

        # No further speaker event will arrive for whoever held the floor when RTMS went
        # away, so the open turn is closed here rather than being left "speaking" forever
        # in whatever the API serves after the session ends.
        if self._speakers is not None:
            with suppress(Exception):
                self._speakers.release()

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

        **The selectors travel this way too**, which is what makes a Zoom UI change a
        settings edit rather than an asset edit — the same argument ``ZoomWebSelectors``
        makes for the join sequence. The script treats each as optional, so a stale one
        costs the signal it carried and nothing else.
        """
        selectors = DEFAULT_HAND_SELECTORS
        observe = DEFAULT_OBSERVER_SELECTORS
        browser = self._config.browser_ingest
        config = {
            "endpoint": self._page_server.endpoint,
            "sampleRateHz": self._config.publish_audio_format.sample_rate_hz,
            "workletSource": playout_worklet(),
            "displayName": self._config.display_name,
            # -- browser ingest ---------------------------------------------
            #
            # **Every one of these is folded against its Python-side consumer here rather
            # than in the page**, which is the same rule the RTMS subscriptions follow in
            # ``config.from_settings``: an observer whose ledger is switched off would be
            # scanning a DOM on the thread that encodes the avatar's audio to produce events
            # nothing reads. Under RTMS ingest they are all false and the page runs exactly
            # the script it ran before this existed.
            "ingestMode": self._config.ingest_mode,
            "captureWorkletSource": capture_worklet() if browser else None,
            "captureFrameMs": self._config.capture_frame_ms,
            "observeMs": self._config.observe_interval_ms,
            "rosterEnabled": browser and self._config.attendance_enabled,
            # Also on for the interrupt source, which is a separate consumer of the same
            # signal: barge-in wants to know *who* took the floor even where nobody is
            # keeping a speaker ledger.
            "speakerEnabled": browser
            and (
                self._config.speaker_tracking_enabled
                or self._config.voice_interrupt_enabled
            ),
            "speakerMinMs": self._config.speaker_min_ms,
            # The transcript is a consumer of chat as well as the chat source is — a meeting
            # held largely in chat should still be answerable — so this is on when either
            # wants it, exactly as ``rtms_chat_enabled`` is folded.
            "chatEnabled": browser
            and (self._config.chat_enabled or self._config.transcript_enabled),
            "chatPanelSelectors": list(observe.chat_panel_button)
            if self._config.chat_open_panel
            else [],
            "captionsEnabled": self._config.captions_enabled,
            "captionsButtonSelectors": list(observe.captions_button)
            if self._config.captions_auto_enable
            else [],
            "captionSettleMs": self._config.caption_settle_ms,
            "rosterRowSelectors": list(observe.roster_row),
            "rosterNameSelectors": list(observe.roster_name),
            "speakerRowSelectors": list(observe.speaker_row),
            "speakerMarkerSelectors": list(observe.speaker_marker),
            # The containers, which are what let an *empty* panel be recognised as open —
            # see ``ZoomObserverSelectors.chat_container``.
            "chatContainerSelectors": list(observe.chat_container),
            "captionContainerSelectors": list(observe.caption_container),
            "panelReadyTimeoutMs": self._config.panel_ready_timeout_ms,
            "chatItemSelectors": list(observe.chat_item),
            "chatNameSelectors": list(observe.chat_name),
            "chatTextSelectors": list(observe.chat_text),
            "captionItemSelectors": list(observe.caption_item),
            "captionNameSelectors": list(observe.caption_name),
            "captionTextSelectors": list(observe.caption_text),
            # -- raised hands ------------------------------------------------
            "handRaiseEnabled": self._config.hand_raise_enabled,
            "handOpenPanel": self._config.hand_raise_open_panel,
            "handSelectors": list(selectors.hand_indicator),
            "handRowSelectors": list(selectors.participant_row),
            "handNameSelectors": list(selectors.participant_name),
            "participantsPanelSelectors": list(selectors.participants_panel_button),
            # In milliseconds, because that is what the page's timers take. The cooldown is
            # applied on **both** sides on purpose: the page's stops a re-render storm from
            # crossing the socket at all, and Python's is the one that survives a page
            # reload and governs the actual handover.
            "handCooldownMs": int(self._config.hand_raise_cooldown_s * 1_000),
        }
        return f"window.__mcZoomConfig = {json.dumps(config)};\n{inject_script()}"

    def health(self) -> HealthReport:
        components = [
            *self._router.health().components,
            self._publisher.health(),
            self._browser_health(),
        ]
        # Reported separately from the browser and the router, because they answer a
        # question neither can: an operator debugging "the avatar never answers the chat"
        # needs to know whether the source is running and what it has seen. Both are always
        # healthy once started — a meeting where nobody typed and nobody interrupted is
        # indistinguishable from a broken observer, and claiming otherwise would be
        # invention.
        if self._chat is not None:
            components.append(self._chat.health())
        if self._interrupts is not None:
            components.append(self._interrupts.health())
        return HealthReport(components=tuple(components))

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

        self_names = _self_name_candidates(session, config)

        # **The gate is the one piece of the media path that browser ingest changes**, and
        # it changes because the thing it was defending against is no longer there.
        #
        # RTMS delivers the meeting's mix *with the avatar in it*, so the avatar's own voice
        # arrives back over a round trip well over a second. A mixed stream carries no
        # attribution, so identity filtering cannot bite, and the strict gate — withholding
        # every inbound frame while the avatar talks — is the only defence. Doc 008 §4
        # records what happens without it: the agent answering the tail of its own sentences,
        # in a loop.
        #
        # The page tap has no such loop. Zoom does not play a participant their own
        # microphone, and the synthetic microphone lives in an ``AudioContext`` that connects
        # only to a ``MediaStreamDestination`` — never to a destination the tap watches. So
        # the avatar's audio is structurally absent, exactly as it is on Google Meet, and the
        # gate is switched off there for the same reason it is here: a shut gate cannot tell
        # the avatar's echo from somebody talking over it, so it suppresses the interruption
        # along with the echo. What remains is the acoustic path — a participant on speakers
        # — which is a real echo the gate could not have caught anyway.
        #
        # That is what makes energy-based barge-in possible in this mode and impossible in
        # the other, and it is the whole reason ``speech`` is passed to the router below.
        echo_guard = EchoGuard(
            per_participant_audio=config.per_participant_audio and not config.browser_ingest,
            hangover_ms=config.echo_gate_hangover_ms,
            gate_enabled=not config.browser_ingest,
            metrics=self._metrics,
        )
        if not config.browser_ingest:
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

        # -- meeting awareness ---------------------------------------------
        #
        # Built after the pacer because the interrupt source reads it, and before the
        # ingest source because the observer they feed is what ingest carries them on.

        # Who has been in the meeting. Built only when enabled, on the same terms as
        # everything below — and note what it is *not* given: no task, no place on the
        # media path, no component in the health report. It folds an event stream the
        # connector already receives, which is why it can be on by default.
        attendance = (
            ZoomAttendanceLedger(self_names=self_names)
            if config.attendance_enabled
            else None
        )

        # Who is speaking. Also no task and no place on the media path: Zoom's
        # active-speaker events arrive on the signaling socket, so this costs the media
        # leg nothing at all — the property the Google Meet connector had to work for.
        speakers = (
            ZoomSpeakerTracker(
                clock=clock,
                hold_ms=config.speaker_hold_ms,
                merge_gap_ms=config.speaker_merge_gap_ms,
                self_names=self_names,
            )
            if config.speaker_tracking_enabled
            else None
        )

        # What was actually said — spoken and typed. This is what makes "what did they ask
        # you?" answerable at all: the agent's own transcription hears one mixed stream and
        # cannot attribute it, and everything above knows who is talking without knowing
        # the words.
        transcript = ZoomTranscript(self_names=self_names) if config.transcript_enabled else None

        chat = (
            ZoomChatSource(
                require_mention=config.chat_require_mention,
                mention_names=(*self_names, *config.chat_mention_names),
            )
            if config.chat_enabled
            else None
        )

        # One source for both ways of claiming the floor — a hand seen on the page, and
        # Zoom reporting somebody talking over the avatar. See ``meeting/hand_raise.py``
        # for why they are one thing rather than two.
        interrupts: ZoomInterruptSource | None = None
        if config.hand_raise_enabled or config.voice_interrupt_enabled:
            interrupts = ZoomInterruptSource(
                clock=clock,
                prompt=config.hand_raise_prompt or DEFAULT_INTERRUPT_PROMPT,
                cooldown_s=config.hand_raise_cooldown_s,
                self_names=self_names,
                voice_enabled=config.voice_interrupt_enabled,
                # **The pacer is the only thing that knows whether the avatar is
                # mid-sentence**, and it knows it from what it actually published rather
                # than from what the agent sent — which is the distinction that matters,
                # because a sentence sitting in a queue is not something anybody is talking
                # over. Passed as a callable rather than the pacer itself so the interrupt
                # source keeps knowing nothing about the media pipeline.
                is_avatar_speaking=lambda: pacer.is_speaking,
            )

        observer = ZoomMeetingObserver(
            attendance=attendance,
            speakers=speakers,
            transcript=transcript,
            chat=chat,
            interrupts=interrupts,
            # Only the page's observations need stamping — RTMS's arrive already timed. Passed
            # unconditionally anyway: the RTMS path never calls the clock, so a mode branch
            # here would buy nothing and be one more thing to get wrong.
            clock=clock,
            # Only the page's roster needs this: RTMS reports joins and leaves as exact
            # events, so nothing there is ever debounced.
            leave_grace_s=config.roster_leave_grace_s if config.browser_ingest else 0.0,
        )
        page_server.set_event_handler(observer.on_page_event)

        source = self._source_override or self._build_source(
            session, ctx, clock, observer=observer, server=page_server
        )

        # **Whether a voice can interrupt from audio at all is a property of the ingest leg,
        # not a preference**, which is why this is a branch on the mode rather than on a
        # setting of its own.
        #
        # Under RTMS there is deliberately no detector, and doc 008 §4 is the argument: the
        # router's energy detector reads inbound frames *after* ``EchoGuard``, the guard runs
        # strict there because RTMS carries the avatar's own voice, and so every frame is
        # withheld during precisely the window a barge-in exists in. A detector would be a
        # second interrupt path that can only fire when no interruption is needed. Zoom's
        # ``ACTIVE_SPEAKER_CHANGE`` is used instead — a control message, delivered regardless
        # of the gate.
        #
        # Under browser ingest the gate is open (see ``echo_guard`` above), so the detector
        # works, and it is the better of the two triggers: it fires on the first syllable
        # rather than when Zoom gets round to redrawing whose tile is highlighted. Both run —
        # the DOM speaker observer still feeds ``ZoomInterruptSource.offer_voice`` — and they
        # converge on the same handover, which the cooldown there already de-duplicates.
        speech: SpeechDetector | None = None
        voice_prompt = ""
        if config.browser_ingest and config.voice_interrupt_enabled:
            speech = SpeechDetector(rms_threshold=config.speech_interrupt_threshold)
            # The anonymous rendering is the fallback. With a tracker attached the router
            # renders the same template with the speaker's name instead — the tapped mix
            # carries no attribution, so the name comes from the tracker rather than from
            # the frame.
            voice_prompt = render_prompt(
                config.hand_raise_prompt or DEFAULT_INTERRUPT_PROMPT, None
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
            chat=chat,
            hands=interrupts,
            hand_raise_mute_ms=config.hand_raise_mute_ms,
            speech=speech,
            voice_prompt=voice_prompt,
            speaker_provider=(
                _speaker_provider(speakers, attendance) if speech is not None else None
            ),
            voice_prompt_template=(
                config.hand_raise_prompt or DEFAULT_INTERRUPT_PROMPT
                if speech is not None
                else ""
            ),
        )

        announcer = (
            ZoomMeetingAnnouncer(
                avatar=avatar,
                ledger=attendance,
                speakers=speakers,
                transcript=transcript,
                interval_s=config.context_push_interval_s,
                require_negotiation=config.context_push_require_negotiation,
            )
            if config.context_push_enabled
            and (attendance is not None or speakers is not None or transcript is not None)
            else None
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
            chat=chat,
            interrupts=interrupts,
            attendance=attendance,
            speakers=speakers,
            transcript=transcript,
            announcer=announcer,
        )

    def _build_trigger(self) -> RtmsTrigger | None:
        """The RTMS starter, or ``None`` when it is not fully configured.

        ``rtms_auto_start`` already folds in the credential check, so an
        unconfigured deployment — or a test — silently gets no trigger and makes no
        outbound call.
        """
        config = self._config
        # Browser ingest has nothing to trigger: there is no RTMS connection waiting for a
        # stream to start, and asking Zoom to start one would provoke a webhook nothing is
        # listening for — on an account that, in the case this mode exists for, cannot serve
        # the request at all.
        if config.browser_ingest or not config.rtms_auto_start:
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
        self,
        session: SessionContext,
        ctx: object,
        clock: MediaClock,
        *,
        observer: MeetingObserver | None = None,
        server: PageAudioServer,
    ) -> AudioSource:
        """The ingest leg — whichever of the two this deployment is configured for.

        **The branch is here and nowhere else.** Both sides satisfy ``AudioSource``, both
        feed the same observer, and everything downstream — the router, the pacer, the
        decoder, the ledgers, the announcer, every HTTP endpoint — is written against those
        two interfaces rather than against either implementation. That is what made browser
        ingest an addition rather than a fork of the connector.
        """
        if self._config.browser_ingest:
            # No credentials, no webhook, no attachment to wait for, no trigger to fire.
            # The page is already tapping by the time this exists — it started patching
            # before Zoom's own scripts ran — so this only has to collect what arrives.
            return PageAudioSource(
                server=server,
                ctx=ctx,  # type: ignore[arg-type]
                clock=clock,
                queue_size=self._config.inbound_queue_size,
                metrics=self._metrics,
            )
        return self._build_rtms_source(session, ctx, clock, observer=observer)

    def _build_rtms_source(
        self,
        session: SessionContext,
        ctx: object,
        clock: MediaClock,
        *,
        observer: MeetingObserver | None = None,
    ) -> AudioSource:
        """RTMS ingest — audio, and everything else the avatar knows about the meeting.

        No attachment is read here: it may not exist yet. The source is handed the
        live ``session`` and resolves the binding itself once
        ``meeting.rtms_started`` has arrived — which is why a browser join and a
        webhook can land in either order without either failing the session.

        **The observer rides the same attachment**, which is why it is passed here rather
        than being started separately. Transcript, chat and participant events are streams
        on the RTMS connection, so they attach when it attaches and re-subscribe when it
        reconnects, with no second lifecycle to get wrong. If the account refuses the text
        subscriptions the connection survives on audio alone and says why in its health
        detail (``RtmsService._media_handshake``).
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
                observer=observer,
                subscribe_transcript=self._config.rtms_transcript_enabled,
                subscribe_chat=self._config.rtms_chat_enabled,
                subscribe_events=self._config.rtms_events_enabled,
            ),
        )


def _speaker_provider(
    speakers: ZoomSpeakerTracker | None,
    attendance: ZoomAttendanceLedger | None,
) -> Callable[[], str | None] | None:
    """Who is talking, asked at the moment of a barge-in.

    **The tracker first, elimination second**, and the second half is what a live meeting
    showed to be necessary. Every interruption in that run reported ``participant=Someone``:
    the tapped mix carries no attribution, and Zoom's speaking indicator had not been found
    in the DOM, so the tracker had nothing to offer. The agent was then told "Someone wants
    to say something" in a two-person meeting where "someone" could only have been one
    person.

    Elimination closes that, and it is the identical rule ``ZoomMeetingObserver._named``
    already applies to a hand the page could not attribute — written down twice because the
    router needs a plain callable and the observer needs a dict repair, not because the
    reasoning differs.

    **Fails closed at two or more.** Naming one of several would be a guess, and a
    confidently wrong "Priya wants to say something" is worse than "Someone" — the agent
    would greet the wrong person by name. Returns ``None``, and the router falls back to its
    anonymous wording without the barge-in itself being delayed or lost.
    """
    if speakers is None and attendance is None:
        return None

    def current() -> str | None:
        if speakers is not None:
            named = speakers.current_speaker()
            if named:
                return named
        if attendance is not None:
            others = attendance.present_names
            if len(others) == 1:
                return others[0]
        return None

    return current


def _self_name_candidates(
    session: SessionContext, config: ZoomWebConnectorConfig
) -> tuple[str, ...]:
    """Every name that might be the avatar's own.

    **The session's name comes first, and reading only the configured one was a bug.**
    The browser joins under ``session.meeting.display_name`` — that is the string
    ``ZoomWebJoiner`` types into Zoom's form, so it is the name every participant sees and
    the name RTMS puts on the avatar's own events. It is *not* ``ZoomWebSettings.display_name``:
    ``MeetingService`` fills it from the ``POST /sessions`` request, falling back to
    ``MC_ZOOM__DISPLAY_NAME``. The two agree only while all three defaults are untouched.

    The consequence of getting it wrong is silent and expensive, because five separate
    things key on it. The avatar would count itself as an attendee (every headcount wrong by
    one), report itself as the current speaker for as long as it talked, feed its own
    sentences back to the agent as things a participant said, answer its own chat messages,
    and — worst — **interrupt itself continuously**, since it is an active speaker precisely
    when the barge-in gate is open.

    ``SelfAudioFilter`` has always read the session's name for the same reason; this brings
    the rest of the connector onto the source that is actually authoritative.

    Both are kept rather than one replacing the other. They are usually the same string, and
    when they differ there is no cost to recognising either — an extra name can only make
    self-detection *more* likely to fire, and the only thing it could wrongly match is a
    participant who has deliberately taken the avatar's name.
    """
    candidates: list[str] = []
    for raw in (session.meeting.display_name, config.display_name):
        name = " ".join(str(raw or "").split())
        if name and not any(name.casefold() == known.casefold() for known in candidates):
            candidates.append(name)
    return tuple(candidates)
