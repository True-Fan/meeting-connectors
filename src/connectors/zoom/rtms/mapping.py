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
    RtmsEventType,
)
from src.connectors.zoom.rtms.models import (
    AudioMediaParams,
    EventUpdate,
    MediaDataAudio,
    MediaDataText,
    RtmsStartedEvent,
)
from src.connectors.zoom.rtms.observations import (
    ParticipantEvent,
    SpeakerEvent,
    TranscriptLine,
)
from src.domain.avatar import AVATAR_INPUT_FORMAT
from src.domain.context import FrameContext
from src.domain.media import AudioFormat, AudioFrame
from src.domain.meeting import ChatMessage, MeetingContext, ParticipantRef
from src.services.media.clock import MediaClock

MAX_TEXT_CHARS = 2_000
"""Longest transcript or chat line kept, in characters.

A cap rather than unbounded because both end up in an LLM's context window: a pasted wall
of text costs tokens, delays the reply, and is far more likely to be spam than a question.
Truncated rather than dropped, so a long but genuine question still gets an answer — the
same call ``connectors/google_meet/meeting/chat.py`` makes, at the same size."""

MAX_NAME_CHARS = 120


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
        pcm = base64.b64decode(message.audio_base64(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RtmsProtocolError("audio content is not valid base64") from exc

    if not pcm:
        # Zero bytes is not silence, it is a frame with no audio in it — reachable
        # when the per-participant envelope arrives without its ``data``. The length
        # check below accepts it, because zero divides evenly.
        raise RtmsProtocolError("audio content is empty")

    if len(pcm) % audio_format.bytes_per_frame:
        raise RtmsProtocolError(
            f"audio payload {len(pcm)} bytes is not a whole number of samples "
            f"for {audio_format}"
        )

    user_id, user_name = message.speaker()
    participant = (
        ParticipantRef(user_id=user_id, display_name=user_name)
        if user_id is not None
        else None
    )

    return AudioFrame(
        pcm=pcm,
        pts_us=clock.now_us(),
        format=audio_format,
        ctx=ctx,
        participant=participant,
    )


def _clean_name(value: str | None) -> str | None:
    """Collapse whitespace, bound the length, and turn empty into ``None``.

    Zoom's display names arrive as the participant typed them, so they carry stray
    whitespace but none of the status text Meet appends inside the same label — which is
    why this is four lines where ``participants._clean`` is forty. The connector that has
    to reconstruct a name from a DOM needs that machinery; one that is handed the name by
    an API does not, and pretending otherwise would be cargo cult.
    """
    cleaned = " ".join(str(value or "").split())[:MAX_NAME_CHARS].strip()
    return cleaned or None


def _clean_text(value: str) -> str:
    """Collapse whitespace and bound the length of a transcript or chat line."""
    return " ".join(str(value or "").split())[:MAX_TEXT_CHARS].strip()


def to_transcript_line(
    message: MediaDataText, *, clock: MediaClock
) -> TranscriptLine | None:
    """Translate ``msg_type 17`` into an attributed line, or ``None`` if it says nothing.

    ``None`` rather than an exception for an empty line: Zoom emits interim fragments and
    keepalive-shaped blanks on the transcript stream, and neither is malformed. Raising on
    them would turn the normal case into a logged error on every silence.
    """
    text = _clean_text(message.text())
    if not text:
        return None
    user_id, user_name = message.speaker()
    return TranscriptLine(
        user_id=user_id,
        display_name=_clean_name(user_name),
        text=text,
        at_us=clock.now_us(),
    )


def to_chat_message(message: MediaDataText, *, clock: MediaClock) -> ChatMessage | None:
    """Translate ``msg_type 18`` into a domain ``ChatMessage``, or ``None`` if empty.

    ``is_self`` is deliberately **not** decided here. This module knows what Zoom said and
    nothing about which participant the avatar is — that is the session's knowledge, and
    the filter that uses it lives beside the policy it serves
    (``connectors/zoom_web/meeting/chat.py``). Guessing here would put a self-check in the
    one place that cannot be told it is wrong.
    """
    text = _clean_text(message.text())
    if not text:
        return None
    _, user_name = message.speaker()
    return ChatMessage(
        text=text, sender=_clean_name(user_name), received_at_us=clock.now_us()
    )


def to_participant_events(
    event: EventUpdate, *, clock: MediaClock
) -> tuple[ParticipantEvent, ...]:
    """Translate a join or leave ``EVENT_UPDATE`` into one event per person.

    **Plural, because Zoom's is.** ``PARTICIPANT_JOIN`` carries a ``participants`` array,
    and the first one after attaching lists the whole roster — which is the only way this
    connector learns about people who were already in the meeting when RTMS attached.

    Empty for any other event type, and for an event with nobody on it: an anonymous join
    is an event with no subject, and inventing one would put a phantom in the attendance
    answer.
    """
    event_type = event.resolved_event_type()
    if event_type not in (
        RtmsEventType.PARTICIPANT_JOIN,
        RtmsEventType.PARTICIPANT_LEAVE,
    ):
        return ()
    joined = event_type == RtmsEventType.PARTICIPANT_JOIN
    at_us = clock.now_us()
    return tuple(
        ParticipantEvent(
            user_id=user_id,
            display_name=_clean_name(user_name),
            joined=joined,
            at_us=at_us,
        )
        for user_id, user_name in event.participants()
        if user_id is not None or user_name
    )


def to_speaker_event(event: EventUpdate, *, clock: MediaClock) -> SpeakerEvent | None:
    """Translate ``ACTIVE_SPEAKER_CHANGE``, or ``None`` for any other event type.

    Kept even when only an id arrives. The name is resolved against the roster by whoever
    holds one, retroactively if it has to be — dropping the event instead would lose the
    *timing*, which is the half of this that nothing else can supply.
    """
    if event.resolved_event_type() != RtmsEventType.ACTIVE_SPEAKER_CHANGE:
        return None
    user_id, user_name = event.participant()
    if user_id is None and not user_name:
        return None
    return SpeakerEvent(
        user_id=user_id, display_name=_clean_name(user_name), at_us=clock.now_us()
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
