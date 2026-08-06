"""Google Meet connector configuration.

A flattened, connector-local view of ``Settings`` — the same pattern as
``connectors/zoom/config.py`` and ``connectors/teams/config.py``, and for the same
reason: this feature depends on the fields it needs rather than on the whole settings
tree, so an unrelated setting cannot change this package and a test can build one of
these in a line.

The three connectors do **not** share a config base class. Their fields overlap only
where the infrastructure is genuinely shared — the avatar agent, queue depths, echo
timing — and a common base would couple three release cycles together to save a few
dozen lines.

**What is validated here, and why here.** Two classes of mistake are worth catching at
startup rather than mid-join:

* Geometry the synthetic camera track cannot carry. Unlike Teams there is no enumerated
  format list to check against — a canvas-backed track takes any even geometry — so the
  check is the I420 plane-layout rule that ``VideoFormat`` already enforces, plus a
  ceiling, because a 4K avatar frame is 12 MB and would saturate the loopback channel
  before Meet ever downscaled it.
* A sample rate the browser will silently resample. Web Audio resamples anything, so a
  mismatch here does not fail — it degrades, quietly, and shows up as a pitch artefact
  nobody can trace. Restricting the rate makes it a configuration error instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydantic import SecretStr

from src.config.settings import Settings
from src.connectors.google_meet.exceptions import MeetConfigurationError
from src.domain.avatar import AVATAR_INPUT_FORMAT
from src.domain.media import AudioFormat, SampleFormat, VideoFormat

MAX_PUBLISH_PIXELS = 1920 * 1080
"""Ceiling on the synthetic camera's frame size.

Not a Meet limit — Meet negotiates its own send resolution and will downscale whatever
the track offers. This bounds *our* cost: every frame crosses a loopback WebSocket as
raw I420, so 1080p is already 3.1 MB per frame and 4K would be 12.4 MB. At 25 fps that
is the difference between ~78 MB/s and ~311 MB/s of memcpy and JSON-free framing work,
for pixels the far end throws away."""

SUPPORTED_PUBLISH_SAMPLE_RATES: frozenset[int] = frozenset({16_000, 24_000, 48_000})
"""Rates the playout ``AudioWorklet`` is built for.

48 kHz is the default and the right answer: it is Web Audio's native rate on every
desktop Chromium, so the synthetic microphone track needs no resampling stage at all.
The other two are permitted because they divide into 48 kHz exactly, which keeps the
browser's implicit resample a clean integer upsample rather than a fractional one."""


@dataclass(frozen=True, slots=True)
class GoogleMeetConnectorConfig:
    """Everything the Google Meet connector needs, and nothing else."""

    # -- Chromium -----------------------------------------------------------
    profile_dir: Path | None
    """Persistent Chromium profile. **This is where the Google session lives**, so it
    is the one piece of durable state the connector has and the reason a launch is a
    ``launch_persistent_context`` rather than a fresh browser (see
    ``browser/profile.py``).

    Optional here, mirroring the setting, rather than defaulted to a plausible path. A
    substituted default would make ``is_configured()`` unable to ever return False, so an
    unconfigured deployment would register the connector and then fail on first use — which
    is precisely the outcome conditional registration exists to prevent."""
    chromium_executable: Path | None
    headless: bool
    browser_launch_timeout_s: float
    extra_browser_args: tuple[str, ...]

    # -- Google account -----------------------------------------------------
    google_email: str
    google_password: SecretStr
    """Optional. Present only to bootstrap an empty profile; the supported path is to
    authenticate the profile once, interactively. See ``auth/google_login.py`` for why
    scripted sign-in is offered but not relied upon."""

    # -- Join ---------------------------------------------------------------
    display_name: str
    join_timeout_s: float
    lobby_timeout_s: float
    """How long to wait in "Asking to join" before giving up. Deliberately much longer
    than ``join_timeout_s``: a human host has to notice and click, and failing after
    30 seconds would abandon meetings that were about to admit us."""

    # -- Page bridge --------------------------------------------------------
    bridge_host: str
    bridge_port: int
    """``0`` means "let the OS choose", which is the default and the right one: one
    server is bound per session, so a fixed port would make two concurrent sessions
    collide."""
    bridge_ready_timeout_s: float
    bridge_send_queue_bytes: int

    # -- Media --------------------------------------------------------------
    video_format: VideoFormat
    publish_audio_format: AudioFormat
    inbound_queue_size: int
    video_queue_size: int
    audio_queue_size: int
    echo_gate_hangover_ms: int
    idle_clip_path: Path | None

    # -- Recovery and monitoring -------------------------------------------
    rejoin_max_attempts: int
    watchdog_interval_s: float

    # -- Avatar agent -------------------------------------------------------
    avatar_url: str
    avatar_connect_timeout_s: float
    avatar_send_queue_size: int
    avatar_reconnect_initial_delay_s: float
    avatar_reconnect_max_delay_s: float
    avatar_reconnect_max_attempts: int

    def __post_init__(self) -> None:
        pixels = self.video_format.width * self.video_format.height
        if pixels > MAX_PUBLISH_PIXELS:
            raise MeetConfigurationError(
                f"google_meet video {self.video_format} is {pixels} pixels, above the "
                f"{MAX_PUBLISH_PIXELS}-pixel ceiling; every frame crosses the page "
                "bridge as raw I420, and Meet downscales it anyway"
            )
        rate = self.publish_audio_format.sample_rate_hz
        if rate not in SUPPORTED_PUBLISH_SAMPLE_RATES:
            supported = ", ".join(str(r) for r in sorted(SUPPORTED_PUBLISH_SAMPLE_RATES))
            raise MeetConfigurationError(
                f"google_meet cannot publish {rate} Hz audio; supported: {supported}. "
                "Web Audio would resample it silently rather than fail, which is worse"
            )
        if self.publish_audio_format.channels != 1:
            raise MeetConfigurationError(
                "the synthetic microphone track is mono; got "
                f"{self.publish_audio_format.channels} channels"
            )

    @property
    def ingest_audio_format(self) -> AudioFormat:
        """What the page sends up, which *is* the avatar's fixed input format.

        Zoom gets this free because RTMS is natively ``L16 / 16 kHz / mono``, and Teams
        gets it because the media platform is configured for ``Pcm16K``. Chromium gets
        it because the capture ``AudioContext`` is constructed with
        ``{sampleRate: 16000}``, so Web Audio resamples the 48 kHz conference audio down
        inside the browser's own graph and the worklet's render quantum is already at
        the target rate.

        That is worth stating plainly: **no resampler exists in this repository**, on any
        of the three connectors, and this one avoids needing one by choosing the graph's
        rate rather than by luck. ``audio_capture/mapping.py`` asserts the equality at
        the boundary rather than trusting it, so a page that ever sends 48 kHz fails
        loudly instead of feeding the avatar audio it cannot use.
        """
        return AVATAR_INPUT_FORMAT

    def is_configured(self) -> bool:
        """True when the connector has what it needs to join a meeting.

        Only the profile directory is required. Chromium's path can be discovered by
        Playwright, and the Google credentials are optional because an
        already-authenticated profile is the supported deployment.
        """
        return self.profile_dir is not None

    def require_configured(self) -> Path:
        """Return the profile directory, or fail fast.

        Returns the narrowed ``Path`` rather than ``None`` so that callers get a checked
        value out of the same call that validates it — which is what lets
        ``ChromiumBridge`` build its ``ProfileManager`` without a second guard.

        Raises:
            MeetConfigurationError: no persistent profile directory is configured.
        """
        if self.profile_dir is not None:
            return self.profile_dir
        raise MeetConfigurationError(
            "google meet connector is not configured: set "
            "MC_GOOGLE_MEET__PROFILE_DIR to a persistent Chromium profile that is "
            "signed in to the avatar's Google account"
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> GoogleMeetConnectorConfig:
        meet = settings.google_meet
        return cls(
            profile_dir=meet.profile_dir,
            chromium_executable=meet.chromium_executable,
            headless=meet.headless,
            browser_launch_timeout_s=meet.browser_launch_timeout_s,
            extra_browser_args=tuple(meet.extra_browser_args),
            google_email=meet.google_email,
            google_password=meet.google_password,
            display_name=meet.display_name,
            join_timeout_s=meet.join_timeout_s,
            lobby_timeout_s=meet.lobby_timeout_s,
            bridge_host=meet.bridge_host,
            bridge_port=meet.bridge_port,
            bridge_ready_timeout_s=meet.bridge_ready_timeout_s,
            bridge_send_queue_bytes=meet.bridge_send_queue_bytes,
            video_format=VideoFormat(
                width=meet.video_width, height=meet.video_height, fps=meet.video_fps
            ),
            publish_audio_format=AudioFormat(
                sample_rate_hz=meet.publish_sample_rate_hz,
                channels=1,
                sample_format=SampleFormat.S16LE,
            ),
            # Queue depths and echo timing are pipeline properties rather than platform
            # ones, so they come from the shared media settings — the same choice both
            # other connectors make.
            inbound_queue_size=settings.media.inbound_queue_size,
            video_queue_size=settings.media.video_queue_size,
            audio_queue_size=settings.media.audio_queue_size,
            echo_gate_hangover_ms=settings.media.echo_gate_hangover_ms,
            idle_clip_path=settings.media.idle_clip_path,
            rejoin_max_attempts=meet.rejoin_max_attempts,
            watchdog_interval_s=meet.watchdog_interval_s,
            avatar_url=settings.avatar.url,
            avatar_connect_timeout_s=settings.avatar.connect_timeout_s,
            avatar_send_queue_size=settings.avatar.send_queue_size,
            avatar_reconnect_initial_delay_s=settings.avatar.reconnect_initial_delay_s,
            avatar_reconnect_max_delay_s=settings.avatar.reconnect_max_delay_s,
            avatar_reconnect_max_attempts=settings.avatar.reconnect_max_attempts,
        )
