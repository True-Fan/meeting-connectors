"""Configuration.

Config-driven with no globals: ``Settings`` is constructed once and supplied through
the DI container. Nothing reads ``os.environ`` directly.

Secrets are ``SecretStr`` so they cannot be leaked by an accidental ``repr()`` in a
log line — with structured logging that would otherwise be an easy mistake to make.

Environment variables use the ``MC_`` prefix and ``__`` as the nesting delimiter,
e.g. ``MC_ZOOM__CLIENT_ID``.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.domain.media import AudioFormat, SampleFormat, VideoFormat


class Environment(StrEnum):
    LOCAL = "local"
    DEV = "dev"
    STAGING = "staging"
    PRODUCTION = "production"

    @property
    def is_production_like(self) -> bool:
        return self in (Environment.STAGING, Environment.PRODUCTION)


class ObservabilitySettings(BaseModel):
    """Logging and metrics configuration."""

    log_level: str = "INFO"
    json_logs: bool = False
    histogram_capacity: int = Field(default=4096, ge=64, le=1_048_576)
    """Ring-buffer size per latency histogram. Bounds memory and fixes the
    percentile window; see ``infrastructure.metrics``."""

    @field_validator("log_level")
    @classmethod
    def _upper(cls, value: str) -> str:
        level = value.upper()
        allowed = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
        if level not in allowed:
            raise ValueError(f"log_level must be one of {sorted(allowed)}, got {value!r}")
        return level


class ZoomSettings(BaseModel):
    """Zoom credentials and RTMS subscription parameters.

    Two independent credential paths (doc 003 §1.1):

    * ``client_id`` / ``client_secret`` / ``webhook_secret_token`` — RTMS webhook
      verification and handshake signature.
    * ``sdk_key`` / ``sdk_secret`` — Meeting SDK JWT for the publishing bot.
    """

    client_id: str = ""
    client_secret: SecretStr = SecretStr("")
    webhook_secret_token: SecretStr = SecretStr("")

    sdk_key: str = ""
    sdk_secret: SecretStr = SecretStr("")

    account_id: str = ""
    s2s_client_id: str = ""
    s2s_client_secret: SecretStr = SecretStr("")
    """Server-to-Server OAuth credentials — a *separate* app from the General App
    above. Needs the ``meeting:update:participant_rtms_app_status`` scope."""

    rtms_auto_start: bool = True
    """Ask Zoom to start RTMS ourselves when a session is created.

    Zoom only emits ``meeting.rtms_started`` if RTMS was explicitly triggered, and
    tears the stream down again if nobody attaches within about a minute. Triggering
    it by hand means racing that window; triggering it here means the session is
    already registered and waiting when the webhook lands. Inert unless the S2S
    credentials above are set, so it cannot fire from an unconfigured deployment."""

    api_base_url: str = "https://api.zoom.us"
    oauth_base_url: str = "https://zoom.us"
    api_timeout_s: float = Field(default=10.0, gt=0.0, le=60.0)

    rtms_send_rate_ms: int = Field(default=20, ge=20, le=1000, multiple_of=20)
    """RTMS audio delivery interval. 20 ms is the protocol floor; the samples
    default to 100, which donates 80 ms of latency at the first hop (doc 003 §3.2)."""

    rtms_per_participant_audio: bool = True
    """Subscribe ``AUDIO_MULTI_STREAMS`` so the avatar's own audio can be filtered
    by participant. Disabling it forces ``EchoGuard`` into strict gating."""

    display_name: str = "AI Avatar"
    """The name other participants see. The avatar should read as a person."""

    def is_configured(self) -> bool:
        """True when RTMS ingest credentials are present."""
        return bool(
            self.client_id
            and self.client_secret.get_secret_value()
            and self.webhook_secret_token.get_secret_value()
        )

    def is_publish_configured(self) -> bool:
        """True when Meeting SDK publish credentials are present."""
        return bool(self.sdk_key and self.sdk_secret.get_secret_value())

    def is_rtms_auto_start_configured(self) -> bool:
        """True when we can ask Zoom to start RTMS ourselves.

        Requires the General App ``client_id`` too: it names the app RTMS starts
        for, and Zoom rejects the call without it.
        """
        return bool(
            self.rtms_auto_start
            and self.account_id
            and self.s2s_client_id
            and self.s2s_client_secret.get_secret_value()
            and self.client_id
        )


class TeamsSettings(BaseModel):
    """Microsoft Teams credentials and app-hosted media sidecar settings.

    Teams' real-time media is only reachable from a **Windows** host running the
    .NET ``Microsoft.Graph.Communications.Calls.Media`` SDK, so unlike Zoom the
    sidecar is a separate machine rather than a sibling process on a shared volume
    (doc 005 §2). Everything here is therefore either an Azure AD credential the
    Python bridge holds, or the network coordinates of that Windows host.

    The bridge itself never calls Graph: the app-hosted media blob can only be
    produced by the media platform inside the sidecar, so the sidecar owns the join
    (doc 005 §3.1). The credentials below are what the *sidecar* is provisioned with;
    they live here so a deployment configures one place, and are forwarded over the
    IPC join message rather than being baked into the Windows image.
    """

    tenant_id: str = ""
    client_id: str = ""
    client_secret: SecretStr = SecretStr("")
    """Azure AD app registration with the ``Calls.JoinGroupCall.All`` and
    ``Calls.AccessMedia.All`` *application* permissions, admin-consented."""

    sidecar_host: str = ""
    sidecar_port: int = Field(default=8445, ge=1, le=65535)
    sidecar_connect_timeout_s: float = Field(default=20.0, gt=0)
    sidecar_ready_timeout_s: float = Field(default=60.0, gt=0)
    """Longer than Zoom's: a Graph call has to be created, signalled, and negotiated
    before media flows, where the Zoom sidecar only has to attach a local SDK."""
    sidecar_reconnect_max_attempts: int = Field(default=10, ge=1)

    sidecar_tls_enabled: bool = True
    """The link crosses a host boundary carrying meeting audio and a bearer token,
    so TLS is on by default and turning it off is an explicit local-dev act."""
    sidecar_ca_file: Path | None = None
    sidecar_client_cert_file: Path | None = None
    sidecar_client_key_file: Path | None = None
    """Client certificate for mutual TLS. When unset the link is server-authenticated
    only — acceptable on a private subnet, not on a shared network."""

    unmixed_audio: bool = True
    """Request per-participant (unmixed) audio from the media platform. Teams gives
    up to four dominant speakers with a source id, which is what lets ``EchoGuard``
    filter by identity instead of falling back to the gate alone."""

    publish_sample_rate_hz: int = Field(default=16_000, ge=8_000)
    """PCM rate handed to the Teams media platform. Its own value rather than the
    shared ``media.publish_sample_rate_hz`` because the two SDKs want different
    rates, and Zoom's is already set for Zoom."""

    video_width: int = Field(default=1280, ge=2)
    video_height: int = Field(default=720, ge=2)
    video_fps: int = Field(default=30, ge=1, le=30)
    """Teams' send formats are an enumerated set, not a free choice — see
    ``connectors/teams/config.py``, which validates the triple against it."""

    display_name: str = "AI Avatar"

    def is_configured(self) -> bool:
        """True when the connector has everything it needs to join a meeting."""
        return bool(
            self.tenant_id
            and self.client_id
            and self.client_secret.get_secret_value()
            and self.sidecar_host
        )


class GoogleMeetSettings(BaseModel):
    """Google Meet connector settings — a signed-in Chromium, driven by Playwright.

    **Why the shape is so different from Zoom's and Teams'.** Those two hold API
    credentials, because both platforms ship a server-side SDK that can publish media into
    a conference. Google does not: its only real-time media API is receive-only and states
    so explicitly, and there is no Meet equivalent of a Meeting SDK. The full evidence is
    in ``connectors/google_meet/capabilities.py``.

    So the avatar has to be a *client* — a real browser, signed into a real Google account,
    joining like a person. That makes the credential a **browser profile on disk** rather
    than a client id and secret, and it makes the rest of these settings browser lifecycle
    rather than API parameters.

    Nothing here is required for a Zoom-only or Teams-only deployment. ``profile_dir``
    defaults to unset, ``is_configured()`` is then False, and ``build_connector_registry``
    does not register the connector at all.
    """

    profile_dir: Path | None = None
    """Persistent Chromium profile holding the avatar's Google session.

    The one required setting, and the only durable state this connector has. Authenticate
    it **once**, interactively — Google's sign-in can present a second factor or a
    device-verification challenge that no script should be attempting on every session, and
    repeatedly trying is what gets an automated account restricted. Treated as a template:
    each session runs on a throwaway copy, so sessions cannot corrupt each other's login
    (see ``connectors/google_meet/browser/profile.py``)."""

    chromium_executable: Path | None = None
    """Overrides Playwright's bundled Chromium. Normally unset."""

    headless: bool = True
    """Set False to perform the one-off interactive Google sign-in described above."""

    browser_launch_timeout_s: float = Field(default=60.0, gt=0)
    extra_browser_args: list[str] = Field(default_factory=list)
    """Appended after the built-in flags, and Chromium takes the last occurrence of a
    repeated switch — so this can override any of them. See
    ``connectors/google_meet/browser/launcher.py`` for what the defaults are and why."""

    google_email: str = ""
    google_password: SecretStr = SecretStr("")
    """Optional, and best-effort. Only bootstraps an empty profile on a deployment that
    cannot run an interactive session, and only works on an account with no second factor —
    which is not a configuration to recommend for an account that sits in customer
    meetings."""

    display_name: str = "AI Avatar"
    """Used only if Meet asks for a name, which means the profile lost its Google session.
    A signed-in profile joins under the account's own name."""

    join_timeout_s: float = Field(default=120.0, gt=0)
    lobby_timeout_s: float = Field(default=300.0, gt=0)
    """Separate from ``join_timeout_s``, and much longer, because "Asking to join" is not a
    failure: a human host has to notice a notification and click Admit. Charging that wait
    against the join budget would abandon meetings that were about to let the avatar in."""

    bridge_host: str = "127.0.0.1"
    bridge_port: int = Field(default=0, ge=0, le=65535)
    """The loopback WebSocket the page connects back on. ``0`` means "let the OS choose",
    which is the default and the right one: one server is bound per session, so a fixed
    port would make two concurrent sessions collide."""
    bridge_ready_timeout_s: float = Field(default=60.0, gt=0)
    bridge_send_queue_bytes: int = Field(default=4 * 1024 * 1024, ge=64 * 1024)

    video_width: int = Field(default=1280, ge=2)
    video_height: int = Field(default=720, ge=2)
    video_fps: int = Field(default=25, ge=1, le=30)
    """Geometry for the synthetic camera. Unlike Teams there is no enumerated format list
    to match — a canvas-backed track takes any even geometry — but every frame crosses the
    page bridge as raw I420, so ``connectors/google_meet/config.py`` caps it at 1080p."""

    publish_sample_rate_hz: int = Field(default=48_000, ge=8_000)
    """PCM rate for the synthetic microphone. 48 kHz because that is Web Audio's native
    rate on desktop Chromium, so the track needs no resampling stage at all. Its own value
    rather than the shared ``media.publish_sample_rate_hz`` because each platform's runtime
    wants a different rate, and Zoom's is already set for Zoom."""

    rejoin_max_attempts: int = Field(default=5, ge=1)
    """Lower than the other connectors' 10 on purpose: a rejoin here relaunches a whole
    browser and may sit in a lobby, so ten attempts would take many minutes during which
    the avatar is visibly absent."""

    inject_stages: list[str] = Field(default_factory=list)
    """Restrict which ``js/bridge.js`` bootstrap stages are installed. Empty means all.

    Stages: ``devices`` (the ``getUserMedia`` patch), ``rtc`` (the peer-connection tap),
    ``observers`` (roster and meeting state), ``heartbeat``, ``socket`` (the loopback channel),
    then ``capture`` / ``playout`` / ``canvas`` once the channel is up.

    A diagnostic, and it earned its place: a renderer SIGSEGV on real Meet was isolated to one
    stage by walking this list — ``devices``, ``rtc`` and ``observers`` each cleared in turn,
    and the crash landing on ``socket``. Every stage reports begin/ok/threw, so the last one to
    complete is always visible. See ``docs/design/007`` §8."""

    disable_injection: bool = False
    """Launch and join without injecting ``js/bridge.js`` at all. **A session with this set
    cannot carry media**, and fails immediately rather than degrading silently.

    The injected script *is* the connector: it patches ``getUserMedia`` for the synthetic camera
    and microphone (all egress), taps ``RTCPeerConnection`` for conference audio (all ingest),
    observes the roster, and opens the channel that carries every frame.

    Failing fast is deliberate. Without it, ``wait_for_page`` would block for
    ``bridge_ready_timeout_s`` waiting for a page that has no script to connect with, and the
    resulting ``BridgeUnavailableError`` is *recoverable* — so the bridge would relaunch the
    browser and repeat until the rejoin budget was spent. Five launches to reach a conclusion
    available at once. Prefer ``inject_stages`` for bisecting; this is the all-or-nothing
    switch."""

    watchdog_interval_s: float = Field(default=5.0, gt=0)
    """How often to check that conference audio is still arriving. See
    ``connectors/google_meet/monitoring/watchdog.py`` for the failure this catches — a
    browser that is alive and connected while the audio has quietly stopped, which every
    other health check reports as healthy."""

    chat_enabled: bool = True
    """Forward the meeting's chat to the avatar agent as text, so a typed question gets a
    spoken answer.

    Reading chat requires **opening the chat panel** — Meet renders message history nowhere
    else, and with the panel closed a message is a transient popup that leaves nothing in the
    DOM. That is a visible UI action taken by the avatar's own account, which is the reason
    this is a switch rather than unconditional behaviour.

    Only messages arriving *after* the avatar joins are forwarded. The backlog rendered when
    the panel opens is recorded and skipped, because answering it would mean replying to a
    conversation that happened before the avatar was in the room."""

    chat_require_mention: bool = True
    """Answer only chat messages that ``@``-tag the avatar, ignoring the rest of the conversation.

    Meeting chat is a conversation between people — links, greetings, participants answering
    each other — and an avatar that replies to every line is interrupting a room that was not
    talking to it. With this on, "@AI Avatar what is the notice period?" is answered while
    "sounds good, thanks!" and "did the AI avatar join?" are not.

    Meet has no mention feature: no autocomplete, no participant token, nothing structural in
    the DOM. The ``@`` is therefore the only deliberate signal a participant can give, and it
    is **required** — it is what separates talking to the avatar from talking about it. What
    follows it is matched loosely, ignoring case and optional separators, so ``@AI Avatar``,
    ``@ai_avatar``, ``@ai-avatar`` and ``@AIAvatar`` all count; the name must still stand as
    whole words, so ``@Aisha`` does not trigger an avatar named "AI". The mention is stripped
    before the text reaches the agent.

    Set false to go back to answering every message — reasonable for a one-to-one meeting,
    where everything typed is addressed to the avatar anyway."""

    chat_mention_names: list[str] = Field(default_factory=list)
    """Extra names the avatar answers to after an ``@``, on top of the one Meet shows for its
    own account.

    Usually unnecessary: the rendered name is read from the roster, so whatever participants
    see is already matched. Worth setting when the account's name is long or awkward to type
    and people will shorten it — an account called "TrueFan Interview Avatar" gets
    ``["Gunika", "bot"]`` so ``@Gunika`` and ``@bot`` are recognised too."""

    attendance_enabled: bool = True
    """Keep a record of who was in the meeting, so the agent can be asked about it later.

    On by default, because unlike ``chat_enabled`` it costs nothing and risks nothing: it adds
    no DOM scanning, no new page observer, and no visible UI action. It subscribes to the roster
    stream the connector already receives and accumulates it in Python — see
    ``connectors/google_meet/meeting/attendance.py``.

    What it makes answerable: who is here now, who was here and left, who rejoined, and — when
    the session has been seeded with an invite list via ``POST /sessions/{id}/invitees`` — who
    was invited and never turned up. Read it back with ``GET /sessions/{id}/participants``.

    Turn it off to have the connector remember nothing about who attended. The roster itself is
    unaffected either way; this only controls whether its history is kept."""

    attendance_push_enabled: bool = True
    """Push the attendance brief to the avatar agent, so it can answer without a round trip.

    Requires ``attendance_enabled``. On by default because without it the feature does not do
    what people expect: in a live meeting the bridge knew exactly who was present and the agent
    still answered *"I don't have access to your meeting details"* — because nothing carried the
    ledger over the avatar socket.

    Delivered as ``kind="meeting_context"``, which is **not** the channel chat and raised hands
    use. That distinction is the point: a chat frame is a turn the avatar answers out loud, and
    an avatar announcing "Aarav Sharma is in the meeting" every time somebody reconnects is
    worse than one that says nothing. An agent that has not implemented the kind negotiates
    below ``1.2`` and receives nothing, with one warning logged naming the fix.

    Turn it off if the agent reads attendance from ``GET /sessions/{id}/participants`` instead —
    a tool-calling agent gets fresher data that way, at the cost of a round trip mid-answer."""

    attendance_push_require_negotiation: bool = True
    """Only send the brief to an agent that negotiated protocol ``1.2`` or above.

    **Set this to False to skip the agent's handshake change.** Adding attendance to an existing
    agent otherwise takes two edits — reply ``"1.2"`` in the server hello, *and* handle
    ``kind="meeting_context"`` — and forgetting the first silently disables the feature while
    the second looks done. With this off the frame is sent regardless of the negotiated version,
    so only the handler is needed.

    Safe for any agent that **ignores control frames it does not recognise**, which is the usual
    behaviour and the only requirement. Leave it on if the agent instead raises on an unknown
    kind, because then an undeliverable frame becomes an error on the avatar socket rather than
    a warning in this log.

    Not a licence to send it as chat: this changes *who is sent the new frame*, never the frame's
    kind. Attendance never travels on the channel the avatar speaks from."""

    attendance_push_interval_s: float = Field(default=5.0, ge=0.5, le=120.0)
    """How often the ledger is checked for changes worth pushing.

    Not how often anything is sent: a meeting whose roster is unchanged sends nothing at all,
    because the brief is standing context and resending an identical one is noise in the agent's
    context window. Five seconds sits between the page's own 250 ms scan floor — low enough that
    a burst of roster churn collapses into one push — and the moment a new arrival finishes
    saying hello."""

    hand_raise_enabled: bool = True
    """Stop the avatar and hand over when a participant raises their hand in the meeting.

    Raising a hand is the one gesture Meet gives a person for "I would like to speak", and an
    avatar that keeps talking through it is the complaint this setting exists to answer. With
    this on, the moment a hand goes up the avatar's audio is cut and the agent is told to yield
    — so what the room hears is the avatar stopping mid-sentence and saying something like "of
    course, go ahead".

    Unlike ``chat_enabled`` this changes nothing other participants can see: the indicator is
    already on screen and the page only reads it. Turn it off for a meeting where the avatar
    should hold the floor — a presentation, or a scripted read-out."""

    hand_raise_prompt: str = ""
    """What the agent is told when a hand goes up. Empty uses the wording that ships with the
    connector (``connectors/google_meet/meeting/hand_raise.py``), which asks it to stop and
    hand over in a few words.

    ``{name}`` is substituted with the raiser's name, or "Someone" when Meet renders an
    indicator it does not attribute. Nothing else is substituted, and a template that fails to
    render costs the wording rather than the feature — the default is used and a warning is
    logged.

    **This steers the avatar's reply; it is not the reply.** The bridge contains no AI and
    speaks none of its own words — the agent composes what is said, and this is the instruction
    it receives. Change it to change the register: an interview avatar might want
    ``"{name} has a question. Stop talking and invite them to ask it."``"""

    hand_raise_cooldown_s: float = Field(default=10.0, ge=0)
    """How long the same participant's hand is ignored after the avatar has yielded to it.

    Meet's indicator lives in a DOM that re-renders constantly, and somebody who feels ignored
    will lower and re-raise. Either can produce a burst, and an avatar interrupted repeatedly
    never gets far enough to say "go ahead" — which looks far more broken than a slightly late
    reaction. Ten seconds is comfortably longer than a re-render storm and shorter than a turn
    in a conversation. Zero disables it, which is only sensible with a very quiet room."""

    hand_raise_mute_ms: int = Field(default=800, ge=0)
    """How long avatar audio keeps being discarded after an interrupt.

    **This is what makes barge-in audible rather than theoretical.** When the hand goes up the
    agent's speech is already in flight — sent over the socket, sitting in the decoder — and
    dropping only what is queued for publication buys a couple of hundred milliseconds before
    the same sentence resumes. Holding the line while the rest drains is what turns that into
    stopping.

    The trade is in both directions: too short and the interrupted sentence comes back, too
    long and the beginning of the agent's *reply* is clipped. 800 ms covers a typical
    in-flight buffer and is shorter than the round trip the agent needs to answer, so the
    "go ahead" lands intact. Raise it if the avatar audibly resumes; lower it if the reply
    starts mid-word. Zero drops only what is already queued."""

    captions_enabled: bool = True
    """**Read Meet's live captions, so the avatar knows who said what — not just who is talking.**

    This is what makes an avatar able to answer *"what did they ask you?"* and *"what did Dev
    say?"*. Without it those questions are unanswerable in principle, and that is worth
    understanding rather than taking on trust: the avatar's own transcription lives in the agent,
    which receives **one mixed stream** and therefore knows the words without knowing whose they
    are; this connector knows who is speaking without knowing the words, because it measures audio
    levels rather than speech. Meet's caption panel is the only place in the meeting where a name
    and the words that person said appear together, because Meet transcribes per participant.

    **Invisible to the meeting, unlike `chat_enabled`.** Captions are rendered locally for whoever
    switched them on; nobody else sees the avatar enable them, and no participant's own caption
    setting changes.

    Also the strongest *attribution* signal available: a caption naming somebody is Meet telling
    us who is talking, in words, which beats every indicator this connector could match on.

    Turn it off to have the connector record nothing about what was said. Ingest is identical
    either way — this is a DOM read of a small panel, and the audio path is untouched.

    Two honest caveats. Captions are Meet's transcription, so wording is approximate and names and
    technical terms are often misheard; the brief says so, so the agent does not quote them as
    verbatim fact. And they are English-first — a Hindi turn is captioned in Hindi only if the
    meeting's caption language is set to it."""

    speaker_tracking_enabled: bool = True
    """**Identify who is speaking, and keep that attribution for the whole meeting.**

    On by default because it costs nothing the meeting can hear. The audio the avatar receives is
    a mix and stays one — no frame is retagged, re-timed, or delayed — and the attribution is
    assembled from two observations taken *beside* the media path: the level of each remote track,
    measured on an ``AnalyserNode`` branched off the node that already feeds the mix, and the
    participant tile Meet renders that track's stream on. See
    ``connectors/google_meet/meeting/active_speaker.py``.

    What it makes answerable: who is talking right now, who has spoken, in what order, and for
    how long each. Read it with ``GET /sessions/{id}/speakers``, or have the agent be told
    (``speaker_push_enabled``). It also names a barge-in: with this off, somebody talking over the
    avatar is reported to the agent as "Someone", and with it on they are reported by name.

    Turn it off to have the connector observe nothing about who is speaking. Ingest is identical
    either way — that is the property this feature was built to preserve."""

    speaker_push_enabled: bool = True
    """Tell the agent who is speaking, as silent context.

    Requires ``speaker_tracking_enabled``. Delivered as ``kind="meeting_context"``, which is
    **not** the channel chat and raised hands use, and that distinction is the safety property:
    a chat frame is a turn the avatar says out loud, so pushing speaker changes down it would have
    the avatar narrate the meeting — "Priya is speaking now" — into the room. Context is silent;
    the agent knows, and mentions it only if asked.

    Sent on change and never on a timer, so a still meeting sends nothing at all.

    Turn it off if the agent reads ``GET /sessions/{id}/speakers`` instead, or if its context
    window is better spent on something else — the barge-in attribution and the endpoint both
    keep working without it."""

    speaker_push_interval_s: float = Field(default=3.0, ge=0.5, le=60.0)
    """How often the tracker is checked for a change of speaker.

    Not how often anything is sent. Faster than ``attendance_push_interval_s`` because the floor
    changes hands on the timescale of a sentence rather than of somebody joining, and far slower
    than the page's own 200 ms sampling — the *history* is exact to a fifth of a second, and this
    only decides how often the agent is re-briefed about it."""

    speaker_push_require_negotiation: bool = True
    """Only send the brief to an agent that negotiated protocol ``1.2`` or above.

    Same escape hatch, and same caveat, as ``attendance_push_require_negotiation``: set it False
    to skip the agent's handshake change when the agent ignores control frames it does not
    recognise, and leave it on if the agent instead raises on an unknown kind. It changes who is
    sent the frame, never the frame's kind — speaker context never travels on the channel the
    avatar speaks from."""

    speaker_hold_ms: int = Field(default=1_500, ge=0)
    """How long somebody stays "the current speaker" after they stop.

    Speech has gaps at every clause boundary, and the page's release window is short on purpose so
    a turn *ends* promptly. Without a hold, asking who is speaking during the pause between two
    sentences answers "nobody" — true of that instant, and the wrong answer to the question. It
    is also what stops a barge-in landing in a gap from being attributed to no one.

    Zero disables it, which makes ``current_speaker`` a statement about this exact moment."""

    speaker_merge_gap_ms: int = Field(default=1_200, ge=0)
    """How long a gap may be before it ends a turn rather than punctuating one.

    This is what makes the history read like a conversation instead of like a waveform: without
    it, one person talking for a minute is forty turns, and "who has been speaking" answers with
    the same name forty times. A gap longer than this is treated as the floor changing hands —
    even if it goes back to the same person, which is then two turns because it was.

    Zero records every detected stretch separately."""

    speech_interrupt_enabled: bool = True
    """**Treat somebody starting to speak exactly as if they had raised their hand.**

    That is the whole feature, and it deliberately has no behaviour of its own: when speech is
    detected the connector runs the same handover ``hand_raise_enabled`` runs — the avatar's
    queued audio is dropped and the agent is sent ``hand_raise_prompt``, so it stops
    mid-sentence and says "ok, go ahead" before listening to the question.

    Both halves matter and neither is sufficient. Dropping the queued audio disposes of speech
    that already exists, but the agent goes on *generating* the rest of its sentence and
    resumes the moment the hold lapses; telling the agent stops that but takes a round trip,
    during which the avatar would talk over the person. A raised hand has always done both,
    which is why this does nothing else.

    ``hand_raise_mute_ms`` and ``hand_raise_prompt`` therefore govern this too — one handover,
    two ways in. Requires the echo gate to be open, which it is on this connector: a shut gate
    drops the interrupting voice along with the echo, and there is nothing for it to catch
    here anyway (``connectors/google_meet/egress/media_sink.own_participant``).

    Turn it off for a meeting where the avatar should hold the floor through noise — a
    presentation into a room with an open microphone — and a raised hand still interrupts."""

    speech_interrupt_threshold: int = Field(default=350, ge=0)
    """The floor under the speech trigger, in int16 RMS amplitude — **not the trigger**.

    The trigger actually applied is ``max(this, noise_floor * 3)``, where the noise floor is
    learned from the meeting while nobody is speaking. That is the answer to a fixed threshold
    being wrong in every room but the one it was tuned in: a quiet room gets a low bar and a
    loud one a high bar, with nothing to set. This value only keeps a near-silent meeting from
    setting a bar low enough that line noise takes the floor.

    ``router.speech_detected`` logs the measured ``rms``, ``noise_floor`` and ``trigger_level``
    on every trigger. Tune against those numbers rather than guessing.

    **One thing no threshold can fix.** A participant listening on *speakers* has an echo
    canceller that suppresses their own voice while the avatar is talking — measured at 20-100x
    quieter than the same person speaking into silence. A headset removes it entirely."""

    def is_configured(self) -> bool:
        """True when the connector has a profile to launch from."""
        return self.profile_dir is not None


class AvatarSettings(BaseModel):
    """Streaming Avatar Agent connection settings."""

    url: str = "ws://localhost:8100/stream"
    connect_timeout_s: float = Field(default=10.0, gt=0)
    reconnect_initial_delay_s: float = Field(default=0.5, gt=0)
    reconnect_max_delay_s: float = Field(default=15.0, gt=0)
    reconnect_max_attempts: int = Field(default=10, ge=1)
    send_queue_size: int = Field(default=25, ge=1)
    """Bounded outbound queue, ~500 ms at 20 ms frames. Overflow drops oldest and
    counts it — it must never block the ingest reader (doc 003 §7.2)."""


class ZoomWebSettings(BaseModel):
    """Zoom joined with a browser, publishing through a virtual microphone.

    Publishing works the same way the Google Meet connector's does — a synthetic
    ``MediaStreamTrack`` injected into the page — with one extra requirement Meet does
    not have: a **persistent profile with a microphone already selected**. Zoom will
    not start its capture pipeline until its device menu has a selection, and that
    selection lives in the Chromium profile.

    Ingest comes over RTMS, Zoom's own API, which carries the audio and the speaker's
    name and needs nothing from the browser at all.
    """

    enabled: bool = False
    """Opt-in, like every connector after the first. It takes no credentials of its
    own — the meeting number and passcode arrive in the request — so there is nothing
    to infer "wanted" from, and it carries a host dependency that should be a
    deliberate choice."""

    display_name: str = "AI Avatar"

    per_participant_audio: bool = False
    """Ask RTMS for one **mixed** stream rather than a stream per participant.

    The opposite of the SDK connector's default, and for a reason specific to this
    one: here the avatar is *in* the meeting, so RTMS carries at least two speakers.
    ``AUDIO_MULTI_STREAMS`` then delivers their audio as separate streams, which this
    pipeline drains into a single sequential one — splicing two speakers together and
    handing the transcriber audio that is chopped between them. Observed live: an
    English question came back as Indonesian fragments, and transcripts lagged by
    fourteen seconds.

    A mixed stream is one coherent conversation, which is what the transcriber wants.
    The cost is losing per-speaker attribution — so the avatar's own voice can no
    longer be filtered by name, and ``EchoGuard`` runs in strict gate mode instead.
    That is the case the strict gate exists for, and it is armed by *audible* avatar
    audio only, so a participant can still interrupt.
    """

    join_timeout_s: float = Field(default=90.0, gt=0)
    """Generous, because it spans a waiting room: failing early turns a slow host
    into an error."""
    join_poll_interval_s: float = Field(default=2.0, gt=0)

    headless: bool = True
    no_sandbox: bool = False

    profile_dir: Path | None = None
    """A persistent Chromium profile, signed in to Zoom with a microphone chosen.

    **This is what makes the synthetic microphone work**, and it is the one piece of
    setup this connector needs. Chromium stores the per-origin device choice in
    ``Default/Preferences``; with a throwaway profile Zoom has no microphone selected,
    never starts its capture pipeline, and publishes nothing however good the injected
    track is. Prepared once, interactively — see ``docs`` and
    ``scripts/zoom_web_login.py``.

    ``None`` falls back to a throwaway profile, which is fine for exercising the join
    and useless for being heard."""

    @field_validator("profile_dir")
    @classmethod
    def _expand(cls, value: Path | None) -> Path | None:
        """Expand ``~`` and make the path absolute.

        Pydantic parses ``~/.mc/zoom-web-profile`` into a *relative* path whose
        first component is literally ``~``, so Chromium silently launches with an
        empty profile in the working directory. Everything then looks correct — the
        avatar joins and reports healthy — while Zoom has no microphone selected and
        publishes nothing, which is the hardest failure in this connector to see.
        """
        if value is None:
            return None
        return value.expanduser().resolve()

    echo_gate_hangover_ms: int | None = None
    """How long after the avatar stops publishing the echo gate keeps withholding inbound
    audio. ``None`` uses the shared ``MC_MEDIA__ECHO_GATE_HANGOVER_MS`` (200 ms).

    **This connector needs a far longer hangover than the shared default, and the reason is
    specific to it.** RTMS delivers the meeting's mix *including the avatar*, so the avatar's
    own voice comes back — page → Zoom encode → Zoom mix → RTMS → us — with a round trip well
    over a second. A mixed stream carries no per-frame attribution, so ``SelfAudioFilter``
    cannot bite (it matches on a name that is only present with per-participant audio), which
    leaves the speaking gate as the only defence. At 200 ms the gate reopens long before the
    *tail* of each utterance arrives, and that tail is forwarded to the agent, transcribed,
    and answered.

    Observed exactly that way in a live meeting with the human's microphone muted — so the
    only audio in the mix was the avatar's own. Every "user" turn the agent received was the
    final word of the avatar's own preceding sentence:

        avatar: "...কোনো সাহায্য প্রয়োজন?"   →   user: "প্রয়োজন।"
        avatar: "...কীভাবে সাহায্য করতে পারি।" →   user: "পারি।"

    The agent then answered its own words, in a loop.

    **Why 200 ms was right where it was chosen and wrong here.** It was tuned for a connector
    whose barge-in depends on *hearing* the interruption — a shut gate drops the interrupting
    voice along with the echo, so the window had to stay small. This connector does not
    detect barge-in from audio at all: it uses Zoom's ``ACTIVE_SPEAKER_CHANGE`` event on the
    signaling socket, which arrives whether or not the gate is withholding frames. So the
    gate can be as conservative as the echo requires, and costs nothing it used to cost.

    Raise it if the agent still answers its own tail; lower it if the first word of a reply
    to the avatar is being clipped. The gate is also released the moment an interruption is
    delivered, so a barge-in does not have to wait this out — see ``MediaRouter._yield_floor``.
    """

    # -- meeting awareness -------------------------------------------------
    #
    # **Almost everything below is served by RTMS rather than by the browser**, which is
    # the structural difference from ``GoogleMeetSettings``. Zoom reports who joined, who
    # left, who is speaking, what each person said and what they typed — each with a name
    # attached — so these switches turn *subscriptions and ledgers* on and off rather than
    # DOM observers. The one exception is ``hand_raise_enabled``, because RTMS has no
    # hand-raise event and the indicator exists only on screen.

    rtms_transcript_enabled: bool = True
    """Subscribe to Zoom's live transcript, so the avatar knows **who said what**.

    This is what makes an avatar able to answer *"what did they ask you?"* and *"what did
    Dev say?"*, and those questions are otherwise unanswerable in principle: the avatar's
    own transcription lives in the agent, which receives one mixed stream and knows the
    words without knowing whose they are, while this connector knows who is speaking
    without knowing the words. Zoom transcribes per participant, so its transcript is the
    only place the two arrive together.

    **Requires RTMS transcription to be enabled for the app on the Zoom account.** If it is
    not, Zoom refuses the data handshake — and because a refused handshake ends the whole
    connection, the connector retries once with audio alone rather than letting the avatar
    go deaf. The reason then appears in the ingest component's health detail. Set this
    false to stop asking.

    Invisible to the meeting: nobody sees the avatar enable anything, and no participant's
    own caption setting changes."""

    rtms_chat_enabled: bool = True
    """Subscribe to the meeting's chat over RTMS.

    Zoom delivers each message with the sender's name, so no panel is opened and nothing is
    scraped — the whole visible-UI-action objection that makes ``chat_enabled`` a judgement
    call on Google Meet does not apply here. Same handshake caveat as
    ``rtms_transcript_enabled``.

    This controls whether messages *arrive*. Whether the avatar answers them is
    ``chat_enabled``, and whether they are remembered is ``transcript_enabled`` — three
    different questions that were one setting until it became clear they were not."""

    rtms_events_enabled: bool = True
    """Subscribe to participant join/leave and active-speaker events.

    The source of attendance, of who-is-speaking, and of voice barge-in. Best-effort by
    construction: some accounts deliver these unsolicited, so a rejected subscription is
    never allowed to fail an attach that otherwise succeeded."""

    chat_enabled: bool = True
    """Forward meeting chat to the avatar agent, so a typed question gets a spoken answer.

    Requires ``rtms_chat_enabled``, which is what makes the messages arrive at all. Turn
    this off — with ``rtms_chat_enabled`` left on — for an avatar that never replies to the
    chat but still remembers what was typed when asked to summarise the meeting."""

    chat_require_mention: bool = True
    """Answer only chat messages that ``@``-tag the avatar, ignoring the rest of the room.

    Meeting chat is a conversation between people — links, greetings, participants
    answering each other — and an avatar that replies to every line is interrupting a room
    that was not talking to it. With this on, "@AI Avatar what is the notice period?" is
    answered while "sounds good, thanks!" and "did the avatar join?" are not.

    Zoom's chat box offers an ``@`` autocomplete, but what reaches RTMS is plain text with
    no participant token in it — so the ``@`` is the only deliberate signal that survives
    the wire, and it is **required**. What follows it is matched loosely, ignoring case and
    optional separators, so ``@AI Avatar``, ``@ai_avatar`` and ``@AIAvatar`` all count; the
    name must still stand as whole words, so ``@Aisha`` does not trigger an avatar named
    "AI". The mention is stripped before the text reaches the agent.

    Set false to answer every message — reasonable for a one-to-one meeting, where
    everything typed is addressed to the avatar anyway."""

    chat_mention_names: list[str] = Field(default_factory=list)
    """Extra names the avatar answers to after an ``@``, on top of its ``display_name``.

    Worth setting when the joined name is long or awkward to type and people will shorten
    it — an avatar joining as "TrueFan Interview Avatar" gets ``["Gunika", "bot"]`` so
    ``@Gunika`` and ``@bot`` are recognised too."""

    transcript_enabled: bool = True
    """Keep a ledger of what each person said — spoken and typed — for the whole meeting.

    Fed by whichever of ``rtms_transcript_enabled`` and ``rtms_chat_enabled`` are on, and
    it is worth having with either: a meeting held largely in chat still has a conversation
    to remember, and gating the ledger on the transcript alone would leave that deployment
    able to answer every question except what was asked.

    Read it back with ``GET /sessions/{id}/transcript``. The recent lines are also folded
    into the brief pushed to the agent."""

    attendance_enabled: bool = True
    """Keep a record of who was in the meeting, so the agent can be asked about it later.

    On by default because it costs nothing: no scanning, no visible action, and no extra
    traffic — it accumulates the participant events ``rtms_events_enabled`` already
    subscribes to.

    What it makes answerable: who is here now, who was here and left, who rejoined, and —
    when the session has been seeded via ``POST /sessions/{id}/invitees`` — who was invited
    and never turned up. Read it back with ``GET /sessions/{id}/participants``."""

    speaker_tracking_enabled: bool = True
    """Identify who is speaking, and keep that attribution for the whole meeting.

    On by default because it costs nothing the meeting can hear: the audio the avatar
    receives is a mix and stays one — no frame is retagged, re-timed or delayed — and the
    attribution comes from Zoom's own ``ACTIVE_SPEAKER_CHANGE`` events, which travel on the
    signaling socket rather than the media one.

    What it makes answerable: who is talking right now, who has spoken, in what order and
    for how long each. Read it with ``GET /sessions/{id}/speakers``. It also names a
    barge-in: with this off, somebody talking over the avatar is reported to the agent as
    "Someone"."""

    context_push_enabled: bool = True
    """Push the meeting brief to the avatar agent, so it can answer without a round trip.

    Who is here, who is talking, and what has been said — as **one** frame, because an agent
    has one slot for standing context and two pushers competing for it evict each other.

    Delivered as ``kind="meeting_context"``, which is **not** the channel chat and
    interruptions use. That distinction is the point: a chat frame is a turn the avatar
    answers out loud, and an avatar announcing "Aarav Sharma is in the meeting" every time
    somebody reconnects is worse than one that says nothing. An agent that has not
    implemented the kind negotiates below ``1.2`` and receives nothing, with one warning
    logged naming the fix.

    Turn it off if the agent reads the HTTP endpoints instead — a tool-calling agent gets
    fresher data that way, at the cost of a round trip mid-answer."""

    context_push_interval_s: float = Field(default=3.0, ge=0.5, le=120.0)
    """How often the ledgers are checked for changes worth pushing.

    Not how often anything is sent: a meeting where nothing changed sends nothing at all,
    because the brief is standing context and resending an identical one is noise in the
    agent's context window. Three seconds is the timescale a speaker changes on, which is
    the fastest thing in the brief."""

    context_push_require_negotiation: bool = True
    """Only send the brief to an agent that negotiated protocol ``1.2`` or above.

    **Set this to False to skip the agent's handshake change.** Adding meeting context to
    an existing agent otherwise takes two edits — reply ``"1.2"`` in the server hello, *and*
    handle ``kind="meeting_context"`` — and forgetting the first silently disables the
    feature while the second looks done.

    Safe for any agent that **ignores control frames it does not recognise**, which is the
    usual behaviour and the only requirement. Leave it on if the agent instead raises on an
    unknown kind. It changes who is sent the frame, never the frame's kind: meeting context
    never travels on the channel the avatar speaks from."""

    voice_interrupt_enabled: bool = True
    """**Let somebody talking over the avatar stop it mid-sentence.**

    The fix for an avatar that speaks until it finishes whatever anybody says. When Zoom
    reports the floor moving to a participant *while the avatar is talking*, the avatar's
    queued audio is dropped and the agent is sent ``hand_raise_prompt`` — so it stops and
    says "ok, go ahead" before listening to the question. Both halves matter: dropping the
    queued audio disposes of speech that already exists, and only telling the agent stops
    it generating the rest of the sentence.

    **Driven by Zoom's active-speaker event rather than by audio energy**, which is the one
    place this connector cannot copy the Google Meet one. RTMS delivers the meeting's mix
    *including the avatar*, so the echo gate withholds every inbound frame while the avatar
    talks — an energy detector would be deaf during the only window barge-in exists for.
    The event travels on the signaling socket, so it arrives regardless, and it names the
    person.

    Only fires while the avatar is actually speaking. Somebody starting to talk into a
    silence is just the meeting happening, and interrupting nothing would send the agent a
    "stop talking" message on every sentence anybody utters.

    Turn it off for a meeting where the avatar should hold the floor through interruptions
    — a presentation, or a scripted read-out. A raised hand still interrupts."""

    hand_raise_enabled: bool = True
    """Stop the avatar and hand over when a participant raises their hand.

    **The one feature here read from the browser rather than from RTMS**, and that is a
    genuine gap rather than an oversight: Zoom's RTMS event list has no hand-raise event in
    it, so the indicator exists only on screen. The injected script watches for it and
    reports the moment a hand goes up; every judgement about what that means is made in
    Python.

    Unlike everything else on this connector it therefore depends on selectors matching a
    UI Zoom is free to change. It degrades to silence rather than to an error, which is why
    the observer reports diagnostics — see ``hand_raise_open_panel`` for the most common
    reason it finds nothing."""

    hand_raise_open_panel: bool = True
    """Open the participants panel once, so raised hands are in the DOM to be seen.

    **The indicator does not exist in a panel nobody opened.** With it closed Zoom renders a
    raised hand as a transient toast and, on some layouts, nothing at all — so the observer
    would be correct, running, and permanently blind. This is the one visible action the
    avatar takes inside the meeting, which is why it is a switch; clicked once per session,
    never toggled.

    Turn it off if the avatar's screen is being shared and the panel would be in the way,
    accepting that hand raises will probably not be seen."""

    hand_raise_prompt: str = ""
    """What the agent is told when somebody asks for the floor — by hand **or** by voice.

    Empty uses the wording that ships with the connector, which asks it to stop and hand
    over in a few words. ``{name}`` is substituted with the person's name, or "Someone" when
    nothing attributed the request. Nothing else is substituted, and a template that fails
    to render costs the wording rather than the feature.

    **This steers the avatar's reply; it is not the reply.** The bridge contains no AI and
    speaks none of its own words — the agent composes what is said, and this is the
    instruction it receives. Change it to change the register: an interview avatar might
    want ``"{name} has a question. Stop talking and invite them to ask it."``"""

    hand_raise_cooldown_s: float = Field(default=10.0, ge=0)
    """How long the same participant is ignored after the avatar has yielded to them.

    Both inputs repeat: the page re-reads a hand that has not moved, and Zoom re-reports the
    same active speaker through a conversation. Either can produce a burst, and an avatar
    interrupted repeatedly never gets far enough to say "go ahead" — which looks far more
    broken than a slightly late reaction. Zero disables it, which is only sensible in a very
    quiet room."""

    hand_raise_mute_ms: int = Field(default=800, ge=0)
    """How long avatar audio keeps being discarded after an interrupt.

    **This is what makes barge-in audible rather than theoretical.** When the floor is
    claimed the agent's speech is already in flight — sent over the socket, sitting in the
    decoder — and dropping only what is queued for publication buys a couple of hundred
    milliseconds before the same sentence resumes. Holding the line while the rest drains is
    what turns that into stopping.

    The trade runs both ways: too short and the interrupted sentence comes back, too long and
    the beginning of the *reply* is clipped. 800 ms covers a typical in-flight buffer and is
    shorter than the round trip the agent needs to answer, so the "go ahead" lands intact."""

    speaker_hold_ms: int = Field(default=1_500, ge=0)
    """How long somebody stays "the current speaker" after the floor moves off them.

    Speech has gaps at every clause boundary. Without a hold, asking who is speaking during
    the pause between two sentences answers "nobody" — true of that instant, and the wrong
    answer to the question. It is also what stops a barge-in landing in a gap from being
    attributed to no one."""

    speaker_merge_gap_ms: int = Field(default=1_200, ge=0)
    """How long a gap may be before it ends a turn rather than punctuating one.

    What makes the history read like a conversation instead of like a waveform: without it,
    two people alternating quickly produce dozens of turns and "who has been speaking"
    answers with the same names over and over."""

    def is_configured(self) -> bool:
        return self.enabled


class MediaSettings(BaseModel):
    """Media pipeline geometry and queue depths."""

    video_width: int = Field(default=1280, ge=2)
    video_height: int = Field(default=720, ge=2)
    video_fps: int = Field(default=25, ge=1, le=30)

    publish_sample_rate_hz: int = Field(default=32_000, ge=8_000)
    """Sample rate fed to the Meeting SDK virtual microphone. A config value, not a
    constant, until confirmed against the SDK headers in M5 (doc 003 §9 Q4)."""
    publish_channels: int = Field(default=1, ge=1, le=2)

    inbound_queue_size: int = Field(default=50, ge=1)
    video_queue_size: int = Field(default=3, ge=1)

    audio_queue_size: int = Field(default=50, ge=1)
    """Decoded avatar audio chunks the pacer may hold, at 20 ms each.

    **Fifty, because ten was ten times too few and it cost the avatar its voice.** The number
    is a jitter budget, and the jitter it has to absorb is the agent's: an utterance is
    synthesised faster than it is spoken, and delivery stalls and catches up. A live session
    showed the agent's own ffmpeg pausing about half a second and resuming at 1.05x, roughly
    every ten seconds. Ten chunks is 200 ms of headroom against half-second bursts — every one
    of them overflowed, and the overflow is speech.

    One second is enough for the bursts observed and cheap: 20 ms chunks of 48 kHz mono are
    96 KB in total. It is a *ceiling*, not a target — the queue sits near empty whenever the
    agent is keeping pace, and ``AUDIO_BACKLOG_TRIM_US`` gives back whatever a burst leaves
    behind during the next pause, so the latency does not accumulate.

    Raise it if ``pacer.audio_lost`` still appears; that warning names this setting."""

    echo_gate_hangover_ms: int = Field(default=200, ge=0)
    """How long after the avatar stops publishing the echo gate stays shut."""

    idle_clip_path: Path | None = None
    """Optional packed raw-I420 loop shown while the avatar is silent. When unset the
    last real frame is held, falling back to a neutral field (doc 003 §1.4)."""

    def video_format(self) -> VideoFormat:
        return VideoFormat(width=self.video_width, height=self.video_height, fps=self.video_fps)

    def publish_audio_format(self) -> AudioFormat:
        return AudioFormat(
            sample_rate_hz=self.publish_sample_rate_hz,
            channels=self.publish_channels,
            sample_format=SampleFormat.S16LE,
        )


class SidecarSettings(BaseModel):
    """C++ publisher sidecar IPC settings (M5)."""

    uds_path: Path = Path("/run/meeting-connectors/sidecar.sock")
    connect_timeout_s: float = Field(default=15.0, gt=0)
    heartbeat_interval_s: float = Field(default=2.0, gt=0)
    heartbeat_timeout_s: float = Field(default=6.0, gt=0)
    reconnect_max_attempts: int = Field(default=10, ge=1)


class Settings(BaseSettings):
    """Root application settings."""

    model_config = SettingsConfigDict(
        env_prefix="MC_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "meeting-connectors"
    env: Environment = Environment.LOCAL

    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
    zoom: ZoomSettings = Field(default_factory=ZoomSettings)
    teams: TeamsSettings = Field(default_factory=TeamsSettings)
    google_meet: GoogleMeetSettings = Field(default_factory=GoogleMeetSettings)
    zoom_web: ZoomWebSettings = Field(default_factory=ZoomWebSettings)
    avatar: AvatarSettings = Field(default_factory=AvatarSettings)
    media: MediaSettings = Field(default_factory=MediaSettings)
    sidecar: SidecarSettings = Field(default_factory=SidecarSettings)
