"""GoogleMeetSession — one avatar in one Google Meet conference.

**This file is where the reuse claim is either true or false, so it is worth reading as
evidence.** Everything between the two platform legs is the *same shared code* Zoom shipped
and Teams reused without modification: ``AvatarClient``, ``WebSocketAvatarTransport``,
``MediaRouter``, ``DecodePipeline``, ``FfmpegDecoder``, ``Pacer``, ``EchoGuard``,
``IdleFrameSource``, ``MediaClock``, ``ReconnectPolicy``, ``BoundedFrameQueue``. Not one line
of any of them changed to accommodate a browser.

What this connector adds is a platform adapter — a browser instead of an SDK — and nothing
else. Structurally identical in role to ``ZoomSessionFactory`` and ``TeamsSessionFactory``,
which is what lets all three satisfy ``ConnectorSessionFactory`` and be registered side by
side.

**The two places Meet genuinely differs, and how each is expressed as data rather than as a
branch:**

* ``EchoGuard(per_participant_audio=False)``. The capture graph mixes every remote track
  before sampling, so inbound frames carry no attribution and the guard runs its speaking
  gate in strict mode. That is the guard's documented fallback, reached by configuration.
  It is also the *only* echo defence needed here, because the WebRTC tap is inbound-only —
  the avatar's own audio cannot enter it. The gate covers the acoustic path on a host with
  speakers, not a software loop. See ``egress/media_sink.own_participant``.
* ``leg_states()`` returns one state twice. One browser tab is the participant, so there is
  no state in which Meet ingest works while Meet egress does not. Teams reports the same
  shape for the same reason; Zoom's two legs genuinely differ.

**One leg, three components in the health report.** ``leg_states`` collapses to a pair
because ``derive_state`` takes a pair, but ``health()`` reports the bridge, the publisher and
the watchdog separately — because an operator debugging a silent avatar needs to know
*which* of those is unhappy, and the pair cannot carry that.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path

from src.avatar.client import AvatarClient
from src.avatar.ws_transport import WebSocketAvatarTransport
from src.connectors.google_meet.audio_capture.audio_source import MeetAudioSource
from src.connectors.google_meet.automation.selectors import MeetSelectors
from src.connectors.google_meet.bridge.chromium_bridge import ChromiumBridge, DriverFactory
from src.connectors.google_meet.browser.profile import ProfileManager
from src.connectors.google_meet.config import GoogleMeetConnectorConfig
from src.connectors.google_meet.egress.media_sink import ChromiumMediaSink
from src.connectors.google_meet.meeting.attendance import AttendanceLedger
from src.connectors.google_meet.meeting.attendance_announcer import AttendanceAnnouncer
from src.connectors.google_meet.meeting.chat import MeetChatSource
from src.connectors.google_meet.meeting.hand_raise import MeetHandRaiseSource, render_prompt
from src.connectors.google_meet.monitoring.watchdog import MediaWatchdog
from src.connectors.google_meet.virtual_camera.adapter import VirtualCameraAdapter
from src.connectors.google_meet.virtual_microphone.adapter import VirtualMicrophoneAdapter
from src.domain.context import FrameContext
from src.domain.health import ComponentState, HealthReport
from src.domain.media import AudioFormat, VideoFormat
from src.domain.session import SessionContext, SessionState
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


class GoogleMeetSession:
    """One avatar participating in one Google Meet conference."""

    __slots__ = (
        "_announcer",
        "_attendance",
        "_bridge",
        "_chat",
        "_clock",
        "_hands",
        "_publisher",
        "_router",
        "_session",
        "_source",
        "_task",
        "_watchdog",
    )

    def __init__(
        self,
        *,
        session: SessionContext,
        clock: MediaClock,
        bridge: ChromiumBridge,
        source: AudioSource,
        publisher: MediaSink,
        router: MediaRouter,
        watchdog: MediaWatchdog,
        chat: MeetChatSource | None = None,
        hands: MeetHandRaiseSource | None = None,
        attendance: AttendanceLedger | None = None,
        announcer: AttendanceAnnouncer | None = None,
    ) -> None:
        self._session = session
        self._clock = clock
        self._bridge = bridge
        self._source = source
        self._publisher = publisher
        self._router = router
        self._watchdog = watchdog
        self._chat = chat
        self._hands = hands
        self._attendance = attendance
        self._announcer = announcer
        self._task: asyncio.Task[None] | None = None

    @property
    def session(self) -> SessionContext:
        return self._session

    @property
    def router(self) -> MediaRouter:
        return self._router

    @property
    def bridge(self) -> ChromiumBridge:
        return self._bridge

    @property
    def attendance(self) -> AttendanceLedger | None:
        """Who has been in this meeting, or ``None`` when the ledger is disabled.

        Read by ``MeetingService.attendance_snapshot`` through a structural check, which is how
        the API serves this without ``MeetingService`` learning that Google Meet exists — the
        same duck-typing ``health_report`` already relies on. ``None`` rather than an empty
        ledger when disabled, so "switched off" and "nobody here yet" stay distinguishable.
        """
        return self._attendance

    async def start(self) -> None:
        """Join the meeting, then start routing.

        One browser covers both directions, so — as with Teams and unlike Zoom — there is no
        "publish first, ingest may still be waiting" ordering to get right, and no join race
        to resolve: we navigate to the meeting rather than waiting for the platform to notify
        us.

        The bridge's first join runs **inline**, so a missing Chromium, an unsigned-in
        profile, a bad meeting code, or a host who denies entry fails session creation with
        the real reason instead of degrading a live session. The watchdog starts last,
        because it has nothing to assess until media is flowing.
        """
        await self._bridge.start(self._session.meeting)
        await self._source.start()
        if self._chat is not None:
            await self._chat.start()
        if self._hands is not None:
            await self._hands.start()
        self._task = asyncio.create_task(self._router.run(), name="media-router")
        # After the router, because it has nothing to send until the avatar client has
        # completed its handshake — the announcer's first push happens one settle interval
        # later, by which time the negotiated version is known and the roster has stopped
        # churning.
        if self._announcer is not None:
            await self._announcer.start()
        await self._watchdog.start()

    async def stop(self) -> None:
        """Tear down in a fixed order. Idempotent.

        The watchdog first, so it cannot report a fault caused by the teardown it is
        watching. Then the router, so nothing is mid-publish. Then the bridge, which leaves
        the meeting and closes the browser for both legs at once. Then the router's queues.
        """
        await self._watchdog.stop()
        # Before the router, so it cannot try to send on a transport that is being closed.
        if self._announcer is not None:
            await self._announcer.stop()

        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

        if self._chat is not None:
            await self._chat.stop()
        if self._hands is not None:
            await self._hands.stop()

        await self._bridge.stop()
        self._router.close()

    def health(self) -> HealthReport:
        """Component-level health.

        The router's components (ingest, avatar, decoder) plus the publisher and the
        watchdog. The watchdog is reported separately from the bridge on purpose: they can
        disagree, and when they do — bridge healthy, watchdog degraded — that combination *is*
        the diagnosis, because it means the browser is fine and the audio is not.
        """
        return HealthReport(
            components=(
                *self._router.health().components,
                self._publisher.health(),
                self._watchdog.health(),
            )
        )

    def leg_states(self) -> tuple[ComponentState, ComponentState]:
        """``(ingest, publish)`` health.

        Both derive from the one browser, so the pair moves together. That is the platform
        being reported accurately rather than an abstraction leaking: one tab is the
        participant, and if it is gone the avatar is not in the meeting in either direction.

        The watchdog's verdict is folded in, downgrading a healthy pair to degraded — because
        a browser that is alive but no longer hearing anything is precisely the failure the
        pair alone cannot express. It can only downgrade, never upgrade: an inference must
        not be able to declare a broken bridge healthy.
        """
        state = self._bridge.health().state

        # Two inferences are folded in, and both may only *downgrade*: a derived signal must
        # never be able to declare a broken browser healthy.
        #
        # Neither applies while the session is still ``JOINING``, and that is a constraint of
        # the shared state machine rather than a preference. ``domain.session`` permits
        # ``JOINING -> ACTIVE`` but not ``JOINING -> DEGRADED``, so reporting a degraded pair
        # before the session has ever been active raises ``IllegalStateTransitionError`` inside
        # the supervisor's poll loop. Waiting costs one poll interval: the session reaches
        # ACTIVE, then degrades on the next tick, and ``ACTIVE -> DEGRADED`` is legal.
        joining = self._session.state is SessionState.JOINING
        stalled = self._watchdog.verdict.state is ComponentState.DEGRADED
        impaired = stalled or self._avatar_has_failed()
        if state is ComponentState.HEALTHY and not joining and impaired:
            state = ComponentState.DEGRADED
        return state, state

    def _avatar_has_failed(self) -> bool:
        """Whether the avatar agent is known to be unreachable.

        **Folded into the leg states because otherwise a dead avatar reads as a healthy
        session.** ``leg_states`` used to report only the browser, so a session whose avatar
        never completed its handshake still derived ``ACTIVE`` — the browser was in the meeting,
        the pacer was publishing, and every layer agreed, while the avatar heard nothing and the
        meeting showed grey idle frames. Observed exactly that way in a live run: the log carried
        ``router.avatar_unreachable`` and then ``session.transition to_state=active`` four lines
        later.

        ``DEGRADED`` rather than ``UNHEALTHY`` on purpose. ``ComponentState.DEGRADED.is_serving``
        is True, so the supervisor's grace window will not fail the session — which is right,
        because an avatar blip is recoverable and the browser is genuinely still in the meeting.
        It changes what the operator is told, not whether the session survives.

        Only the Google Meet connector does this. Zoom's and Teams' ``leg_states`` have the same
        gap, and closing it there means editing two deployed connectors — recorded in
        ``docs/design/007`` §7 rather than done here.
        """
        avatar = self._router.health().component("avatar_client")
        if avatar is None:
            return False
        # Only an explicit UNHEALTHY counts. ``UNKNOWN`` is the transport's "not started yet",
        # which every session passes through in the moment between ``start()`` creating the
        # router task and that task completing its handshake — treating it as a fault would
        # report a degraded pair for the first tick of every healthy session. ``DEGRADED`` is
        # impaired-but-serving, and the avatar client is the component entitled to decide that
        # about itself; downgrading again on top of it would say nothing new.
        return avatar.state is ComponentState.UNHEALTHY


class GoogleMeetSessionFactory:
    """Builds a fully wired ``GoogleMeetSession``.

    All concrete-type knowledge for the Meet feature lives here, so ``MeetingService``
    composes sessions without naming Chromium, Playwright, a canvas, or an AudioWorklet.
    """

    __slots__ = (
        "_config",
        "_driver_factory",
        "_metrics",
        "_profiles",
        "_selectors",
        "_sink_override",
        "_source_override",
    )

    def __init__(
        self,
        *,
        config: GoogleMeetConnectorConfig,
        metrics: MetricsCollector | None = None,
        sink_override: MediaSink | None = None,
        source_override: AudioSource | None = None,
        driver_factory: DriverFactory | None = None,
        selectors: MeetSelectors | None = None,
        profiles: ProfileManager | None = None,
    ) -> None:
        self._config = config
        self._metrics = metrics
        # Overrides mirror ``ZoomSessionFactory``'s and ``TeamsSessionFactory``'s: they let
        # the whole pipeline run into a ``FileSink`` for verification, and let tests
        # substitute fakes, without a second code path. ``driver_factory`` additionally
        # allows an in-process fake page — which is how this connector is testable with no
        # Chromium, no Google account, and no meeting.
        self._sink_override = sink_override
        self._source_override = source_override
        self._driver_factory = driver_factory
        self._selectors = selectors
        self._profiles = profiles

    def build(self, session: SessionContext) -> GoogleMeetSession:
        config = self._config
        ctx = session.frame_context()
        clock = MediaClock()

        video_format = config.video_format
        publish_audio_format = config.publish_audio_format

        bridge = ChromiumBridge(
            config=config,
            ctx=ctx,
            clock=clock,
            driver_factory=self._driver_factory,
            selectors=self._selectors,
            profiles=self._profiles,
        )

        source = self._source_override or MeetAudioSource(bridge=bridge, metrics=self._metrics)
        publisher = self._sink_override or ChromiumMediaSink(
            bridge=bridge,
            camera=VirtualCameraAdapter(
                bridge=bridge,
                video_format=video_format,
                clock=clock,
                metrics=self._metrics,
            ),
            microphone=VirtualMicrophoneAdapter(
                bridge=bridge,
                audio_format=publish_audio_format,
                clock=clock,
                metrics=self._metrics,
            ),
        )

        echo_guard = EchoGuard(
            # False, and structurally so: the capture graph mixes every remote track before
            # sampling, so no inbound frame carries attribution. Capability as data, not a
            # branch.
            per_participant_audio=False,
            hangover_ms=config.echo_gate_hangover_ms,
            # ...and the speaking gate is *open* on this connector, which is what makes the
            # avatar interruptible. The gate cannot tell the avatar's echo from a person
            # talking over it, so a shut gate suppresses the interruption too — and here there
            # is no echo for it to catch: the WebRTC tap is inbound-only, so the avatar's own
            # audio never enters it (``egress/media_sink.own_participant``).
            gate_enabled=False,
            metrics=self._metrics,
        )
        # Deliberately no ``set_own_participant`` and no listener, where Zoom sets it from the
        # publisher and Teams subscribes to the roster. There is no identity to set: the
        # avatar's audio never enters the tap, so the identity filter would have nothing to
        # match and arming it would only invite a false suppression.

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
                video_format=video_format,
                audio_format=publish_audio_format,
                metrics=self._metrics,
            ),
            ctx=ctx,
            metrics=self._metrics,
        )

        idle = self._build_idle(ctx, video_format, publish_audio_format)

        pacer = Pacer(
            ctx=ctx,
            clock=clock,
            sink=publisher,
            idle=idle,
            video_format=video_format,
            audio_format=publish_audio_format,
            echo_guard=echo_guard,
            video_queue_size=config.video_queue_size,
            audio_queue_size=config.audio_queue_size,
            metrics=self._metrics,
        )

        # Chat is the one capability here that is *configuration*, not platform shape: Meet can
        # always do it, and an operator may not want the avatar opening the chat panel. Built
        # only when enabled, so a disabled session carries no chat surface at all rather than an
        # inert one.
        chat: MeetChatSource | None = None
        if config.chat_enabled:
            chat_source = MeetChatSource(
                clock=clock,
                require_mention=config.chat_require_mention,
                mention_names=(config.display_name, *config.chat_mention_names),
            )
            bridge.attach_chat(chat_source)
            # The name participants actually see — and therefore the one they type when they
            # want an answer — belongs to the signed-in Google account, not to
            # ``display_name``, which Meet only asks for when the profile has lost its session.
            # The roster is the only place that name appears, so the source learns it from
            # there and treats the configured name as the fallback until it does.
            bridge.add_roster_listener(
                lambda roster: chat_source.observe_self_name(roster.self_name)
            )
            chat = chat_source

        # Raised hands, on the same terms and for the same reason: built only when enabled, so
        # a session that should hold the floor carries no interrupt surface at all. Separate
        # from ``chat_enabled`` because the two cost different things — chat opens a panel
        # other participants can see the avatar using, this reads an indicator already on
        # screen — and an operator may well want one without the other.
        hands: MeetHandRaiseSource | None = None
        if config.hand_raise_enabled:
            hand_source = MeetHandRaiseSource(
                clock=clock,
                prompt=config.hand_raise_prompt,
                cooldown_s=config.hand_raise_cooldown_s,
                self_names=(config.display_name,),
            )
            bridge.attach_hand_raise(hand_source)
            # The account's rendered name again, for the same reason chat learns it: it is the
            # only way to recognise our own row, and ``display_name`` is not it on a signed-in
            # profile.
            bridge.add_roster_listener(
                lambda roster: hand_source.observe_self_name(roster.self_name)
            )
            hands = hand_source

        # Attendance, on the same "built only when enabled" terms — but note what it is *not*
        # given: no queue, no task, no place on the media path, and no component in the health
        # report. It is a listener on the roster stream that already exists, which is the whole
        # reason it can be on by default where chat and hands are judgement calls. The page does
        # no extra work for it, so it cannot cost media latency (``meeting/attendance.py``).
        attendance: AttendanceLedger | None = None
        announcer: AttendanceAnnouncer | None = None
        if config.attendance_enabled:
            # Seeded with every name that could mean "us", because the avatar counting itself as
            # an attendee makes every answer wrong by one — observed in a live meeting, where the
            # bot reported two others in a call Meet itself described as having "one other
            # person". ``display_name`` alone is not enough: Meet only asks for it when the
            # profile has lost its session, and a signed-in profile renders the *account's* name
            # instead. That name is not configured anywhere, but the account address is, and the
            # local part is what Google derives the rendered name from.
            attendance = AttendanceLedger(self_names=_self_name_candidates(config))
            bridge.add_roster_listener(attendance.observe_roster)
            # Serving the ledger over HTTP makes it available; this is what makes the agent
            # actually hold it. Both paths exist because they answer different questions — the
            # push keeps a conversational agent able to answer "who is here?" with no round
            # trip, and the endpoint is what an operator or a tool-calling agent reads.
            if config.attendance_push_enabled:
                announcer = AttendanceAnnouncer(
                    ledger=attendance,
                    avatar=avatar,
                    interval_s=config.attendance_push_interval_s,
                    require_negotiation=config.attendance_push_require_negotiation,
                )

        # Speech as a trigger for the hand-raise handover — built only when enabled, on the
        # same terms as chat and hands, so a session that should hold the floor carries no
        # detector at all rather than an inert one. The wording is the hand's, rendered here
        # because the inbound mix carries no name: a voice and a hand are the same request, so
        # the avatar should answer both with "ok, go ahead".
        speech: SpeechDetector | None = None
        voice_prompt = ""
        if config.speech_interrupt_enabled:
            speech = SpeechDetector(rms_threshold=config.speech_interrupt_threshold)
            voice_prompt = render_prompt(config.hand_raise_prompt, None)

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
            hands=hands,
            hand_raise_mute_ms=config.hand_raise_mute_ms,
            speech=speech,
            voice_prompt=voice_prompt,
        )

        watchdog = MediaWatchdog(
            bridge=bridge,
            source=source,
            interval_s=config.watchdog_interval_s,
        )

        return GoogleMeetSession(
            session=session,
            clock=clock,
            bridge=bridge,
            source=source,
            publisher=publisher,
            router=router,
            watchdog=watchdog,
            chat=chat,
            hands=hands,
            attendance=attendance,
            announcer=announcer,
        )

    # -- component builders ------------------------------------------------

    def _build_idle(
        self, ctx: FrameContext, video_format: VideoFormat, audio_format: AudioFormat
    ) -> IdleFrameSource:
        """Idle media, so the avatar reads as a person between utterances.

        Meet needs this for the same reason Zoom and Teams do, and arguably more visibly: the
        synthetic camera track is driven frame by frame from the pacer, so if frames stop the
        canvas stops changing and Meet publishes a still image. A frozen tile reads as a
        broken connection rather than as someone listening.
        """
        clip_path = self._config.idle_clip_path
        if clip_path is not None and Path(clip_path).exists():
            return IdleFrameSource.from_raw_clip(
                clip_path, ctx=ctx, video_format=video_format, audio_format=audio_format
            )
        return IdleFrameSource(ctx=ctx, video_format=video_format, audio_format=audio_format)


def _self_name_candidates(config: GoogleMeetConnectorConfig) -> tuple[str, ...]:
    """Every name that might be the avatar's own, so it is never counted as an attendee.

    Two sources, both configuration rather than observation, because this has to be right on the
    *first* roster — by the time Meet's own "(you)" marker is read, a wrong entry has already
    been recorded as a person who joined.

    * ``display_name`` — what we asked to be called. Correct only when the profile has lost its
      Google session and Meet prompted for a name.
    * the local part of ``google_email`` — Google derives an unnamed account's rendered name
      from it, which is why a bot signed in as ``jadumeetboot@gmail.com`` appears in the roster
      as "jadumeetboot" and matches nothing configured. Dots and plus-addressing are dropped so
      ``first.last+meet@`` also yields "first last".
    """
    candidates: list[str] = []
    if config.display_name:
        candidates.append(config.display_name)

    local = (config.google_email or "").split("@", 1)[0].split("+", 1)[0].strip()
    if local:
        candidates.append(local)
        spaced = local.replace(".", " ").replace("_", " ").replace("-", " ")
        if spaced != local:
            candidates.append(" ".join(spaced.split()))

    seen: set[str] = set()
    unique: list[str] = []
    for name in candidates:
        folded = name.casefold()
        if folded and folded not in seen:
            seen.add(folded)
            unique.append(name)
    return tuple(unique)
