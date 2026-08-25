"""``TeamsWebSession`` — one avatar in one Teams meeting, hearing and speaking.

Both legs are the same browser page, which is the structural fact everything else follows from:

* **publish** — Chromium joins the meeting and the avatar speaks through a synthetic
  ``MediaStreamTrack`` handed to a patched ``getUserMedia``.
* **ingest** — the page's own playout graph, tapped, and the roster/speaker/chat/captions read
  off the DOM.

**Why there is only one ingest leg**, where the Zoom-web connector has two: Teams' media and
event streams live behind the Graph entitlement ``connectors/teams`` needs — an app registration
with admin-consented ``Calls.AccessMedia.All`` in the tenant that owns the meeting. A guest
cannot obtain it, and the whole point of this connector is not to need it. So there is nothing to
select between, and no ``ingest_mode``.

**Start order is load-bearing, and differently from Zoom's.** On the Zoom-web connector the
microphone must exist before the join because Zoom picks its capture device *while* joining. Here
the ordering constraint is the *script*: it patches ``getUserMedia`` and installs the audio tap,
and it must be injected before we navigate — Teams calls ``getUserMedia`` during the pre-join
screen and builds its playout graph while joining, and a patch installed afterwards sees
neither.

**Why the legs do not recover independently.** They cannot: they are one socket into one page. A
lost page takes both, which is exactly what ``_browser_health`` reports — and stating that
plainly is better than a health report that implies an independence this connector does not have.
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

# Imported from the Google Meet connector rather than moved into shared code. Moving it is the
# better end state — Chromium is not a Meet concept — but that refactor touches ~10 files across
# connectors that are in production, and the brief for this change is explicitly not to disturb
# them. So the coupling is deliberate, narrow (a driver protocol and a launch-plan builder,
# neither of which knows what a meeting is), identical to the one ``connectors/zoom_web``
# already takes, and recorded here as the debt it is.
from src.connectors.google_meet.automation.driver import (
    BrowserDriver,
    PlaywrightDriver,
)
from src.connectors.google_meet.browser.launcher import build_launch_plan
from src.connectors.teams_web.automation.selectors import (
    DEFAULT_HAND_SELECTORS,
    DEFAULT_OBSERVER_SELECTORS,
)
from src.connectors.teams_web.config import TeamsWebConnectorConfig
from src.connectors.teams_web.egress.media_sink import TeamsWebMediaSink
from src.connectors.teams_web.ingest.page_audio_source import PageAudioSource
from src.connectors.teams_web.js import capture_worklet, inject_script, playout_worklet
from src.connectors.teams_web.meeting.active_speaker import TeamsSpeakerTracker
from src.connectors.teams_web.meeting.announcer import TeamsMeetingAnnouncer
from src.connectors.teams_web.meeting.attendance import TeamsAttendanceLedger
from src.connectors.teams_web.meeting.chat import TeamsChatSource
from src.connectors.teams_web.meeting.hand_raise import (
    DEFAULT_PROMPT as DEFAULT_INTERRUPT_PROMPT,
)
from src.connectors.teams_web.meeting.hand_raise import TeamsInterruptSource, render_prompt
from src.connectors.teams_web.meeting.join import DEFAULT_SELECTORS as JOIN_SELECTORS
from src.connectors.teams_web.meeting.join import TeamsWebJoiner
from src.connectors.teams_web.meeting.names import NOISE_PHRASES, STATUS_WORDS
from src.connectors.teams_web.meeting.observer import TeamsMeetingObserver
from src.connectors.teams_web.meeting.transcript import TeamsTranscript
from src.connectors.teams_web.page.server import PageAudioServer
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

COMPONENT_BROWSER = "teams_web_browser"

_PAGE_PROBE = """() => {
  const s = window.__mcTeamsMic;
  if (!s) return { script: false };
  return {
    script: true,
    socket: s.socket ? s.socket.readyState : null,
    connects: s.connects,
    closes: s.closes,
    staleSockets: s.staleSockets,
    reconnectAttempts: s.reconnectAttempts,
    reconnectPending: s.reconnectTimer !== null,
    connectError: s.connectError,
    playoutFrames: s.frames,
    captureSources: s.captureSources,
    captureFrames: s.captureFrames,
    micTrack: !!s.micTrack,
  };
}"""
"""What ``_probe_page`` asks the page.

Reads the state the script keeps on ``window.__mcTeamsMic`` — deliberately the same object an
operator can inspect by hand in a headed browser's console, so a live debugging session and
this log line see identical facts."""

_CONSOLE_NEEDLES = (
    "websocket",
    "ws://",
    "refused",
    "blocked",
    "mixed content",
    "insecure",
    "content security policy",
    "local network",
    "private network",
    "err_",
)
"""Console lines worth surfacing, matched case-insensitively.

Teams' own client logs hundreds of lines a minute, and a log that carries all of them is a log
nobody reads. These are the words a browser uses when it *refuses* something — which is the
only class of console line that can explain a channel the page cannot see failing."""

_MAX_CONSOLE_LINES = 20
"""Ceiling per session. A page that repeats a refusal fifty-eight times has said it once."""

_WS_OPEN = 1
"""``WebSocket.OPEN``. Anything else means the avatar's audio has nowhere to go and no page
observation can be reported — including the ones that would say so."""

_PROBE_ATTEMPTS = 6
_PROBE_INTERVAL_S = 1.0
"""How long ``_probe_page`` gives the page's liveness poll to heal before it complains.

Six seconds, against a one-second poll: comfortably more than one recovery cycle, and short
enough that a session which will never connect says so while somebody is still watching."""

MEETING_URL_KEY = "meeting_url"
"""The single ``platform_data`` key this connector reads.

Written by ``MeetingService.create_session`` from ``CreateSessionRequest.meeting_url``, which
is the same field the Graph-based Teams connector reads for the same purpose. Zoom writes
``rtms_stream_id`` and ``signaling_url`` into that dict for its own sessions; no connector may
read another's keys."""


class TeamsWebSession:
    """One avatar participating in one Teams meeting through a browser."""

    __slots__ = (
        "_announcer",
        "_attendance",
        "_chat",
        "_clock",
        "_config",
        "_console_lines",
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
    )

    def __init__(
        self,
        *,
        session: SessionContext,
        config: TeamsWebConnectorConfig,
        clock: MediaClock,
        driver: BrowserDriver,
        joiner: TeamsWebJoiner,
        page_server: PageAudioServer,
        source: AudioSource,
        publisher: MediaSink,
        router: MediaRouter,
        chat: TeamsChatSource | None = None,
        interrupts: TeamsInterruptSource | None = None,
        attendance: TeamsAttendanceLedger | None = None,
        speakers: TeamsSpeakerTracker | None = None,
        transcript: TeamsTranscript | None = None,
        announcer: TeamsMeetingAnnouncer | None = None,
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
        # Every one of these is optional, and ``None`` means the feature is switched off rather
        # than broken — the same terms the other browser sessions hold their equivalents on, so
        # a disabled session carries no inert surface at all.
        self._chat = chat
        self._interrupts = interrupts
        self._attendance = attendance
        self._speakers = speakers
        self._transcript = transcript
        self._announcer = announcer
        self._joined = False
        self._task: asyncio.Task[None] | None = None
        self._temp_profile: str | None = None
        self._console_lines = 0

    @property
    def session(self) -> SessionContext:
        return self._session

    @property
    def router(self) -> MediaRouter:
        return self._router

    @property
    def attendance(self) -> TeamsAttendanceLedger | None:
        """Who has been in this meeting, or ``None`` when the ledger is disabled.

        Read by ``MeetingService.attendance_snapshot`` through a structural check, which is how
        ``GET /sessions/{id}/participants`` serves this session without ``MeetingService``
        learning that Teams exists — the same duck-typing the other browser connectors rely on,
        and the reason their snapshot types and this one are field-for-field compatible. ``None``
        rather than an empty ledger when disabled, so "switched off" and "nobody here yet" stay
        distinguishable.
        """
        return self._attendance

    @property
    def speakers(self) -> TeamsSpeakerTracker | None:
        """Who is speaking and who has spoken, or ``None`` when tracking is disabled."""
        return self._speakers

    @property
    def transcript(self) -> TeamsTranscript | None:
        """What each participant said, or ``None`` when nothing records it."""
        return self._transcript

    async def start(self) -> None:
        """Launch the browser, inject the page, join, then route.

        A join failure propagates: a session that never got into the meeting must fail creation
        rather than sit there reporting health.
        """
        # A persistent profile is optional on this connector, unlike on Zoom-web where it is
        # what makes the microphone work at all. It is worth having — a signed-in profile joins
        # as a tenant user rather than an anonymous guest, which some organisers require and
        # which usually skips the lobby — so it is honoured when configured and a throwaway
        # directory is used when it is not.
        if self._config.profile_dir is not None:
            user_data_dir = self._config.profile_dir
        else:
            self._temp_profile = tempfile.mkdtemp(prefix="mc-teams-web-")
            user_data_dir = Path(self._temp_profile)

        plan = build_launch_plan(
            user_data_dir=user_data_dir,
            headless=self._config.headless,
            no_sandbox=self._config.no_sandbox,
            video_format=self._config.video_format,
            # **Teams' own CSP is what was closing the channel.** A page's ``connect-src``
            # governs WebSockets, and a blocked one fails silently: Chromium hands back a
            # socket already in ``CLOSED``, throws nothing, and fires no event — which is
            # exactly what a live run measured (58 retries, ``connects=0``, ``closes=0``, no
            # error) on an origin whose launcher pages had connected to the same port fine.
            # See ``LaunchPlan.bypass_csp``; off for every other connector.
            bypass_csp=self._config.bypass_csp,
        )

        # **Registered before the launch, because the failure it exists to catch happens
        # during the join.** Optional on the driver — see ``PlaywrightDriver`` — so an
        # in-memory double simply does not have it and nothing here branches on more than
        # "is it there".
        register = getattr(self._driver, "set_console_handler", None)
        if callable(register):
            register(self._on_page_console)

        await self._driver.start(plan)

        # **This order is the one thing here that cannot be rearranged.** The socket must be
        # bound before the script that dials it is injected, and the script must be injected
        # before we navigate: it patches ``getUserMedia`` and installs the audio tap, and Teams
        # calls the first on its pre-join screen and builds the graph the second watches while
        # joining.
        await self._page_server.start()
        await self._driver.add_init_script(self._page_bootstrap())
        await self._publisher.start(self._session.meeting)

        meeting = self._session.meeting
        outcome = await self._joiner.join(
            meeting_number=meeting.meeting_number,
            passcode=meeting.passcode,
            display_name=meeting.display_name,
            meeting_url=str(meeting.platform_data.get(MEETING_URL_KEY) or "") or None,
        )
        self._joined = True
        logger.info(
            "teams_web.session_joined",
            unmuted=outcome.unmuted,
            lobby=outcome.lobby,
        )

        # The page has to attach before the avatar speaks, or its first words go nowhere. Not
        # fatal: a late page still works, and health reports the gap.
        if not await self._page_server.wait_connected(timeout_s=10.0):
            logger.warning("teams_web.page_never_attached")

        # Asked once, after the join, and it is the difference between a named cause and
        # silence. See ``_probe_page``.
        await self._probe_page()

        # Before the source, because starting these is what lets observations be accepted and a
        # queue nobody has opened yet would drop the first of them.
        if self._chat is not None:
            await self._chat.start()
        if self._interrupts is not None:
            await self._interrupts.start()

        await self._source.start()
        self._task = asyncio.create_task(self._router.run(), name="media-router")

        # After the router, because the announcer has nothing to send until the avatar client
        # has completed its handshake — its first push happens one settle interval later, by
        # which time the negotiated version is known.
        if self._announcer is not None:
            await self._announcer.start()

    async def stop(self) -> None:
        """Tear down in a fixed order. Idempotent.

        Leave the meeting first so the participant disappears promptly rather than lingering as
        a frozen tile, then the media, then the browser.
        """
        # Before the router, so it cannot try to send on a transport that is being closed.
        if self._announcer is not None:
            await self._announcer.stop()

        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning(
                    "teams_web.task_failed_on_stop", task="media-router", error=str(exc)
                )

        # Before anything else is torn down, and before the browser is closed: leaving is a UI
        # action that needs a live page, and the participant must be gone from the meeting
        # before the thing hosting it disappears.
        if self._joined:
            self._joined = False
            left = await self._joiner.leave()
            if not left:
                logger.warning(
                    "teams_web.leaving_unconfirmed",
                    note="closing the browser without a confirmed leave; Teams may show the "
                    "avatar until it times the participant out",
                )

        # **Every step is guarded, and the browser is closed last and always.** Running these
        # unguarded in sequence means one raising skips the rest — and the Zoom-web connector
        # shipped exactly that: a failed ingest ``stop`` aborted teardown before the browser
        # closed, so ``DELETE /sessions/{id}`` returned success and the avatar stayed in the
        # meeting. Closing the browser is what actually removes the participant, so nothing
        # earlier is allowed to prevent it.
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
                logger.warning("teams_web.stop_step_failed", step=step, error=str(exc))

        # No further speaker observation will arrive for whoever held the floor when the page
        # went away, so the open turn is closed here rather than being left "speaking" forever
        # in whatever the API serves after the session ends.
        if self._speakers is not None:
            with suppress(Exception):
                self._speakers.release()

        with suppress(Exception):
            self._router.close()
        with suppress(Exception):
            await self._driver.stop()
        logger.info("teams_web.session_stopped")

        # Only ever a directory this session created.
        temp, self._temp_profile = self._temp_profile, None
        if temp is not None:
            shutil.rmtree(temp, ignore_errors=True)

    def _on_page_console(self, kind: str, text: str) -> None:
        """One console line from the page. Never raises, never floods.

        **This is here to answer a question nothing else can.** A probe of a joined page
        reported a socket in ``CLOSED`` with no ``open`` event, no ``close`` event, no
        constructor error, and 58 retries — every one of them failing instantly and silently.
        A WebSocket that Chromium refuses can end up exactly like that: the page has no signal
        to read, so neither the script nor the bridge can say why. Chromium writes the reason
        to the console and nowhere else.

        **Filtered rather than forwarded wholesale.** Teams' own client is loud — hundreds of
        lines a minute — and a log that carries all of it is a log nobody reads. Only lines
        that mention *our* channel or a browser-level refusal are surfaced, at ``warning``,
        which is the level the thing they explain deserves.
        """
        try:
            lowered = text.lower()
            if not any(needle in lowered for needle in _CONSOLE_NEEDLES):
                return
            if self._console_lines >= _MAX_CONSOLE_LINES:
                return
            self._console_lines += 1
            logger.warning(
                "teams_web.page_console",
                kind=kind,
                # Truncated: a console line can carry a stack trace, and the useful part of a
                # refusal is its first sentence.
                text=text[:400],
                note="the browser refused something on the page; if this names the loopback "
                "channel it is why the avatar is mute and deaf",
            )
        except Exception:  # pragma: no cover - defensive
            pass

    async def _probe_page(self) -> None:
        """Ask the page what state the injected script is actually in. Never raises.

        **This exists because every other diagnostic in the page travels over the socket**, so
        the one failure they cannot report is the socket itself being gone. A live run showed
        exactly that shape: the script had loaded and armed its observers, Teams' navigation
        churn closed the channel, nothing reconnected, and the only visible symptom anywhere
        was ``first_audio_published attached_pages=0``. The avatar was mute, the tap was deaf,
        and every line that would have explained it was being dropped on the way out.

        So this reads the state directly, through the driver, over a path that does not depend
        on the channel working.

        **Read from the main frame only**, which is the honest limit: the script keeps its state
        per frame, and if Teams ever renders the meeting inside an iframe this reports the
        wrapper's view rather than the meeting's. Even then ``script=False`` versus a socket in
        a bad state is a real distinction, which is more than silence offers.
        """
        # **Retried, because the page's liveness poll needs a moment to heal.** Probing once,
        # immediately after the join, catches the channel mid-recovery and reports a failure
        # that fixes itself a second later — which is worse than not reporting, because it
        # trains an operator to ignore the line. A handful of attempts spaced over a few
        # seconds distinguishes "recovering" from "cannot connect at all", which is the
        # distinction that matters.
        probe: object = None
        for attempt in range(_PROBE_ATTEMPTS):
            try:
                probe = await self._driver.evaluate(_PAGE_PROBE)
            except Exception as exc:
                logger.info("teams_web.page_probe_unavailable", error=str(exc))
                return
            if isinstance(probe, dict) and probe.get("socket") == _WS_OPEN:
                break
            if attempt + 1 < _PROBE_ATTEMPTS:
                await asyncio.sleep(_PROBE_INTERVAL_S)

        if not isinstance(probe, dict):
            logger.warning("teams_web.page_probe_unusable", got=type(probe).__name__)
            return

        if not probe.get("script"):
            logger.error(
                "teams_web.page_script_not_running",
                note="the injected script is absent from this page, so the avatar is mute and "
                "deaf; the init script was registered before navigation, so this points at a "
                "frame the script could not run in",
            )
            return

        # ``1`` is ``WebSocket.OPEN``. Anything else means the avatar's audio has nowhere to
        # go and no observation can be reported — including the ones that would say so.
        if probe.get("socket") != 1:
            logger.error(
                "teams_web.page_channel_down",
                socket_state=probe.get("socket"),
                connects=probe.get("connects"),
                closes=probe.get("closes"),
                # **The field that named the second failure.** A socket found already dead by
                # the liveness poll never fired a close event, so ``closes`` stays at zero
                # while this climbs — which is how "the handlers never ran" is told apart from
                # "the channel keeps dropping".
                stale_sockets=probe.get("staleSockets"),
                reconnect_attempts=probe.get("reconnectAttempts"),
                reconnect_pending=probe.get("reconnectPending"),
                error=probe.get("connectError"),
                note="the script is running but not attached after several seconds of "
                "retrying, so the page cannot reach the loopback channel at all; check that "
                "Chromium still launches with LocalNetworkAccessChecks disabled",
            )
            return

        logger.info(
            "teams_web.page_probe",
            connects=probe.get("connects"),
            closes=probe.get("closes"),
            stale_sockets=probe.get("staleSockets"),
            mic_track=probe.get("micTrack"),
            capture_sources=probe.get("captureSources"),
            capture_frames=probe.get("captureFrames"),
            playout_frames=probe.get("playoutFrames"),
        )

    def _page_bootstrap(self) -> str:
        """The injection script with its configuration prepended.

        Config travels as a global rather than by template substitution, so the JavaScript stays
        a file that can be linted and read on its own.

        **The selectors travel this way too**, which is what makes a Teams UI change a settings
        edit rather than an asset edit. The script treats each as optional, so a stale one costs
        the signal it carried and nothing else.

        **Every switch is folded against its Python-side consumer here rather than in the
        page.** An observer whose ledger is switched off would be scanning a DOM on the thread
        that encodes the avatar's audio to produce events nothing reads.
        """
        selectors = DEFAULT_HAND_SELECTORS
        observe = DEFAULT_OBSERVER_SELECTORS
        config = self._config
        config_payload = {
            "endpoint": self._page_server.endpoint,
            "sampleRateHz": config.publish_audio_format.sample_rate_hz,
            "workletSource": playout_worklet(),
            "displayName": config.display_name,
            # -- the audio tap ------------------------------------------------
            "captureWorkletSource": capture_worklet(),
            "captureFrameMs": config.capture_frame_ms,
            # -- the observers ------------------------------------------------
            "observeMs": config.observe_interval_ms,
            "rosterEnabled": config.attendance_enabled,
            # Also on for the interrupt source, which is a separate consumer of the same
            # signal: barge-in wants to know *who* took the floor even where nobody is keeping
            # a speaker ledger.
            "speakerEnabled": (
                config.speaker_tracking_enabled or config.voice_interrupt_enabled
            ),
            "speakerMinMs": config.speaker_min_ms,
            # The transcript is a consumer of chat as well as the chat source is — a meeting
            # held largely in chat should still be answerable — so this is on when either wants
            # it.
            "chatEnabled": config.chat_enabled or config.transcript_enabled,
            "chatPanelSelectors": list(observe.chat_panel_button)
            if config.chat_open_panel
            else [],
            # **The two guards that keep the page inside its own meeting.** A panel toggle is
            # only clicked while a meeting marker is present, and never when the candidate sits
            # inside Teams' app navigation — a live run had the observer click the app rail's
            # "People" button and navigate the whole SPA to the contacts page, meeting still
            # running behind it and every observer reading a page with no meeting in it. See
            # ``TeamsObserverSelectors.app_rail``.
            "appRailSelectors": list(observe.app_rail),
            # The joiner's own "are we in the call" test, reused: one definition of *in a
            # meeting* for both sides, so the page cannot believe it is in a call the joiner
            # would say it had left.
            "meetingMarkerSelectors": list(JOIN_SELECTORS.in_meeting_markers),
            "captionsEnabled": config.captions_enabled,
            "captionsButtonSelectors": list(observe.captions_button)
            if config.captions_auto_enable
            else [],
            "captionSettleMs": config.caption_settle_ms,
            "rosterRowSelectors": list(observe.roster_row),
            "rosterNameSelectors": list(observe.roster_name),
            # Not a selector: the ``data-tid`` prefixes whose remainder is the display name.
            # Read before any rendered text, because a Teams row's *label* changes when its
            # owner mutes and its ``data-tid`` does not — and every name-keyed thing in this
            # connector (the ledger, the hand latch, chat elimination) breaks when one person
            # arrives spelled three ways. See ``TeamsObserverSelectors.roster_tid_prefixes``.
            "rosterTidPrefixes": list(observe.roster_tid_prefixes),
            # The decorations Teams renders inside the same label as the name, as data for the
            # same reason the selectors are: a rename should cost a settings edit. Shared with
            # ``meeting/names.py``, which scrubs again on the way in — the page's copy is what
            # keeps the hand-raise key stable at source, and Python's is what stops a stale
            # page script putting a status label into an answer about who attended.
            "nameNoisePhrases": list(NOISE_PHRASES),
            "nameStatusWords": list(STATUS_WORDS),
            "speakerRowSelectors": list(observe.speaker_row),
            "speakerMarkerSelectors": list(observe.speaker_marker),
            # The containers, which are what let an *empty* panel be recognised as open — see
            # ``TeamsObserverSelectors.chat_container``.
            "chatContainerSelectors": list(observe.chat_container),
            "captionContainerSelectors": list(observe.caption_container),
            "panelReadyTimeoutMs": config.panel_ready_timeout_ms,
            "chatItemSelectors": list(observe.chat_item),
            "chatNameSelectors": list(observe.chat_name),
            "chatTextSelectors": list(observe.chat_text),
            "captionItemSelectors": list(observe.caption_item),
            "captionNameSelectors": list(observe.caption_name),
            "captionTextSelectors": list(observe.caption_text),
            # -- raised hands -------------------------------------------------
            "handRaiseEnabled": config.hand_raise_enabled,
            "handOpenPanel": config.hand_raise_open_panel,
            "handSelectors": list(selectors.hand_indicator),
            "handRowSelectors": list(selectors.participant_row),
            "handNameSelectors": list(selectors.participant_name),
            "participantsPanelSelectors": list(selectors.participants_panel_button),
            # In milliseconds, because that is what the page's timers take. The cooldown is
            # applied on **both** sides on purpose: the page's stops a re-render storm from
            # crossing the socket at all, and Python's is the one that survives a page reload
            # and governs the actual handover.
            "handCooldownMs": int(config.hand_raise_cooldown_s * 1_000),
        }
        return f"window.__mcTeamsConfig = {json.dumps(config_payload)};\n{inject_script()}"

    def health(self) -> HealthReport:
        components = [
            *self._router.health().components,
            self._publisher.health(),
            self._browser_health(),
        ]
        # Reported separately from the browser and the router, because they answer a question
        # neither can: an operator debugging "the avatar never answers the chat" needs to know
        # whether the source is running and what it has seen. Both are always healthy once
        # started — a meeting where nobody typed and nobody interrupted is indistinguishable
        # from a broken observer, and claiming otherwise would be invention.
        if self._chat is not None:
            components.append(self._chat.health())
        if self._interrupts is not None:
            components.append(self._interrupts.health())
        return HealthReport(components=tuple(components))

    def leg_states(self) -> tuple[ComponentState, ComponentState]:
        """``(ingest, publish)`` health, the input to session-state derivation.

        Both read off the same page, which is honest rather than lazy: they *are* the same
        socket, and reporting an independence this connector does not have would mislead
        whoever is reading the report at three in the morning.
        """
        return self._source.health().state, self._publisher.health().state

    def _browser_health(self) -> ComponentHealth:
        alive = self._driver.is_alive()
        return ComponentHealth(
            name=COMPONENT_BROWSER,
            state=ComponentState.HEALTHY if alive else ComponentState.UNHEALTHY,
            detail=None if alive else "the browser is gone",
        )


class TeamsWebSessionFactory:
    """Builds a fully wired ``TeamsWebSession``.

    All concrete-type knowledge lives here, so ``MeetingService`` composes one without naming
    Chromium, a DOM selector, or a page socket.
    """

    def __init__(
        self,
        *,
        config: TeamsWebConnectorConfig,
        metrics: MetricsCollector | None = None,
        driver_override: BrowserDriver | None = None,
        sink_override: MediaSink | None = None,
        source_override: AudioSource | None = None,
    ) -> None:
        self._config = config
        self._metrics = metrics
        # The same seams the other factories provide: tests and verification runs substitute
        # fakes without a second code path.
        self._driver_override = driver_override
        self._sink_override = sink_override
        self._source_override = source_override

    def build(self, session: SessionContext) -> TeamsWebSession:
        config = self._config
        ctx = session.frame_context()
        clock = MediaClock()

        driver = self._driver_override or PlaywrightDriver()
        joiner = TeamsWebJoiner(
            driver=driver,
            join_url_template=config.join_url_template,
            live_url_template=config.live_url_template,
            force_web_client=config.force_web_client,
            timeout_s=config.join_timeout_s,
            poll_interval_s=config.join_poll_interval_s,
        )

        page_server = PageAudioServer()
        publisher = self._sink_override or TeamsWebMediaSink(server=page_server)

        self_names = _self_name_candidates(session, config)

        # **The gate is switched off, and that is what makes barge-in possible.**
        #
        # The gate exists to stop the avatar hearing itself, which matters on a leg that carries
        # the meeting's mix *with the avatar in it* — RTMS does, and doc 008 §4 records the loop
        # that follows without a gate: the agent answering the tail of its own sentences.
        #
        # The page tap has no such loop. Teams does not play a participant their own microphone,
        # and the synthetic microphone lives in an ``AudioContext`` that connects only to a
        # ``MediaStreamDestination`` — never to a destination the tap watches. So the avatar's
        # audio is structurally absent, exactly as it is on Google Meet.
        #
        # And a shut gate would be actively harmful here: it cannot tell the avatar's echo from
        # somebody talking over it, so it would suppress the interruption along with the echo —
        # which is the whole reason ``speech`` is passed to the router below. What remains is the
        # acoustic path, a participant listening on speakers, which is a real echo no gate could
        # have caught anyway.
        echo_guard = EchoGuard(
            per_participant_audio=False,
            hangover_ms=config.echo_gate_hangover_ms,
            gate_enabled=False,
            metrics=self._metrics,
        )

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
        # Built after the pacer because the interrupt source reads it, and before the source
        # because the observer they feed has to exist before the page channel starts delivering.

        attendance = (
            TeamsAttendanceLedger(self_names=self_names)
            if config.attendance_enabled
            else None
        )

        speakers = (
            TeamsSpeakerTracker(
                clock=clock,
                hold_ms=config.speaker_hold_ms,
                merge_gap_ms=config.speaker_merge_gap_ms,
                self_names=self_names,
            )
            if config.speaker_tracking_enabled
            else None
        )

        # What was actually said — spoken and typed. This is what makes "what did they ask you?"
        # answerable at all: the agent's own transcription hears one mixed stream and cannot
        # attribute it, and everything above knows who is talking without knowing the words.
        transcript = (
            TeamsTranscript(self_names=self_names) if config.transcript_enabled else None
        )

        chat = (
            TeamsChatSource(
                require_mention=config.chat_require_mention,
                mention_names=(*self_names, *config.chat_mention_names),
            )
            if config.chat_enabled
            else None
        )

        # One source for both ways of claiming the floor — a hand seen on the page, and somebody
        # drawn as the active speaker while the avatar is talking. See ``meeting/hand_raise.py``
        # for why they are one thing rather than two.
        interrupts: TeamsInterruptSource | None = None
        if config.hand_raise_enabled or config.voice_interrupt_enabled:
            interrupts = TeamsInterruptSource(
                clock=clock,
                prompt=config.hand_raise_prompt or DEFAULT_INTERRUPT_PROMPT,
                cooldown_s=config.hand_raise_cooldown_s,
                self_names=self_names,
                voice_enabled=config.voice_interrupt_enabled,
                # **The pacer is the only thing that knows whether the avatar is mid-sentence**,
                # and it knows it from what it actually published rather than from what the
                # agent sent — which is the distinction that matters, because a sentence sitting
                # in a queue is not something anybody is talking over. Passed as a callable so
                # the interrupt source keeps knowing nothing about the media pipeline.
                is_avatar_speaking=lambda: pacer.is_speaking,
            )

        observer = TeamsMeetingObserver(
            attendance=attendance,
            speakers=speakers,
            transcript=transcript,
            chat=chat,
            interrupts=interrupts,
            clock=clock,
            leave_grace_s=config.roster_leave_grace_s,
        )
        page_server.set_event_handler(observer.on_page_event)

        source = self._source_override or PageAudioSource(
            server=page_server,
            ctx=ctx,
            clock=clock,
            queue_size=config.inbound_queue_size,
            metrics=self._metrics,
        )

        # **Energy barge-in works here because the gate is open** — see ``echo_guard`` above.
        # It is the better of the two triggers: it fires on the first syllable rather than when
        # Teams gets round to redrawing whose tile has the ring on it. Both run — the DOM speaker
        # observer still feeds ``TeamsInterruptSource.offer_voice`` — and they converge on the
        # same handover, which the cooldown there de-duplicates.
        speech: SpeechDetector | None = None
        voice_prompt = ""
        if config.voice_interrupt_enabled:
            speech = SpeechDetector(rms_threshold=config.speech_interrupt_threshold)
            # The anonymous rendering is the fallback. With a tracker attached the router
            # renders the same template with the speaker's name instead — the tapped mix carries
            # no attribution, so the name comes from the tracker rather than from the frame.
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
            TeamsMeetingAnnouncer(
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

        return TeamsWebSession(
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


def _speaker_provider(
    speakers: TeamsSpeakerTracker | None,
    attendance: TeamsAttendanceLedger | None,
) -> Callable[[], str | None] | None:
    """Who is talking, asked at the moment of a barge-in.

    **The tracker first, elimination second**, and the second half is not optional padding. The
    tapped mix carries no attribution at all, so an energy-triggered interruption knows *that*
    somebody spoke and never *who* — and if the DOM speaker marker has not been found, the
    tracker has nothing to offer either. The agent is then told "Someone wants to say something"
    in a two-person meeting where "someone" could only have been one person. A live run on the
    Zoom-web connector reported exactly that on every interruption, which is what added this.

    It is the identical rule ``TeamsMeetingObserver._named`` applies to a hand the page could not
    attribute — written down twice because the router needs a plain callable and the observer
    needs a dict repair, not because the reasoning differs.

    **Fails closed at two or more.** Naming one of several would be a guess, and a confidently
    wrong "Priya wants to say something" is worse than "Someone" — the agent would greet the
    wrong person by name. Returns ``None``, and the router falls back to its anonymous wording
    without the barge-in itself being delayed or lost.
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
    session: SessionContext, config: TeamsWebConnectorConfig
) -> tuple[str, ...]:
    """Every name that might be the avatar's own.

    **The session's name comes first**, because that is the string ``TeamsWebJoiner`` types into
    Teams' pre-join form — so it is the name every participant sees and the name the page reads
    back off the roster. It is *not* ``TeamsWebSettings.display_name``: ``MeetingService`` fills
    it from the ``POST /sessions`` request, falling back to ``MC_ZOOM__DISPLAY_NAME``. The two
    agree only while the defaults are untouched.

    The consequence of getting it wrong is silent and expensive, because five separate things key
    on it. The avatar would count itself as an attendee (every headcount wrong by one), report
    itself as the current speaker for as long as it talked, feed its own captioned sentences back
    to the agent as things a participant said, answer its own chat messages, and — worst —
    **interrupt itself continuously**, since it is the active speaker precisely when the barge-in
    path is live.

    Both are kept rather than one replacing the other. They are usually the same string, and when
    they differ there is no cost to recognising either — an extra name can only make
    self-detection *more* likely to fire, and the only thing it could wrongly match is a
    participant who has deliberately taken the avatar's name.
    """
    candidates: list[str] = []
    for raw in (session.meeting.display_name, config.display_name):
        name = " ".join(str(raw or "").split())
        if name and not any(name.casefold() == known.casefold() for known in candidates):
            candidates.append(name)
    return tuple(candidates)
