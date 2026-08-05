"""The anti-corruption boundary.

Everything to the left of this module speaks RTMS. Everything to the right speaks
``src.domain``. This is the only place both vocabularies exist, and it is the reason
the router, decoder, pacer and publisher can be tested without RTMS at all.

``tests/architecture/test_layering.py`` asserts that ``rtms/models.py`` is imported
nowhere outside ``connectors/zoom/rtms/`` — so this translation cannot be bypassed.
"""

from __future__ import annotations

import base64
import binascii

from src.connectors.zoom.exceptions import RtmsProtocolError
from src.connectors.zoom.rtms.enums import (
    AudioChannel,
    AudioCodec,
    AudioSampleRate,
    MediaContentType,
    MediaDataOption,
)
from src.connectors.zoom.rtms.models import (
    AudioMediaParams,
    MediaDataAudio,
    RtmsStartedEvent,
)
from src.domain.avatar import AVATAR_INPUT_FORMAT
from src.domain.context import FrameContext
from src.domain.media import AudioFormat, AudioFrame
from src.domain.meeting import MeetingContext, ParticipantRef
from src.services.media.clock import MediaClock


def build_audio_params(*, send_rate_ms: int, per_participant: bool) -> AudioMediaParams:
    """Build the audio subscription from the fixed avatar contract.

    The format is *derived from* ``AVATAR_INPUT_FORMAT`` rather than hardcoded. That
    makes the zero-resample property structural: if the avatar contract ever changed,
    this would fail loudly at startup instead of silently inserting a resampler in
    the hot path (doc 003 §3.3).

    Raises:
        RtmsProtocolError: RTMS cannot deliver the avatar's required format.
    """
    try:
        sample_rate = AudioSampleRate.from_hz(AVATAR_INPUT_FORMAT.sample_rate_hz)
    except ValueError as exc:
        raise RtmsProtocolError(str(exc)) from exc

    if AVATAR_INPUT_FORMAT.channels == 1:
        channel = AudioChannel.MONO
    elif AVATAR_INPUT_FORMAT.channels == 2:
        channel = AudioChannel.STEREO
    else:
        raise RtmsProtocolError(
            f"RTMS offers mono or stereo only, avatar wants {AVATAR_INPUT_FORMAT.channels}"
        )

    return AudioMediaParams(
        content_type=MediaContentType.RAW_AUDIO,
        sample_rate=sample_rate,
        channel=channel,
        codec=AudioCodec.L16,
        data_opt=(
            MediaDataOption.AUDIO_MULTI_STREAMS
            if per_participant
            else MediaDataOption.AUDIO_MIXED_STREAM
        ),
        send_rate=send_rate_ms,
    )


def negotiated_audio_format(params: AudioMediaParams) -> AudioFormat:
    """The domain format implied by a subscription we sent."""
    return AudioFormat(
        sample_rate_hz=AudioSampleRate(params.sample_rate).hz,
        channels=AudioChannel(params.channel).value,
        sample_format=AVATAR_INPUT_FORMAT.sample_format,
    )


def to_audio_frame(
    message: MediaDataAudio,
    *,
    audio_format: AudioFormat,
    ctx: FrameContext,
    clock: MediaClock,
) -> AudioFrame:
    """Translate ``msg_type 14`` into a canonical ``AudioFrame``.

    The PTS is taken from **our** media clock rather than Zoom's ``timestamp`` field.
    Zoom's timestamp is on an unrelated timeline; mixing the two would corrupt the
    single-clock invariant that A/V sync depends on (doc 003 §5.2). Zoom's value is
    still useful for latency attribution, but never as a presentation timestamp.

    Raises:
        RtmsProtocolError: the payload is not decodable base64, or its length is not
            a whole number of samples for the negotiated format.
    """
    try:
        pcm = base64.b64decode(message.content, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RtmsProtocolError("audio content is not valid base64") from exc

    if len(pcm) % audio_format.bytes_per_frame:
        raise RtmsProtocolError(
            f"audio payload {len(pcm)} bytes is not a whole number of samples "
            f"for {audio_format}"
        )

    participant = (
        ParticipantRef(user_id=message.user_id, display_name=message.user_name)
        if message.user_id is not None
        else None
    )

    return AudioFrame(
        pcm=pcm,
        pts_us=clock.now_us(),
        format=audio_format,
        ctx=ctx,
        participant=participant,
    )


def to_meeting_context(
    event: RtmsStartedEvent,
    *,
    display_name: str,
    meeting_number: str | None = None,
) -> MeetingContext:
    """Build a ``MeetingContext`` from a ``meeting.rtms_started`` webhook.

    RTMS-specific fields go into ``platform_data`` as opaque values. Only this
    package may read them back, via ``rtms_attachment``.
    """
    payload = event.payload
    return MeetingContext(
        meeting_number=meeting_number or "",
        display_name=display_name,
        meeting_uuid=payload.meeting_uuid,
        platform_data={
            "rtms_stream_id": payload.rtms_stream_id,
            "signaling_url": payload.signaling_url(),
        },
    )


def rtms_attachment(meeting: MeetingContext) -> tuple[str, str, str]:
    """Read back the RTMS attachment details this package stored.

    Returns:
        ``(meeting_uuid, rtms_stream_id, signaling_url)``.

    Raises:
        RtmsProtocolError: the context carries no RTMS attachment, i.e. no
            ``rtms_started`` webhook has been bound to this session yet.
    """
    stream_id = meeting.platform_data.get("rtms_stream_id")
    signaling_url = meeting.platform_data.get("signaling_url")
    if not meeting.meeting_uuid or not stream_id or not signaling_url:
        raise RtmsProtocolError("meeting context has no RTMS attachment bound yet")
    return meeting.meeting_uuid, str(stream_id), str(signaling_url)
