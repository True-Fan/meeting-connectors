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
    audio_queue_size: int = Field(default=10, ge=1)

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
    avatar: AvatarSettings = Field(default_factory=AvatarSettings)
    media: MediaSettings = Field(default_factory=MediaSettings)
    sidecar: SidecarSettings = Field(default_factory=SidecarSettings)
