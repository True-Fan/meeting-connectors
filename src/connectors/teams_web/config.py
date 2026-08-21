"""Teams-web connector configuration.

A flattened, connector-local view of ``Settings``, like the other connectors have: this
package depends on the fields it needs rather than on the whole settings tree, so an
unrelated setting cannot change it and a test can build one in a line.

**It draws from ``teams_web``, ``media`` and ``avatar`` — and deliberately not from
``teams``.** The Graph connector's settings are a tenant id, a client secret and the
coordinates of a Windows host, none of which this connector can use or should imply it
needs. Reading them here would make "Teams is configured" ambiguous between two connectors
whose whole difference is what they require.

**Consumers are folded against the observers that feed them here rather than in the page**,
which is the rule ``ZoomWebConnectorConfig.from_settings`` follows for the same reason: an
observer whose ledger is switched off would be scanning a DOM on the thread that encodes the
avatar's audio to produce events nothing reads.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from src.config.settings import Settings
from src.domain.media import AudioFormat, VideoFormat


@dataclass(frozen=True, slots=True)
class TeamsWebConnectorConfig:
    """Everything the Teams-web connector needs, and nothing else."""

    # Joining, in the browser
    display_name: str
    join_url_template: str
    live_url_template: str
    bypass_csp: bool
    force_web_client: bool
    join_timeout_s: float
    join_poll_interval_s: float
    headless: bool
    no_sandbox: bool
    profile_dir: Path | None

    # Publishing, through the page's synthetic microphone
    publish_audio_format: AudioFormat
    audio_queue_size: int

    # Ingest — the page's audio tap, and how the page-side observers behave
    capture_frame_ms: int
    inbound_queue_size: int
    observe_interval_ms: int
    speaker_min_ms: int
    captions_enabled: bool
    captions_auto_enable: bool
    chat_open_panel: bool
    caption_settle_ms: int
    panel_ready_timeout_ms: int
    speech_interrupt_threshold: int
    roster_leave_grace_s: float

    # Meeting awareness — who is here, who is talking, what was said, and who wants the
    # floor. Every one of these is read off the page; there is no API half on this
    # connector, which is the whole reason it exists.
    chat_enabled: bool
    chat_require_mention: bool
    chat_mention_names: tuple[str, ...]
    transcript_enabled: bool
    attendance_enabled: bool
    speaker_tracking_enabled: bool
    speaker_hold_ms: int
    speaker_merge_gap_ms: int
    context_push_enabled: bool
    context_push_interval_s: float
    context_push_require_negotiation: bool
    voice_interrupt_enabled: bool
    hand_raise_enabled: bool
    hand_raise_open_panel: bool
    hand_raise_prompt: str
    hand_raise_cooldown_s: float
    hand_raise_mute_ms: int

    # Media pipeline
    video_format: VideoFormat
    video_queue_size: int
    echo_gate_hangover_ms: int

    # Avatar agent
    avatar_url: str
    avatar_connect_timeout_s: float
    avatar_send_queue_size: int
    avatar_reconnect_initial_delay_s: float
    avatar_reconnect_max_delay_s: float
    avatar_reconnect_max_attempts: int

    @classmethod
    def from_settings(cls, settings: Settings) -> TeamsWebConnectorConfig:
        teams_web = settings.teams_web
        return cls(
            display_name=teams_web.display_name,
            join_url_template=teams_web.join_url_template,
            live_url_template=teams_web.live_url_template,
            bypass_csp=teams_web.bypass_csp,
            force_web_client=teams_web.force_web_client,
            join_timeout_s=teams_web.join_timeout_s,
            join_poll_interval_s=teams_web.join_poll_interval_s,
            headless=teams_web.headless,
            no_sandbox=teams_web.no_sandbox,
            profile_dir=teams_web.profile_dir,
            publish_audio_format=settings.media.publish_audio_format(),
            audio_queue_size=settings.media.audio_queue_size,
            capture_frame_ms=teams_web.capture_frame_ms,
            inbound_queue_size=settings.media.inbound_queue_size,
            observe_interval_ms=teams_web.observe_interval_ms,
            speaker_min_ms=teams_web.speaker_min_ms,
            # **Folded against the transcript, which is the only consumer of a caption.**
            # Reading a panel nobody records is renderer time spent on the thread that
            # encodes the avatar's audio; *enabling* one is a visible action in somebody
            # else's meeting taken for no purpose at all. The same fold
            # ``ZoomWebConnectorConfig`` applies, and the second half is why it matters more
            # than tidiness.
            captions_enabled=teams_web.captions_enabled and teams_web.transcript_enabled,
            captions_auto_enable=(
                teams_web.captions_auto_enable
                and teams_web.captions_enabled
                and teams_web.transcript_enabled
            ),
            chat_open_panel=teams_web.chat_open_panel,
            caption_settle_ms=teams_web.caption_settle_ms,
            panel_ready_timeout_ms=teams_web.panel_ready_timeout_ms,
            speech_interrupt_threshold=teams_web.speech_interrupt_threshold,
            roster_leave_grace_s=teams_web.roster_leave_grace_s,
            chat_enabled=teams_web.chat_enabled,
            chat_require_mention=teams_web.chat_require_mention,
            chat_mention_names=tuple(teams_web.chat_mention_names),
            transcript_enabled=teams_web.transcript_enabled,
            attendance_enabled=teams_web.attendance_enabled,
            speaker_tracking_enabled=teams_web.speaker_tracking_enabled,
            speaker_hold_ms=teams_web.speaker_hold_ms,
            speaker_merge_gap_ms=teams_web.speaker_merge_gap_ms,
            context_push_enabled=teams_web.context_push_enabled,
            context_push_interval_s=teams_web.context_push_interval_s,
            context_push_require_negotiation=teams_web.context_push_require_negotiation,
            voice_interrupt_enabled=teams_web.voice_interrupt_enabled,
            hand_raise_enabled=teams_web.hand_raise_enabled,
            hand_raise_open_panel=teams_web.hand_raise_open_panel,
            hand_raise_prompt=teams_web.hand_raise_prompt,
            hand_raise_cooldown_s=teams_web.hand_raise_cooldown_s,
            hand_raise_mute_ms=teams_web.hand_raise_mute_ms,
            video_format=settings.media.video_format(),
            video_queue_size=settings.media.video_queue_size,
            # The connector's own value wins where it has one, and today nothing reads it:
            # ``TeamsWebSessionFactory`` builds ``EchoGuard`` with ``gate_enabled=False``,
            # because the avatar's voice is structurally absent from the tapped audio and a
            # shut gate would suppress a barge-in along with the echo it cannot distinguish
            # it from. Carried anyway so the field is present the day an ingest leg needs it.
            echo_gate_hangover_ms=(
                teams_web.echo_gate_hangover_ms
                if teams_web.echo_gate_hangover_ms is not None
                else settings.media.echo_gate_hangover_ms
            ),
            avatar_url=settings.avatar.url,
            avatar_connect_timeout_s=settings.avatar.connect_timeout_s,
            avatar_send_queue_size=settings.avatar.send_queue_size,
            avatar_reconnect_initial_delay_s=settings.avatar.reconnect_initial_delay_s,
            avatar_reconnect_max_delay_s=settings.avatar.reconnect_max_delay_s,
            avatar_reconnect_max_attempts=settings.avatar.reconnect_max_attempts,
        )
