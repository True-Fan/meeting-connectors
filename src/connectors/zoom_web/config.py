"""Zoom-web connector configuration.

A flattened, connector-local view of ``Settings``, like the other connectors have:
this package depends on the fields it needs rather than the whole settings tree, so
an unrelated setting cannot change it and a test can build one in a line.

It draws from **both** ``zoom_web`` (browser and microphone) and ``zoom`` (RTMS
credentials), because that is what this connector is: a browser publishing half and
an RTMS ingest half. Reading the RTMS credentials from ``zoom`` rather than copying
them means one place to configure Zoom, whichever connector runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydantic import SecretStr

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

    # Ingest, over RTMS
    client_id: str
    client_secret: SecretStr
    rtms_auto_start: bool
    account_id: str
    s2s_client_id: str
    s2s_client_secret: SecretStr
    api_base_url: str
    oauth_base_url: str
    api_timeout_s: float
    rtms_send_rate_ms: int
    per_participant_audio: bool
    inbound_queue_size: int
    rtms_transcript_enabled: bool
    rtms_chat_enabled: bool
    rtms_events_enabled: bool

    # Meeting awareness — who is here, who is talking, what was said, and who wants the
    # floor. Every one of these is served by RTMS except ``hand_raise_*``, which is read
    # from the page because Zoom raises no hand-raise event.
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
            display_name=zoom_web.display_name,
            join_timeout_s=zoom_web.join_timeout_s,
            join_poll_interval_s=zoom_web.join_poll_interval_s,
            headless=zoom_web.headless,
            no_sandbox=zoom_web.no_sandbox,
            profile_dir=zoom_web.profile_dir,
            publish_audio_format=settings.media.publish_audio_format(),
            audio_queue_size=settings.media.audio_queue_size,
            client_id=settings.zoom.client_id,
            client_secret=settings.zoom.client_secret,
            rtms_auto_start=settings.zoom.is_rtms_auto_start_configured(),
            account_id=settings.zoom.account_id,
            s2s_client_id=settings.zoom.s2s_client_id,
            s2s_client_secret=settings.zoom.s2s_client_secret,
            api_base_url=settings.zoom.api_base_url,
            oauth_base_url=settings.zoom.oauth_base_url,
            api_timeout_s=settings.zoom.api_timeout_s,
            rtms_send_rate_ms=settings.zoom.rtms_send_rate_ms,
            per_participant_audio=zoom_web.per_participant_audio,
            inbound_queue_size=settings.media.inbound_queue_size,
            # **Subscribed only when something will consume the result**, which is why each
            # of these is an ``and`` rather than a straight read. Asking Zoom for a stream
            # nobody reads is not free: it is traffic on the socket that carries the
            # meeting's audio, and — because the text streams are the ones an account may
            # refuse — it is also an extra way for the data handshake to be rejected. A
            # deployment that has turned every consumer off should send the handshake it
            # sent before any of this existed.
            rtms_transcript_enabled=(
                zoom_web.rtms_transcript_enabled and zoom_web.transcript_enabled
            ),
            rtms_chat_enabled=(
                zoom_web.rtms_chat_enabled
                and (zoom_web.chat_enabled or zoom_web.transcript_enabled)
            ),
            rtms_events_enabled=(
                zoom_web.rtms_events_enabled
                and (
                    zoom_web.attendance_enabled
                    or zoom_web.speaker_tracking_enabled
                    or zoom_web.voice_interrupt_enabled
                )
            ),
            chat_enabled=zoom_web.chat_enabled and zoom_web.rtms_chat_enabled,
            chat_require_mention=zoom_web.chat_require_mention,
            chat_mention_names=tuple(zoom_web.chat_mention_names),
            transcript_enabled=(
                zoom_web.transcript_enabled
                and (zoom_web.rtms_transcript_enabled or zoom_web.rtms_chat_enabled)
            ),
            attendance_enabled=zoom_web.attendance_enabled and zoom_web.rtms_events_enabled,
            speaker_tracking_enabled=(
                zoom_web.speaker_tracking_enabled and zoom_web.rtms_events_enabled
            ),
            speaker_hold_ms=zoom_web.speaker_hold_ms,
            speaker_merge_gap_ms=zoom_web.speaker_merge_gap_ms,
            context_push_enabled=zoom_web.context_push_enabled,
            context_push_interval_s=zoom_web.context_push_interval_s,
            context_push_require_negotiation=zoom_web.context_push_require_negotiation,
            voice_interrupt_enabled=(
                zoom_web.voice_interrupt_enabled and zoom_web.rtms_events_enabled
            ),
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
