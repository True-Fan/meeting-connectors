"""Zoom-web connector configuration.

A flattened, connector-local view of ``Settings``, like the other connectors have:
this package depends on the fields it needs rather than the whole settings tree, so
an unrelated setting cannot change it and a test can build one in a line.

**It used to draw from two settings groups.** The connector once had two ingest legs —
a browser tap and Zoom's RTMS API — so it read RTMS credentials out of the ``zoom``
group belonging to the Meeting-SDK connector. That connector and that leg are both
gone: the meeting is heard and observed through the page, which is what makes this
connector work on an ordinary Zoom account rather than only on one with RTMS enabled
for the app. So everything here now comes from ``zoom_web``, ``media`` and ``avatar``,
and there is no cross-connector settings read left to explain.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from src.config.settings import Settings
from src.domain.media import AudioFormat, VideoFormat


@dataclass(frozen=True, slots=True)
class ZoomWebConnectorConfig:
    """Everything the Zoom-web connector needs, and nothing else."""

    # Joining, in the browser
    display_name: str
    join_timeout_s: float
    join_poll_interval_s: float
    headless: bool
    no_sandbox: bool
    profile_dir: Path | None

    # Publishing, through the page's synthetic microphone
    publish_audio_format: AudioFormat
    audio_queue_size: int

    # Ingest — the page tap, and how it behaves
    capture_frame_ms: int
    observe_interval_ms: int
    speaker_min_ms: int
    captions_enabled: bool
    captions_auto_enable: bool
    chat_open_panel: bool
    caption_settle_ms: int
    panel_ready_timeout_ms: int
    speech_interrupt_threshold: int
    roster_leave_grace_s: float
    inbound_queue_size: int

    # Meeting awareness — who is here, who is talking, what was said, and who wants the
    # floor. Every one of these is read off the page.
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
    def from_settings(cls, settings: Settings) -> ZoomWebConnectorConfig:
        zoom_web = settings.zoom_web
        return cls(
            capture_frame_ms=zoom_web.capture_frame_ms,
            observe_interval_ms=zoom_web.observe_interval_ms,
            speaker_min_ms=zoom_web.speaker_min_ms,
            # Folded against the transcript consumer, because opening a panel nobody reads
            # is a visible action in somebody else's meeting taken for no purpose at all.
            captions_enabled=(zoom_web.captions_enabled and zoom_web.transcript_enabled),
            captions_auto_enable=(
                zoom_web.captions_auto_enable
                and zoom_web.captions_enabled
                and zoom_web.transcript_enabled
            ),
            chat_open_panel=zoom_web.chat_open_panel,
            caption_settle_ms=zoom_web.caption_settle_ms,
            panel_ready_timeout_ms=zoom_web.panel_ready_timeout_ms,
            speech_interrupt_threshold=zoom_web.speech_interrupt_threshold,
            roster_leave_grace_s=zoom_web.roster_leave_grace_s,
            display_name=zoom_web.display_name,
            join_timeout_s=zoom_web.join_timeout_s,
            join_poll_interval_s=zoom_web.join_poll_interval_s,
            headless=zoom_web.headless,
            no_sandbox=zoom_web.no_sandbox,
            profile_dir=zoom_web.profile_dir,
            publish_audio_format=settings.media.publish_audio_format(),
            audio_queue_size=settings.media.audio_queue_size,
            inbound_queue_size=settings.media.inbound_queue_size,
            chat_enabled=zoom_web.chat_enabled,
            chat_require_mention=zoom_web.chat_require_mention,
            chat_mention_names=tuple(zoom_web.chat_mention_names),
            transcript_enabled=zoom_web.transcript_enabled,
            attendance_enabled=zoom_web.attendance_enabled,
            speaker_tracking_enabled=zoom_web.speaker_tracking_enabled,
            speaker_hold_ms=zoom_web.speaker_hold_ms,
            speaker_merge_gap_ms=zoom_web.speaker_merge_gap_ms,
            context_push_enabled=zoom_web.context_push_enabled,
            context_push_interval_s=zoom_web.context_push_interval_s,
            context_push_require_negotiation=zoom_web.context_push_require_negotiation,
            voice_interrupt_enabled=zoom_web.voice_interrupt_enabled,
            hand_raise_enabled=zoom_web.hand_raise_enabled,
            hand_raise_open_panel=zoom_web.hand_raise_open_panel,
            hand_raise_prompt=zoom_web.hand_raise_prompt,
            hand_raise_cooldown_s=zoom_web.hand_raise_cooldown_s,
            hand_raise_mute_ms=zoom_web.hand_raise_mute_ms,
            video_format=settings.media.video_format(),
            video_queue_size=settings.media.video_queue_size,
            # The connector's own value wins where it has one. Zoom's loop-back is far
            # slower than the shared 200 ms default assumes, and this connector can
            # afford a long gate because its barge-in does not read inbound audio.
            echo_gate_hangover_ms=(
                zoom_web.echo_gate_hangover_ms
                if zoom_web.echo_gate_hangover_ms is not None
                else settings.media.echo_gate_hangover_ms
            ),
            avatar_url=settings.avatar.url,
            avatar_connect_timeout_s=settings.avatar.connect_timeout_s,
            avatar_send_queue_size=settings.avatar.send_queue_size,
            avatar_reconnect_initial_delay_s=settings.avatar.reconnect_initial_delay_s,
            avatar_reconnect_max_delay_s=settings.avatar.reconnect_max_delay_s,
            avatar_reconnect_max_attempts=settings.avatar.reconnect_max_attempts,
        )
