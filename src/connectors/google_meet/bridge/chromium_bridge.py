"""ChromiumBridge — one browser, in one meeting, shared by ingest and egress.

**The structural comparison that explains this class.** Zoom has two independent
integrations: RTMS carries audio in over its own WebSocket, and a C++ sidecar carries audio
and video out over its own socket. Either can fail and recover without the other noticing,
which is why ``ZoomMeetingSession`` holds two separately-supervised legs.

Teams has one: a single ``LocalMediaSession`` bound to a single Graph call carries both
directions, so one object owns it and both legs report its health.

Google Meet is like Teams in that respect and more so. **One browser tab is the
participant.** The inbound tap and the outbound synthetic devices are the same page, in the
same renderer, on the same peer connection. There is no state in which Meet ingest works
while Meet egress does not — if the tab is gone, the avatar is not in the meeting at all.
So this object owns the browser, the profile, the channel, the join, the roster and
recovery, and the two port adapters are views onto it.

**Recovery is a full relaunch, not a reconnect.** Teams re-creates its Graph call because a
media session cannot outlive its call's signalling. The same is true here and then some: a
crashed renderer takes the peer connection, both ``AudioContext`` graphs, the canvas, and
the synthetic tracks with it, and none can be reattached. So a rejoin closes the browser,
discards the working profile, takes a fresh one from the template, launches again, and
joins again. That is expensive — several seconds — which is precisely why
``monitoring/watchdog.py`` exists to distinguish "the tab died" from "nobody is speaking".

**What this class deliberately does not do.** It records no metrics. Frame counting lives in
the three adapters (``audio_capture/audio_source.py``, ``virtual_camera/adapter.py``,
``virtual_microphone/adapter.py``), because that is where frames actually are and it keeps
this file about browser control. It also holds no media formats of its own beyond passing
them to the page, no decoding, and no avatar knowledge.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from src.connectors.google_meet.audio_capture.mapping import to_audio_frame
from src.connectors.google_meet.auth.google_login import attempt_password_login
from src.connectors.google_meet.automation.driver import BrowserDriver, PlaywrightDriver
from src.connectors.google_meet.automation.selectors import DEFAULT_SELECTORS, MeetSelectors
from src.connectors.google_meet.browser.launcher import build_launch_plan
from src.connectors.google_meet.browser.profile import ProfileLease, ProfileManager
from src.connectors.google_meet.config import GoogleMeetConnectorConfig
from src.connectors.google_meet.exceptions import (
    BridgeProtocolError,
    BridgeUnavailableError,
    GoogleMeetError,
    MeetConfigurationError,
)
from src.connectors.google_meet.js import load_assets
from src.connectors.google_meet.meeting.active_speaker import SpeakerTracker
from src.connectors.google_meet.meeting.chat import MeetChatSource
from src.connectors.google_meet.meeting.controls import MeetControls
from src.connectors.google_meet.meeting.hand_raise import MeetHandRaiseSource
from src.connectors.google_meet.meeting.join import MeetJoiner
from src.connectors.google_meet.meeting.meet_url import resolve_join_target
from src.connectors.google_meet.meeting.participants import MeetRoster, parse_roster
from src.connectors.google_meet.meeting.transcript import MeetTranscript
from src.connectors.google_meet.reconnect.classify import build_policy, is_fatal
from src.connectors.google_meet.websocket.protocol import (
    MeetMessage,
    MeetMessageType,
    MeetState,
)
from src.connectors.google_meet.websocket.server import PageBridgeServer
from src.domain.context import FrameContext
from src.domain.health import ComponentHealth, ComponentState
from src.domain.media import AudioFrame
from src.domain.meeting import MeetingContext
from src.infrastructure.logging import get_logger
from src.infrastructure.reconnect import ReconnectPolicy
from src.services.media.clock import MediaClock
from src.services.media.queues import BoundedFrameQueue, OverflowPolicy

logger = get_logger(__name__)

COMPONENT_NAME = "google_meet_bridge"

CAPTURE_FRAME_MS = 20
"""Frame size the capture worklet batches to. 20 ms at 16 kHz is 320 samples — the same
cadence Zoom's RTMS leg is configured for, so the avatar sees an identical arrival pattern
whichever platform it is talking to."""

PLAYOUT_BUFFER_SECONDS = 0.5
"""Ring-buffer *capacity* in the playout worklet — the hard ceiling, past which audio is
dropped outright. Half a second absorbs a scheduling hiccup between the Pacer's media clock
and the browser's audio clock without adding audible latency; deeper would trade
responsiveness for smoothness in a conversation, which is the wrong direction."""

PLAYOUT_TARGET_SECONDS = 0.12
"""Standing depth the playout ring is trimmed back towards, through silence only.

**A capacity is not a target, and treating one as the other is where the avatar's replies
went late.** Nothing ever removed samples from that ring except overflow at the capacity
above, so every hiccup between two uncorrected clocks added to the standing depth and none of
it ever came back: the buffer ratcheted up towards half a second over the course of a call and
stayed there, which is half a second of delay on every answer, growing, with every health
check green.

120 ms is comfortably more than the jitter between a 20 ms arrival cadence and a 128-sample
render quantum, and it is reached by discarding silent blocks between utterances — never
speech. See ``js/playout_worklet.js``."""

PLAYOUT_WARN_INTERVAL_US = 60_000_000
"""How often to report a standing playout backlog. Rate limited because the condition that
produces it produces it on every heartbeat."""

HEARTBEAT_INTERVAL_MS = 5_000
DOM_SCAN_INTERVAL_MS = 2_000

DOM_SCAN_THROTTLE_MS = 250
"""Minimum gap between DOM scans, however often Meet mutates.

The scans were coalesced per animation frame, which bounds them at the repaint rate and not
below it — and Meet mutates on essentially every frame, so they ran at ~60 Hz for the whole
call. Each pass forces a full-document layout and reads ``innerText`` off every chat and
participant row, **on the renderer's main thread**: the same thread encoding the avatar's
720p camera track and handing its PCM to the playout worklet. Starving it delays the media,
which is heard as an avatar that answers slowly.

A quarter of a second is below what a person notices in a reply and far above what the DOM
needs to settle. Chat, the roster and the meeting state all change at human speed."""

CHAT_OPEN_WINDOW_MS = 90_000
"""How long, after being admitted, the page keeps trying to open the chat panel.

**A duration, not a number of scans, and the difference is the whole point.** The budget was
originally ten *scans*, and scans are driven by DOM mutations — Meet mutates continuously, so ten
attempts elapsed in about a second and a half and the page gave up permanently before Meet had
even drawn its in-call control bar. Ninety seconds of wall clock is what "keep trying while Meet
finishes rendering" actually means."""

CHAT_BASELINE_MS = 3_000
"""How long after the chat panel opens its contents count as history rather than as questions.

Timed from the panel opening, **not from the first message seen** — that distinction is the fix
for a bug where the avatar ignored whatever anybody typed first and only began replying from the
second message. Baselining used to mean "the first scan that finds any messages is history", and
an empty panel produced no such scan, so the user's opening message became the baseline.

Three seconds is comfortably longer than Meet takes to render an existing backlog and far shorter
than anyone takes to read the room and type. A backlog is still skipped; an empty panel simply
lets the window lapse."""

CHAT_OPEN_RETRY_MS = 1_500
"""Minimum gap between attempts to open the chat panel.

Without it the mutation-driven scan would click many times a second, which fights Meet's own
layout work and can toggle a panel back shut as fast as it opens."""

HAND_RAISE_BASELINE_MS = 3_000
"""How long after being admitted a hand that is already up counts as pre-existing.

Somebody whose hand was up before the avatar walked in is not interrupting it — it had not
said anything yet. Without this the first scan after joining would report every raised hand in
the room at once and the avatar would open by yielding the floor to all of them."""

HAND_RAISE_SWEEP_MS = 500
"""How often the page sweeps every labelled element looking for a raised hand.

The narrow selector pass runs on every scan; this is the broad one that actually finds it when
Meet's markup has moved, and it reads a few hundred elements. Twice a second is far faster than
anybody notices a hand going up and far cheaper than running it per animation frame — Meet
mutates the DOM continuously, and the scan is driven by those mutations."""

HAND_RAISE_DIAG_MS = 20_000
"""How long to wait before reporting that no raised hand has been seen, and what the page does
have.

Emitted at most four times, and only while nothing has ever been detected. This is the lesson
from the chat button, which cost two rounds of guessing Meet's ARIA labels from the outside:
when a DOM-reading feature finds nothing, the useful artefact is the list of labels that *are*
on the page. Silent failure is the one outcome worth spending log lines to avoid."""

HAND_RAISE_COOLDOWN_MS = 5_000
"""Minimum gap between reports of the *same* participant's hand, applied in the page.

The page's floor, not the policy: ``MeetHandRaiseSource`` holds the real cooldown, because a
rate limit that survives a rejoin has to live in Python. This one exists so a DOM re-render
that momentarily drops the indicator cannot put a burst on the wire in the first place."""

HAND_RAISE_DOWN_GRACE_MS = 5_000
"""How long a hand must go **unseen** before the page treats it as lowered.

**This is what stops an avatar telling somebody "ok, go ahead" every few seconds while their
hand has not moved.** The page reports a hand's *transition*, so it has to know which hands
are already up — and it used to rebuild that set from whatever the current scan happened to
find. Only the twice-a-second sweep finds anything, because Meet renders the evidence as text
and icon glyphs rather than as attributes; every scan in between found nothing and emptied
the set, so the next sweep re-reported the same unmoved hand as a new one. The cooldowns above
then spaced the repeats out into an interruption on a timer, arriving in the middle of
whatever the meeting was doing — including somebody typing questions into the chat.

A grace window spanning several sweeps is what makes the set mean what it says. It has to,
because a raised hand really does vanish from the DOM for a moment when Meet re-renders a
tile, closes the people panel, or scrolls a participant out of its virtualised list. Matched
to the cooldown above: a re-raise inside that window would not be reported anyway, so a
shorter grace could only cost false interruptions and could buy nothing."""

SPEAKER_SAMPLE_MS = 200
"""How often the page measures each remote track's level.

**On its own timer rather than on the DOM scan, and that is the design.** Every other observer
in this connector is driven by Meet's mutations, which is right for things that change when
somebody clicks. Speech is not one: a meeting can sit visually still while a person talks for a
minute, and a turn sampled at the mutation rate would start late and end later.

Affordable because it touches no DOM at all — it reads 512 bytes out of an ``AnalyserNode`` per
participant and does arithmetic on them. That is why it can run five times a second while the
scans that force a layout are floored at four; the two costs are not comparable. 200 ms also
bounds the error on a turn boundary, which is what makes "who was speaking then" answerable."""

SPEAKER_MAP_MS = 2_000
"""How often the page re-checks which participant each remote stream is rendered on.

Property reads (``srcObject``, ``closest``) rather than text, so no layout is forced — but it
walks every media element on the page, and the answer only changes when somebody joins, leaves,
or Meet re-lays-out its tiles. All of those are human-speed."""

SPEAKER_DOM_SCAN_MS = 400
"""Floor between passes over Meet's own speaking indicator.

The fallback signal, and the only one that can be broken by a Meet release — so it is rate
limited below the energy sampler and above the DOM scan floor. It matters most in the case the
energy path cannot cover: a participant whose stream never appears on a tile the page can read."""

SPEAKER_START_LEVEL = 0.02
"""RMS at which a track counts as somebody talking, on a 0-1 scale.

Two thresholds rather than one, because a single one makes a speaker flicker on and off across
every consonant and each flicker is a turn boundary. This is the level a voice has to reach to
take the floor; ``SPEAKER_STOP_LEVEL`` is the lower one it has to fall below to give it up.

Deliberately above room tone and below conversational speech. It is a floor rather than a tuned
value: the page reports the measured level with every edge, so a room that needs a different
number can be tuned from a reading — the same discipline ``speech_interrupt_threshold`` uses."""

SPEAKER_STOP_LEVEL = 0.012
"""RMS below which a track is no longer speaking, once ``SPEAKER_RELEASE_MS`` has passed."""

SPEAKER_RELEASE_MS = 600
"""How long a track must stay quiet before its turn ends.

Short on purpose. This decides when a turn *ends*, and a long release would report somebody as
speaking through the other person's reply. The complementary judgement — whether a gap ended
the turn or merely punctuated it — is made in Python, where a rejoin can look at the turn it is
rejoining. See ``meeting/active_speaker.py``."""

SPEAKER_DIAG_MS = 30_000
"""How long to wait before reporting that nobody has been heard, and what the page does have.

The lesson from the chat button and the hand indicator, applied before it costs a round of live
testing this time: a DOM-reading feature that finds nothing is indistinguishable from a quiet
meeting. Emitted at most four times, and only while nothing has ever been attributed."""

CAPTION_OPEN_RETRY_MS = 2_000
"""Minimum gap between attempts to switch captions on. Bounded by ``CHAT_OPEN_WINDOW_MS``, which
is the same wall-clock budget and exists for the same reason: Meet has not drawn its control bar
in the first seconds of a call, and a scan-count budget is spent before it does."""

CAPTION_SCAN_MS = 400
"""Floor between reads of the caption panel.

The panel is small — a few short lines — so this is the one place in the bridge where reading
``innerText`` is affordable, and the only place it buys something no cheaper read can: Meet
renders the speaker's name and the caption as separate blocks, so the *rendered* line break is
what separates the name from the words. ``textContent`` runs them together."""

CAPTION_SETTLE_MS = 1_200
"""How long a caption must stop changing before it counts as a finished line.

Meet extends a caption word by word while somebody talks, so forwarding on sight would deliver a
dozen fragments of one sentence and the transcript would read as stutter. A little over a second
is longer than the gap between words and shorter than the gap between turns."""

CAPTION_DIAG_MS = 25_000
"""How long to wait before reporting that no caption has been captured, and what the panel holds.

Emitted at most three times. Same discipline as the hand-raise and speaker diagnostics, and the
same reason: a DOM-reading feature that finds nothing looks exactly like a quiet meeting."""

AUDIO_ENFORCE_INTERVAL_MS = 2_000
"""How often the page re-checks that Meet's audio sender still carries the avatar's track.

A reconciliation loop rather than a one-off, because Meet's audio transceiver does not exist
until negotiation completes and Meet may swap the track again on a device change or an ICE
restart. Each pass is a few property reads and replaces nothing when the track is already
ours, so the interval is cheap; it exists to close the window between Meet creating a sender
and anything noticing."""

DriverFactory = Callable[[], BrowserDriver]
RosterListener = Callable[[MeetRoster], None]
StateListener = Callable[[MeetState], None]


@dataclass(frozen=True, slots=True)
class PageReady:
    """What the page reported once its media graph was live.

    Verified rather than trusted — see ``_verify_page_media``. A page that silently built a
    48 kHz capture context would otherwise feed the avatar audio it cannot use, and the
    symptom (a fast, high-pitched voice) points at the avatar service rather than here.
    """

    capture_sample_rate_hz: int
    publish_sample_rate_hz: int
    video_width: int
    video_height: int


class ChromiumBridge:
    """The browser that is the avatar's presence in one Google Meet conference."""

    __slots__ = (
        "_channel",
        "_chat",
        "_clock",
        "_config",
        "_controls",
        "_ctx",
        "_detail",
        "_driver",
        "_driver_factory",
        "_hands",
        "_inbound",
        "_lease",
        "_malformed_audio",
        "_meet_state",
        "_meeting",
        "_page_ready",
        "_playout_warned_at_us",
        "_policy",
        "_profiles",
        "_rejoins",
        "_roster",
        "_roster_listeners",
        "_selectors",
        "_server",
        "_speakers",
        "_state",
        "_state_listeners",
        "_task",
        "_transcript",
    )

    def __init__(
        self,
        *,
        config: GoogleMeetConnectorConfig,
        ctx: FrameContext,
        clock: MediaClock,
        policy: ReconnectPolicy | None = None,
        driver_factory: DriverFactory | None = None,
        selectors: MeetSelectors | None = None,
        profiles: ProfileManager | None = None,
    ) -> None:
        self._config = config
        self._ctx = ctx
        self._clock = clock
        self._policy = policy or build_policy(max_attempts=config.rejoin_max_attempts)
        # Injectable so the entire Meet pipeline can be exercised against an in-process
        # fake page — no Chromium, no Google account, no meeting. That is what makes this
        # connector developable and testable on the machines we actually have, and it is
        # the same move the removed Teams sidecar link made with its ``client_factory``.
        self._driver_factory = driver_factory or PlaywrightDriver
        self._selectors = selectors or DEFAULT_SELECTORS
        # Left unbuilt when not injected. ``profile_dir`` is optional — an unconfigured
        # deployment has none — so constructing a manager here would need a path that does
        # not exist yet. It is built in ``_launch_and_join``, after
        # ``require_configured()`` has narrowed the value.
        self._profiles: ProfileManager | None = profiles

        self._driver: BrowserDriver | None = None
        self._server: PageBridgeServer | None = None
        self._channel: object | None = None
        self._controls: MeetControls | None = None
        self._lease: ProfileLease | None = None
        self._meeting: MeetingContext | None = None
        self._page_ready: PageReady | None = None
        # Set by the session factory when chat is enabled. Optional rather than always
        # constructed, so a chat-disabled session carries no chat surface at all — the same
        # shape ``MeetingService`` uses for an unconfigured connector.
        self._chat: MeetChatSource | None = None
        self._hands: MeetHandRaiseSource | None = None
        # Attached by the session factory when speaker tracking is on, on the same terms as the
        # two above: a session with it off carries no tracker at all rather than an inert one.
        self._speakers: SpeakerTracker | None = None
        self._transcript: MeetTranscript | None = None

        self._inbound: BoundedFrameQueue[AudioFrame] = BoundedFrameQueue(
            name="google_meet_inbound",
            maxsize=config.inbound_queue_size,
            policy=OverflowPolicy.DROP_OLDEST,
        )

        self._meet_state: MeetState | None = None
        self._roster = MeetRoster()
        self._roster_listeners: list[RosterListener] = []
        self._state_listeners: list[StateListener] = []

        self._state = ComponentState.UNKNOWN
        self._detail: str | None = None
        self._task: asyncio.Task[None] | None = None
        self._rejoins = 0
        self._malformed_audio = 0
        # ``None`` rather than zero, so the first deep buffer is reported when it happens
        # rather than being swallowed by a window that started at session start.
        self._playout_warned_at_us: int | None = None

    # -- lifecycle ---------------------------------------------------------

    async def start(self, meeting: MeetingContext) -> None:
        """Launch, join, and begin carrying media in both directions.

        The first join happens **inline** rather than in the background, deliberately: an
        unsigned-in profile, a missing Chromium, an invalid meeting code, or a host who
        denies entry then fails ``POST /sessions`` with a precise reason, instead of
        returning 202 and leaving an operator to discover from a health endpoint that the
        avatar never arrived. The same choice the removed Teams sidecar link made.

        Raises:
            MeetConfigurationError: the connector is not configured.
            PlaywrightUnavailableError: no browser to launch. Fatal.
            GoogleAuthError: the profile has no Google session. Fatal.
            MeetingAdmissionError: entry was denied or the avatar was removed. Fatal.
            MeetingEndedError: the conference is over.
            JoinTimeoutError / BrowserError / BridgeUnavailableError: recoverable.
            MeetUrlError: the meeting cannot be resolved into a Meet URL.
        """
        if self._task is not None:
            return
        self._meeting = meeting

        await self._launch_and_join(meeting)
        self._task = asyncio.create_task(self._supervise(), name="meet-bridge")

    async def stop(self) -> None:
        """Leave the meeting and close the browser. Idempotent."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            # We cancelled it deliberately; its cancellation is not an error.
            with contextlib.suppress(asyncio.CancelledError):
                await task

        await self._leave_and_close()
        self._inbound.close()
        self._state = ComponentState.UNKNOWN
        self._detail = "stopped"
        self._meet_state = None
        self._roster = MeetRoster()

    # -- ingest ------------------------------------------------------------

    def audio_queue(self) -> BoundedFrameQueue[AudioFrame]:
        """The queue inbound conference audio is delivered into."""
        return self._inbound

    # -- egress ------------------------------------------------------------

    async def send_audio(self, payload: bytes) -> bool:
        """Send one encoded ``AUDIO_PCM`` message to the page.

        Returns False when the channel is down, rather than raising. The Pacer runs a
        continuous cadence and is the same shared component all three connectors use;
        letting an error escape would tear its task group down mid-rejoin, when this bridge
        may be seconds from healing itself. The caller counts the drop and the leg is marked
        degraded, which is what the supervisor acts on.
        """
        channel = self._live_channel()
        if channel is None:
            return False
        try:
            await channel.send_raw(payload)
        except BridgeUnavailableError as exc:
            self._degrade(f"audio write failed: {exc}")
            return False
        return True

    async def send_video(self, payload: bytes) -> bool:
        """Send one encoded ``VIDEO_I420`` message, dropping it under backpressure.

        Returns False when the frame was dropped or the channel is down. See
        ``websocket/channel.py`` for why the backpressure bound is "one send in flight"
        rather than a byte threshold.
        """
        channel = self._live_channel()
        if channel is None:
            return False
        try:
            return await channel.try_send_video(payload)
        except BridgeUnavailableError as exc:
            self._degrade(f"video write failed: {exc}")
            return False

    def _live_channel(self):
        channel = self._server.channel if self._server is not None else None
        if channel is None or not channel.is_connected:
            return None
        return channel

    # -- observation -------------------------------------------------------

    def health(self) -> ComponentHealth:
        """Current health. Must not block — called from the supervisor's poll loop."""
        if self._state is ComponentState.HEALTHY:
            driver = self._driver
            if driver is None or not driver.is_alive():
                return ComponentHealth.unhealthy(COMPONENT_NAME, "chromium is gone")
            if self._live_channel() is None:
                return ComponentHealth.unhealthy(COMPONENT_NAME, "page channel disconnected")
        return ComponentHealth(name=COMPONENT_NAME, state=self._state, detail=self._detail)

    def attach_chat(self, chat: MeetChatSource) -> None:
        """Route observed chat messages into ``chat``.

        Injected rather than constructed here so the bridge keeps knowing nothing about who
        consumes chat, and so a session with chat disabled never creates the queue.
        """
        self._chat = chat

    def attach_hand_raise(self, hands: MeetHandRaiseSource) -> None:
        """Route observed raised hands into ``hands``.

        Injected for the same reason chat is: the bridge stays ignorant of who consumes the
        events, and a session with the feature off never creates the surface.
        """
        self._hands = hands

    def attach_speakers(self, speakers: SpeakerTracker) -> None:
        """Route observed speaking edges into ``speakers``.

        Injected for the reason chat and hands are: the bridge stays ignorant of who consumes
        the events, and a session with the feature off never creates the surface.
        """
        self._speakers = speakers

    def attach_transcript(self, transcript: MeetTranscript) -> None:
        """Route settled caption lines *and* observed chat messages into ``transcript``.

        Injected on the same terms as the rest: the bridge learns nothing about who consumes
        them. Both feeds, because a meeting's conversation is spoken and typed and the ledger
        that answers "what was said?" has to hold both — see ``_on_chat``.
        """
        self._transcript = transcript

    async def page_stats(self) -> dict[str, Any] | None:
        """Read ``window.__MC_BRIDGE_STATS__()`` from the live page.

        **The only window onto the outbound half of the media path.** Everything else this
        connector reports is either upstream of the browser (frames delivered to the page) or
        about inbound audio. Whether Meet is *publishing* our synthetic tracks is knowable
        only from inside the renderer — and a headless browser has nothing to inspect.

        ``bridge.js`` has exposed this hook since it was written and no Python code ever
        called it, which left exactly one question unanswerable from the outside: an avatar
        that joins, unmutes, is handed every frame on time, and cannot be heard. That is the
        failure this returns the evidence for; ``micClonesIssued == 0`` means Meet never took
        our microphone.

        Returns ``None`` when the page is gone or the hook is absent, because a diagnostic
        must never be the reason a session dies.
        """
        driver = self._driver
        if driver is None:
            return None
        try:
            stats = await driver.evaluate(
                "(() => (typeof window.__MC_BRIDGE_STATS__ === 'function'"
                " ? window.__MC_BRIDGE_STATS__() : null))()"
            )
        except Exception as exc:
            logger.debug("meet_bridge.page_stats_unavailable", error=str(exc))
            return None
        return stats if isinstance(stats, dict) else None

    @property
    def meet_state(self) -> MeetState | None:
        """Where the browser is in the meeting, as the page last reported."""
        return self._meet_state

    @property
    def roster(self) -> MeetRoster:
        return self._roster

    @property
    def page_ready(self) -> PageReady | None:
        """The media parameters the page confirmed, once joined."""
        return self._page_ready

    @property
    def is_joined(self) -> bool:
        return self._page_ready is not None and self._state.is_serving

    @property
    def rejoins(self) -> int:
        return self._rejoins

    @property
    def stats(self) -> dict[str, int]:
        channel = self._live_channel()
        return {
            "rejoins": self._rejoins,
            "malformed_audio": self._malformed_audio,
            "dropped_video": channel.dropped_video if channel is not None else 0,
            "inbound_dropped": self._inbound.dropped,
            "participants": self._roster.count,
            "others": len(self._roster.others),
            "speaker_events": self._speakers.events if self._speakers is not None else 0,
        }

    def add_roster_listener(self, listener: RosterListener) -> None:
        """Register a callback for roster changes. Fired immediately if one is known."""
        self._roster_listeners.append(listener)
        if self._roster.count:
            listener(self._roster)

    # -- join --------------------------------------------------------------

    async def _launch_and_join(self, meeting: MeetingContext) -> None:
        """Do everything from a cold start to publishing media.

        The ordering is not arbitrary and each step depends on the one before:

        1. Resolve the URL *first*, so a bad meeting code costs nothing.
        2. Start the bridge server, because its port has to be known before the browser
           launches — the endpoint is baked into an init script.
        3. Take a working profile from the template, so this session cannot corrupt
           another's Google session.
        4. Launch, then inject. Injection must precede any navigation, or Meet captures a
           pristine ``getUserMedia`` and the synthetic devices are never used.
        5. Join. This is where a human may have to click Admit.
        6. *Then* attach to the page channel — after joining, not before. Navigating to
           Meet replaces the socket the sign-in probe opened, so a channel captured
           earlier would be stale.
        7. Configure the page's media graph and wait for it to confirm.
        8. Unmute and turn the camera on, because Meet does not publish tracks it was
           handed until told to.

        Raises:
            MeetConfigurationError: no profile directory is configured. Checked here rather
                than in ``start`` so that a rejoin re-validates it too.
        """
        # Both resolved before anything is allocated: a bad meeting code and a missing
        # profile directory must fail before a socket is bound or a browser is launched.
        target = resolve_join_target(meeting)
        profiles = self._profile_manager()

        server = PageBridgeServer(
            host=self._config.bridge_host, port=self._config.bridge_port
        )
        await server.start()
        self._server = server

        self._lease = profiles.acquire(self._ctx.session_id)
        plan = build_launch_plan(
            user_data_dir=self._lease.path,
            video_format=self._config.video_format,
            headless=self._config.headless,
            executable_path=self._config.chromium_executable,
            extra_args=self._config.extra_browser_args,
            timeout_s=self._config.browser_launch_timeout_s,
        )

        driver = self._driver_factory()
        await driver.start(plan)
        self._driver = driver
        await self._inject(driver, server)

        await self._bootstrap_auth_if_needed(driver)

        joiner = MeetJoiner(
            driver=driver,
            selectors=self._selectors,
            display_name=self._config.display_name,
            join_timeout_s=self._config.join_timeout_s,
            lobby_timeout_s=self._config.lobby_timeout_s,
        )
        outcome = await joiner.join(target)
        self._meet_state = outcome.state

        if self._config.disable_injection:
            # Fail here rather than at ``wait_for_page``, and fail *fatally*. Otherwise the next
            # line blocks for bridge_ready_timeout_s waiting for a page that has no script to
            # connect with, and BridgeUnavailableError is classed recoverable — so the bridge
            # would relaunch the browser once per rejoin attempt to reach the same conclusion.
            # MeetConfigurationError is in FATAL_ERRORS, so this ends the session at once.
            raise MeetConfigurationError(
                "MC_GOOGLE_MEET__DISABLE_INJECTION is set, so js/bridge.js was not injected. "
                f"The browser joined {target} successfully, but nothing can connect to the page "
                "bridge: there is no synthetic camera or microphone, no conference-audio tap, "
                "and no channel to carry frames. This session cannot publish an avatar or hear "
                "anything, so it is failed now rather than left running and silent. Unset the "
                "flag to carry media."
            )

        channel = await server.wait_for_page(timeout_s=self._config.bridge_ready_timeout_s)
        await channel.send_json(MeetMessageType.CONFIG, self._page_config(server))
        ready = await channel.await_message(
            MeetMessageType.READY, timeout_s=self._config.bridge_ready_timeout_s
        )
        self._page_ready = self._verify_page_media(ready)

        self._controls = MeetControls(driver=driver, selectors=self._selectors)
        audio_live, video_live = await self._controls.publish_both()

        # **Video counts toward health, and it did not used to.** ``video_live`` was computed
        # here and then dropped on the floor: a camera that never came on left the component
        # ``HEALTHY``, so the one trace of it was a single ``video_published=False`` field in
        # the log line below. That is precisely the failure that shipped — the avatar in the
        # meeting as an initial rather than a face, with nothing in ``GET /sessions/{id}``
        # saying so. Audio-only is genuinely degraded-but-useful (``publish_both``'s own
        # docstring says as much), which is what ``DEGRADED`` is for.
        if audio_live and video_live:
            self._state = ComponentState.HEALTHY
            self._detail = None
        else:
            self._state = ComponentState.DEGRADED
            self._detail = (
                "the avatar could not unmute"
                if not audio_live
                else "the avatar's camera could not be confirmed on; it may appear as an "
                "initial rather than as a person"
            )

        logger.info(
            "meet_bridge.joined",
            meeting=str(target),
            lobby_wait_s=round(outcome.waited_in_lobby_s, 1),
            audio_published=audio_live,
            video_published=video_live,
            capture_hz=self._page_ready.capture_sample_rate_hz,
            publish_hz=self._page_ready.publish_sample_rate_hz,
        )

    async def _inject(self, driver: BrowserDriver, server: PageBridgeServer) -> None:
        """Install the page-side configuration and the bridge script, in that order.

        Two scripts rather than one, because Playwright's argument passing would require
        the asset to be a function expression — which would mean ``bridge.js`` could not be
        a plain, lintable script. A preamble that defines two globals is simpler and keeps
        the asset honest.

        The worklet sources travel as globals too, and never as files: ``bridge.js`` wraps
        each in a ``Blob`` and calls ``addModule`` on the object URL, so no HTTP server has
        to exist to serve them.
        """
        if self._config.disable_injection:
            # Warning, not info: this switch disables the entire media path, and it must be
            # impossible to find a silent session later and wonder why nothing flowed.
            logger.warning(
                "meet_bridge.injection_disabled",
                note="js/bridge.js was NOT injected, so this session cannot carry media in "
                "either direction; unset MC_GOOGLE_MEET__DISABLE_INJECTION to publish an avatar",
            )
            return

        assets = load_assets()
        preamble = (
            f"window.__MC_BRIDGE_CONFIG__ = {json.dumps(self._page_config(server))};\n"
            "window.__MC_BRIDGE_WORKLETS__ = {"
            f"capture: {json.dumps(assets.capture_worklet)},"
            f"playout: {json.dumps(assets.playout_worklet)}"
            "};\n"
        )
        await driver.add_init_script(preamble)
        await driver.add_init_script(assets.bridge)

    def _profile_manager(self) -> ProfileManager:
        """The profile manager, built on first use.

        Deferred rather than built in ``__init__`` because ``profile_dir`` is optional — an
        unconfigured deployment has none — and ``require_configured`` is what turns it into a
        checked path. Cached, so a rejoin reuses the same template and does not re-validate
        on every attempt.

        Raises:
            MeetConfigurationError: no profile directory is configured.
        """
        existing = self._profiles
        if existing is not None:
            return existing
        built = ProfileManager(template=self._config.require_configured())
        self._profiles = built
        return built

    def _page_config(self, server: PageBridgeServer) -> dict[str, object]:
        """Everything the page needs to know, pushed rather than hardcoded in the asset.

        Media geometry and the selector set both live in Python, so adapting to a Meet UI
        change or a different publish resolution is a settings edit reviewable in a diff —
        not a change to the asset that also contains the media path.
        """
        video = self._config.video_format
        return {
            "endpoint": server.endpoint,
            "captureSampleRateHz": self._config.ingest_audio_format.sample_rate_hz,
            "captureFrameMs": CAPTURE_FRAME_MS,
            "publishSampleRateHz": self._config.publish_audio_format.sample_rate_hz,
            "playoutBufferSeconds": PLAYOUT_BUFFER_SECONDS,
            "playoutTargetSeconds": PLAYOUT_TARGET_SECONDS,
            "audioEnforceIntervalMs": AUDIO_ENFORCE_INTERVAL_MS,
            "chatEnabled": self._config.chat_enabled,
            "chatOpenWindowMs": CHAT_OPEN_WINDOW_MS,
            "chatOpenRetryMs": CHAT_OPEN_RETRY_MS,
            "chatBaselineMs": CHAT_BASELINE_MS,
            "handRaiseEnabled": self._config.hand_raise_enabled,
            "handRaiseBaselineMs": HAND_RAISE_BASELINE_MS,
            "handRaiseCooldownMs": HAND_RAISE_COOLDOWN_MS,
            "handRaiseDownGraceMs": HAND_RAISE_DOWN_GRACE_MS,
            "handRaiseSweepMs": HAND_RAISE_SWEEP_MS,
            "handRaiseDiagMs": HAND_RAISE_DIAG_MS,
            "speakerTrackingEnabled": self._config.speaker_tracking_enabled,
            "speakerSampleMs": SPEAKER_SAMPLE_MS,
            "speakerMapMs": SPEAKER_MAP_MS,
            "speakerDomScanMs": SPEAKER_DOM_SCAN_MS,
            "speakerStartLevel": SPEAKER_START_LEVEL,
            "speakerStopLevel": SPEAKER_STOP_LEVEL,
            "speakerReleaseMs": SPEAKER_RELEASE_MS,
            "speakerDiagMs": SPEAKER_DIAG_MS,
            "captionsEnabled": self._config.captions_enabled,
            "captionOpenWindowMs": CHAT_OPEN_WINDOW_MS,
            "captionOpenRetryMs": CAPTION_OPEN_RETRY_MS,
            "captionScanMs": CAPTION_SCAN_MS,
            "captionSettleMs": CAPTION_SETTLE_MS,
            "captionDiagMs": CAPTION_DIAG_MS,
            "videoWidth": video.width,
            "videoHeight": video.height,
            "videoFps": video.fps,
            "displayName": self._config.display_name,
            "heartbeatIntervalMs": HEARTBEAT_INTERVAL_MS,
            "scanIntervalMs": DOM_SCAN_INTERVAL_MS,
            "scanThrottleMs": DOM_SCAN_THROTTLE_MS,
            "selectors": self._selectors.to_page_config(),
            # Absent rather than empty when unrestricted: bridge.js reads a missing key as
            # "install everything", and an empty list as "install nothing".
            **(
                {"stages": list(self._config.inject_stages)}
                if self._config.inject_stages
                else {}
            ),
        }

    def _verify_page_media(self, ready: MeetMessage) -> PageReady:
        """Reject a page whose media graph does not match what we configured.

        A silent mismatch is the expensive failure here. The wrong capture rate produces
        audio the avatar interprets at the wrong speed, and the wrong canvas size produces
        a sheared or letterboxed frame — both of which look like a decoder bug from the
        Python side and cost hours to trace back through a headless browser.

        Raises:
            BridgeProtocolError: the page's graph differs from the request, which in
                practice means a stale injected script.
        """
        body = ready.json()
        page = PageReady(
            capture_sample_rate_hz=int(body.get("capture_sample_rate_hz") or 0),
            publish_sample_rate_hz=int(body.get("publish_sample_rate_hz") or 0),
            video_width=int(body.get("video_width") or 0),
            video_height=int(body.get("video_height") or 0),
        )

        expected_capture = self._config.ingest_audio_format.sample_rate_hz
        if page.capture_sample_rate_hz != expected_capture:
            raise BridgeProtocolError(
                f"the page built a {page.capture_sample_rate_hz} Hz capture context but "
                f"the avatar contract requires {expected_capture} Hz"
            )
        expected_publish = self._config.publish_audio_format.sample_rate_hz
        if page.publish_sample_rate_hz != expected_publish:
            raise BridgeProtocolError(
                f"the page built a {page.publish_sample_rate_hz} Hz playout context but "
                f"{expected_publish} Hz was requested"
            )
        video = self._config.video_format
        if (page.video_width, page.video_height) != (video.width, video.height):
            raise BridgeProtocolError(
                f"the page's camera canvas is {page.video_width}x{page.video_height} but "
                f"{video.width}x{video.height} was requested"
            )
        return page

    async def _bootstrap_auth_if_needed(self, driver: BrowserDriver) -> None:
        """Attempt a scripted sign-in, but only for a profile that has never been used.

        Gated on the template looking unauthenticated, so a steady-state deployment never
        touches Google's sign-in flow — which is the point. Re-running a scripted login
        against an account that already has a session is how a browser gets challenged.

        A failure here is not raised: ``MeetJoiner`` runs ``verify_signed_in`` immediately
        afterwards and produces the better error, naming the specific obstacle Google
        presented rather than "bootstrap failed".
        """
        if self._profile_manager().is_authenticated():
            return
        if not self._config.google_email:
            return

        status = await attempt_password_login(
            driver,
            email=self._config.google_email,
            password=self._config.google_password.get_secret_value(),
            timeout_s=self._config.join_timeout_s,
        )
        if not status.signed_in:
            logger.error("meet_bridge.bootstrap_failed", detail=status.detail)

    async def _leave_and_close(self) -> None:
        """Leave the call, close the browser, and release the profile.

        Order matters. Leaving first removes the avatar from the roster immediately;
        killing the browser without it leaves a frozen tile until Meet times the peer
        connection out, which is minutes of a dead avatar sitting in a customer's meeting.

        Every step is best-effort: teardown must not be able to fail a session stop, and by
        this point the browser is going away regardless.
        """
        controls, self._controls = self._controls, None
        if controls is not None:
            with contextlib.suppress(GoogleMeetError, OSError):
                await controls.leave()
                # A moment for Meet to send the leave signal before the tab dies with it.
                await asyncio.sleep(0.5)

        driver, self._driver = self._driver, None
        if driver is not None:
            with contextlib.suppress(GoogleMeetError, OSError):
                await driver.stop()

        server, self._server = self._server, None
        if server is not None:
            with contextlib.suppress(GoogleMeetError, OSError):
                await server.stop()

        lease, self._lease = self._lease, None
        if lease is not None and self._profiles is not None:
            self._profiles.release(lease)

        self._page_ready = None

    # -- supervision -------------------------------------------------------

    async def _supervise(self) -> None:
        """Read from the page until it fails, then rejoin. Repeats until told to stop."""
        while True:
            try:
                await self._read_loop()
                # A clean disconnect still means we have stopped hearing and stopped being
                # seen, so it is a failure to recover from rather than a reason to exit.
                raise BridgeUnavailableError("the page disconnected")
            except asyncio.CancelledError:
                await self._leave_and_close()
                raise
            except (GoogleMeetError, OSError) as exc:
                # ``reconnect/classify.py`` owns this decision so that this branch and the
                # one inside ``_rejoin`` cannot drift apart.
                if is_fatal(exc):
                    self._fail(str(exc))
                    return
                if not await self._rejoin(exc):
                    return

    async def _rejoin(self, cause: Exception) -> bool:
        """Relaunch the browser and rejoin, with backoff.

        The retry loop lives here rather than in ``_supervise`` so that a *failed* rejoin
        counts as an attempt and retries the join, instead of falling back into
        ``_read_loop`` on a browser that was never launched — which would block forever on a
        channel with nothing behind it and leave the reconnect budget permanently unspent,
        so the leg would sit unhealthy without ever being declared failed. Same shape as
        the two removed sidecar connectors' reconnect paths, deliberately.

        Returns:
            True once rejoined, False when the budget is spent or the failure is fatal.
        """
        meeting = self._meeting
        if meeting is None:  # pragma: no cover - start() always sets it
            self._fail("no meeting context to rejoin with")
            return False

        self._state = ComponentState.UNHEALTHY
        self._detail = str(cause)

        attempt = 0
        while True:
            attempt += 1
            if self._policy.exhausted(attempt):
                self._fail(f"rejoin budget exhausted after {attempt - 1} attempts: {cause}")
                return False

            await self._leave_and_close()
            delay = await self._policy.sleep(attempt)
            logger.warning(
                "meet_bridge.rejoining",
                attempt=attempt,
                delay_s=round(delay, 3),
                error=str(cause),
            )

            try:
                await self._launch_and_join(meeting)
            except (GoogleMeetError, OSError) as error:
                if is_fatal(error):
                    self._fail(str(error))
                    return False
                self._detail = str(error)
                continue

            # Buffered audio is stale by definition; replaying it would burst.
            self._inbound.clear()
            if self._speakers is not None:
                # The page that opened those turns is gone, so no stop edge for them will ever
                # arrive and whoever was mid-sentence would read as speaking for the rest of the
                # meeting. The turns themselves are kept — they did happen.
                self._speakers.reset()
            self._rejoins += 1
            logger.warning(
                "meet_bridge.rejoined",
                attempts=attempt,
                note="a new browser joined the meeting; audio during the gap is lost, and "
                "the avatar briefly left and re-entered the roster",
            )
            return True

    async def _read_loop(self) -> None:
        """Demultiplex messages from the page until it disconnects."""
        channel = self._live_channel()
        if channel is None:
            raise BridgeUnavailableError("no page is attached")

        async for message in channel.messages():
            await self._dispatch(message, channel)

    async def _dispatch(self, message: MeetMessage, channel) -> None:
        match message.msg_type:
            case MeetMessageType.AUDIO_PCM:
                self._on_audio(message)
            case MeetMessageType.PARTICIPANTS:
                self._on_participants(message)
            case MeetMessageType.MEET_STATE:
                self._on_meet_state(message)
            case MeetMessageType.HEARTBEAT:
                body = message.json()
                self._note_playout(body.get("playout") or {})
                await channel.send_json(
                    MeetMessageType.HEARTBEAT, {"sent_at_us": body.get("sent_at_us", 0)}
                )
            case MeetMessageType.ERROR:
                self._on_error(message)
            case MeetMessageType.CHAT_MESSAGE:
                self._on_chat(message)
            case MeetMessageType.HAND_RAISE:
                self._on_hand_raise(message)
            case MeetMessageType.ACTIVE_SPEAKER:
                self._on_active_speaker(message)
            case MeetMessageType.CAPTION:
                self._on_caption(message)
            case MeetMessageType.PAGE_EVENT:
                # The page reports facts and never interprets them, so this is the layer
                # that decides they are worth a log line at all.
                body = message.json()
                self._log_page_event(body.get("event"), body.get("detail") or {})
            case MeetMessageType.READY:
                # A second READY means the page rebuilt its media graph on its own — a
                # same-page navigation, or a device revoked and reacquired. Adopt the new
                # parameters rather than ignoring them.
                self._page_ready = self._verify_page_media(message)
                logger.info("meet_bridge.page_re_ready")
            case _:
                logger.warning(
                    "meet_bridge.unexpected_message", msg_type=message.msg_type.name
                )

    def _on_chat(self, message: MeetMessage) -> None:
        """Hand one observed chat message to the chat source, and to the transcript.

        Synchronous and total, like ``_on_audio``: this runs inside the read loop, which is the
        media channel. A coroutine could stall it and an exception would tear it down — and a
        dead read loop stops audio in both directions, which is a catastrophic price for a
        malformed chat payload. So the sink is a plain method that swallows.

        **Two sinks, and they are given different things on purpose.** The chat source decides
        what the avatar should *answer* — it filters on ``@mention``, drops the avatar's own
        messages, and queues the survivors. The transcript records what was *said*, and neither
        of those filters applies to history: a message between two participants is not the
        avatar's to answer and is still part of the conversation it will be asked to recall. So
        the transcript is fed the raw payload, independently, and a session with chat disabled
        keeps neither.
        """
        try:
            body = message.json()
        except Exception as exc:
            logger.warning("meet_bridge.chat_dropped", error=str(exc))
            return

        message_id = str(body.get("id") or "") or None
        chat = self._chat
        if chat is not None:
            try:
                chat.offer(body, message_id=message_id)
            except Exception as exc:
                logger.warning("meet_bridge.chat_dropped", error=str(exc))

        transcript = self._transcript
        if transcript is not None:
            # Second, and in its own guard: a bookkeeping failure must not cost the avatar the
            # answer to a question it was actually asked.
            try:
                transcript.offer_chat(body, message_id=message_id)
            except Exception as exc:
                logger.warning("meet_bridge.chat_transcript_dropped", error=str(exc))

    def _on_hand_raise(self, message: MeetMessage) -> None:
        """Hand one observed raised hand to the hand-raise source.

        Synchronous and total, like ``_on_chat``: this runs inside the read loop, and a
        malformed payload must not cost the meeting its audio in both directions.
        """
        hands = self._hands
        if hands is None:
            return
        try:
            body = message.json()
            hands.offer(body, event_id=str(body.get("id") or "") or None)
        except Exception as exc:
            logger.warning("meet_bridge.hand_raise_dropped", error=str(exc))

    def _on_active_speaker(self, message: MeetMessage) -> None:
        """Hand one speaking edge to the tracker.

        Synchronous and total, like ``_on_chat`` and ``_on_hand_raise``. This one arrives more
        often than either — several times a turn, per participant — which makes the rule it
        follows more load-bearing rather than less: it runs inside the read loop, and a malformed
        payload must cost an attribution rather than the meeting's audio in both directions.
        """
        speakers = self._speakers
        if speakers is None:
            return
        try:
            speakers.offer(message.json())
        except Exception as exc:
            logger.warning("meet_bridge.active_speaker_dropped", error=str(exc))

    def _on_caption(self, message: MeetMessage) -> None:
        """Hand one settled caption line to the transcript. Synchronous and total, like the rest.

        This is what makes "what did they ask you?" answerable, so it is worth being explicit that
        it still may not cost the meeting anything: a malformed payload from a DOM we do not
        control must lose a line of transcript, never the read loop that carries audio.
        """
        transcript = self._transcript
        if transcript is None:
            return
        try:
            transcript.offer(message.json())
        except Exception as exc:
            logger.warning("meet_bridge.caption_dropped", error=str(exc))

    def _note_playout(self, stats: dict[str, object]) -> None:
        """Say out loud when the avatar's voice is standing in a queue inside the browser.

        **The one part of this connector's latency that nothing could see.** The playout ring
        holds however much audio has accumulated between the Pacer's clock and the browser's,
        and that depth *is* delay on every reply — but it lived in a worklet, was reported only
        in a heartbeat body, and the heartbeat handler discarded it. A call whose answers had
        drifted half a second late logged exactly what a healthy one did.

        Rate limited to once a minute and warned about only past the trim target, so a buffer
        doing its job is silent and one that is not is visible where an operator already looks.
        Never raises: this is a log line inside the read loop.
        """
        try:
            buffered = int(stats.get("buffered") or 0)
        except (TypeError, ValueError):
            return
        if buffered <= 0:
            return

        rate = self._config.publish_audio_format.sample_rate_hz
        buffered_ms = buffered * 1_000 // rate
        if buffered_ms <= PLAYOUT_TARGET_SECONDS * 1_000 * 2:
            return

        now_us = self._clock.now_us()
        if (
            self._playout_warned_at_us is not None
            and now_us - self._playout_warned_at_us < PLAYOUT_WARN_INTERVAL_US
        ):
            return
        self._playout_warned_at_us = now_us
        logger.warning(
            "meet_bridge.playout_backlog",
            buffered_ms=buffered_ms,
            target_ms=int(PLAYOUT_TARGET_SECONDS * 1_000),
            underruns=stats.get("underruns"),
            dropped=stats.get("dropped"),
            trimmed=stats.get("trimmed"),
            note="the avatar's audio is queued inside the browser, and that depth is added "
            "to every reply; it is trimmed back through silence, so a backlog that persists "
            "means the page is not keeping up rather than that the trim is off",
        )

    def _log_page_event(self, event: str | None, detail: dict[str, object]) -> None:
        """Log one page diagnostic, promoting the one that explains a silent avatar.

        ``getUserMedia`` is reported once per acquisition and is the only direct evidence of
        whether **Meet is publishing our tracks at all**. Everything upstream of the browser
        can look perfect — PCM delivered, worklet fed, microphone unmuted — while Meet
        publishes a real (empty) device instead, because the patch is only installed after a
        navigation and Meet may have already acquired media. The symptom is an avatar nobody
        can hear, with no error anywhere.

        At ``debug`` this sat alongside a roster line every two seconds and was easy to miss.
        A once-per-join line earns ``info``, and a request that yielded no track of the kind
        it asked for earns ``warning`` — that combination is the diagnosis, not a hint.
        """
        if event == "handRaiseNothingSeen":
            # **Warning, and the highest-value line in this file when it appears.** It means the
            # page has been watching for a raised hand and has not recognised one — which is
            # indistinguishable, from every other signal the connector produces, from a meeting
            # where nobody raised their hand. The labels it carries are what Meet actually
            # renders, so the fix is a selector edit made from a reading rather than a guess.
            logger.warning(
                "meet_bridge.hand_raise_not_seen",
                seconds=detail.get("seconds"),
                labels_with_hand=detail.get("labelsWithHand"),
                # Text and icons as well as labels, and that is the lesson from the first live
                # run: it reported one label — our own toolbar button — while a participant's
                # hand was up, which is what proved the signal was not in the labels at all.
                # Meet marks the tile with an icon-font glyph, and a glyph is a text node.
                text_with_hand=detail.get("textWithHand"),
                icons_seen=detail.get("iconsSeen"),
                # Zero here means the tiles are not in the DOM at all, which is a different
                # problem from a wording miss and would otherwise look identical.
                participant_nodes=detail.get("participantNodes"),
                note="no raised hand recognised yet; if somebody did raise one, this is "
                "everything the page shows that mentions a hand",
            )
            return

        if event == "speakerNothingSeen":
            # **Warning, for the same reason ``handRaiseNothingSeen`` is.** It means the page has
            # been listening for a speaker and attributed nobody, which is indistinguishable from
            # a meeting where nobody has spoken. The counters separate the two failures that
            # produce it: ``probes: 0`` with remote tracks present means the analysers never
            # attached and the energy path is dead, while probes with ``mapped: 0`` means the
            # levels are being measured and nothing on the page says whose they are.
            logger.warning(
                "meet_bridge.speaker_not_seen",
                seconds=detail.get("seconds"),
                probes=detail.get("probes"),
                mapped=detail.get("mapped"),
                media_elements=detail.get("mediaElements"),
                streams_on_tiles=detail.get("streamsOnTiles"),
                participant_nodes=detail.get("participantNodes"),
                probe_errors=detail.get("probeErrors"),
                # **The three counters and the two shape lists, because without them this warning
                # says only "something is wrong" — which the log already implied.** A run where
                # captions named four speakers and the indicator named none produced no reading at
                # all: the page collected the tiles and media elements it can see, and this handler
                # dropped them on the floor. ``attributed_live`` at 0 with ``attributed`` above it
                # is the specific diagnosis that the indicator selectors match nothing and only the
                # caption panel — always a beat late — is naming anybody.
                edges=detail.get("edges"),
                attributed=detail.get("attributed"),
                attributed_live=detail.get("attributedLive"),
                selectors=detail.get("selectors"),
                # What the DOM actually contains: the shape of each media element and of each
                # participant tile. This is the reading the ``speaking`` selectors are still
                # waiting on, and the reason they are documented as unverified.
                elements=detail.get("elements"),
                tiles=detail.get("tiles"),
                note="no speaker attributed live yet; if somebody did talk, these counters say "
                "which half of the attribution path is missing and the shapes say what the "
                "page has to match on",
            )
            return

        if event == "captionsNothingSeen":
            # **Warning, and the most useful line in this file when attribution is failing.** The
            # caption panel is the one place a name and the words that person said appear together,
            # so if it is empty the avatar cannot answer "what did they ask". ``on: False`` means
            # captions never switched on — a button-selector problem — and ``on: True`` with no
            # blocks means the panel is open and the block selectors do not match what is in it,
            # which ``regionText`` then shows verbatim.
            logger.warning(
                "meet_bridge.captions_not_seen",
                seconds=detail.get("seconds"),
                captions_on=detail.get("on"),
                attempts=detail.get("attempts"),
                blocks=detail.get("blocks"),
                captured=detail.get("captured"),
                attributed=detail.get("attributed"),
                buttons_seen=detail.get("buttonsSeen"),
                # The shape of each matched block, which is what separates "the selector found
                # nothing" from "the selector found the words and the name is elsewhere" — the
                # second is what a live run actually did, eleven times.
                block_shapes=detail.get("blockShapes"),
                alts=detail.get("alts"),
                region_text=detail.get("regionText"),
                note="no caption attributed yet; if anybody spoke, this is what the panel "
                "actually contains",
            )
            return

        if event in ("captionsOn", "captionsArmed", "captionsGaveUp"):
            # Info rather than debug for the same reason ``handsArmed`` is: "is the feature even
            # running" is the first question asked when a transcript is empty.
            logger.info(
                f"meet_bridge.{event}",
                attempts=detail.get("attempts"),
                seconds=detail.get("seconds"),
                buttons_seen=detail.get("buttonsSeen"),
            )
            return

        if event == "speakersArmed":
            logger.info(
                "meet_bridge.speakers_armed",
                sample_ms=detail.get("sampleMs"),
                selectors=detail.get("selectors"),
            )
            return

        if event == "speakerProbeFailed":
            # The energy path is the half that does not depend on Meet's markup, so losing it
            # is worth a warning even though the DOM indicator may still cover the participant.
            logger.warning(
                "meet_bridge.speaker_probe_failed",
                track=detail.get("trackId"),
                error=detail.get("error"),
                note="this participant's level cannot be measured, so their speech will only "
                "be attributed if Meet's own indicator is readable",
            )
            return

        if event == "handsArmed":
            # Info rather than debug: this answers "is the feature even running", which is the
            # first question when a raised hand produces nothing — asked at exactly the moment
            # nobody wants to re-run the meeting at debug level.
            logger.info("meet_bridge.hand_raise_armed", selectors=detail.get("selectors"))
            return

        if event == "handRaise":
            # Fields named explicitly rather than splatted: ``detail`` is page-supplied, and a
            # key called ``event`` would collide with structlog's own parameter and raise
            # inside the read loop — the failure this file already documents once.
            logger.info(
                "meet_bridge.hand_raise_seen",
                participant=detail.get("name"),
                matched_by=detail.get("how"),
                is_self=detail.get("self"),
            )
            return

        if event != "getUserMedia":
            # ``page_event``, not ``event``. structlog's bound-logger methods take the message
            # as a parameter literally named ``event``, so passing ``event=`` as a keyword
            # raises ``TypeError: meth() got multiple values for argument 'event'``.
            #
            # It stayed hidden because a *disabled* level is replaced by a no-op that swallows
            # any keywords — so at INFO this line was free, and at DEBUG it killed the bridge's
            # read loop on the first page event. That is fatal well beyond a lost log line: the
            # read loop is the media channel, so both directions stop, the session sits
            # ``degraded`` with a live browser, and its teardown then raises the same
            # ``TypeError`` — which is how a DELETE fails.
            logger.debug("meet_bridge.page_event", page_event=event, detail=detail)
            return

        wanted_audio = bool(detail.get("audio"))
        wanted_video = bool(detail.get("video"))
        tracks = detail.get("tracks")
        expected = int(wanted_audio) + int(wanted_video)

        if isinstance(tracks, int) and tracks < expected:
            logger.warning(
                "meet_bridge.get_user_media_incomplete",
                audio=wanted_audio,
                video=wanted_video,
                tracks=tracks,
                expected=expected,
                note="Meet asked for media and we handed back fewer tracks than it asked "
                "for; it is publishing a real empty device instead, so the avatar will be "
                "silent and/or blank. Check that the playout worklet started.",
            )
            return

        logger.info(
            "meet_bridge.get_user_media",
            audio=wanted_audio,
            video=wanted_video,
            tracks=tracks,
            note="Meet is publishing the avatar's synthetic tracks",
        )

    def _on_audio(self, message: MeetMessage) -> None:
        try:
            frame = to_audio_frame(message, ctx=self._ctx, clock=self._clock)
        except BridgeProtocolError as exc:
            # One malformed frame is not worth tearing a meeting down for; a persistent
            # fault shows up as silence and as this counter climbing.
            self._malformed_audio += 1
            logger.warning("meet_bridge.audio_dropped", error=str(exc))
            return
        self._inbound.put(frame, ctx=frame.ctx, reason="ingest_overflow")

    def _on_participants(self, message: MeetMessage) -> None:
        roster = parse_roster(message.json())
        if roster == self._roster:
            return

        # **Announced once, at info, because "which entry is the avatar" decides three features
        # and was previously unobservable.** A live log showed ``others=3 participants=3`` — no
        # entry marked as ours — while attendance reported the avatar's own account as a second
        # attendee and speaker attribution then credited it with somebody else's speech. Nothing in
        # the log said which name the connector thought was its own, so the fault looked like three
        # unrelated ones. Logged on change rather than per roster: it is fixed for a session.
        # **On the first roster that has anybody in it, and whenever the answer changes.**
        # Gating on ``self_name`` alone was a bug in this line: the first roster of a session
        # arrives *empty* — the page reports before Meet has drawn a tile — so the name was logged
        # against ``entries=[]`` and never again, because the fallback name never changed. The one
        # reading that settles which entry is the avatar was therefore missing from exactly the
        # logs it was added for.
        first_populated = roster.count > 0 and self._roster.count == 0
        if first_populated or roster.self_name != self._roster.self_name:
            logger.info(
                "meet_bridge.self_name",
                self_name=roster.self_name,
                others=[p.display_name for p in roster.others],
                # Every entry with the flag the page put on it, which is the reading that settles
                # "is one of these the avatar". ``self_name`` alone could not: it says what the
                # connector *believes*, and a live log showed an account name among the others with
                # nothing to say whether the page had marked it or simply failed to.
                entries=[
                    f"{p.display_name}{' [self]' if p.is_self else ''}"
                    for p in roster.participants
                ],
                note="the name the avatar's own account appears under; every entry other than "
                "this one is counted as a participant",
            )

        self._roster = roster
        logger.debug("meet_bridge.roster", participants=roster.count, others=len(roster.others))
        for listener in self._roster_listeners:
            listener(roster)

    def _on_meet_state(self, message: MeetMessage) -> None:
        body = message.json()
        raw = str(body.get("state") or "")
        try:
            state = MeetState(raw)
        except ValueError:
            logger.warning("meet_bridge.unknown_meet_state", state=raw)
            return

        if state is self._meet_state:
            return
        self._meet_state = state
        logger.info("meet_bridge.meet_state", state=state)
        for listener in self._state_listeners:
            listener(state)

        if state in (MeetState.DENIED, MeetState.EJECTED):
            # Terminal, and the reason is not ours to retry: a host decided. Failing the
            # leg lets the session end cleanly instead of relaunching a browser into a
            # meeting that has explicitly refused the avatar.
            self._fail(f"the meeting refused the avatar: {state}")
            self._inbound.close()
        elif state is MeetState.ENDED:
            self._fail("the meeting has ended")
            self._inbound.close()
        elif state is MeetState.LEFT:
            # Recoverable, so degrade and let the supervisor's grace window decide rather
            # than unilaterally failing a session an operator may be about to stop anyway.
            self._degrade("the browser left the meeting")

    def _on_error(self, message: MeetMessage) -> None:
        body = message.json()
        code = str(body.get("code") or "UNKNOWN")
        detail = str(body.get("message") or "")
        if bool(body.get("fatal")):
            self._fail(f"[{code}] {detail}")
            self._inbound.close()
            return
        logger.warning("meet_bridge.page_error", code=code, message=detail)
        self._degrade(f"[{code}] {detail}")

    # -- state helpers -----------------------------------------------------

    def _degrade(self, detail: str) -> None:
        """Mark impaired-but-serving, without clobbering a hard failure."""
        if self._state is ComponentState.UNHEALTHY:
            return
        self._state = ComponentState.DEGRADED
        self._detail = detail

    def _fail(self, detail: str) -> None:
        self._state = ComponentState.UNHEALTHY
        self._detail = detail
        logger.error("meet_bridge.failed", detail=detail)
