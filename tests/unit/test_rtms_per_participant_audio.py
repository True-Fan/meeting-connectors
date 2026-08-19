"""Audio arriving on a per-participant (``AUDIO_MULTI_STREAMS``) subscription.

**A real outage, reproduced.** ``MediaDataAudio.content`` was declared ``str``, which
is only the shape a *mixed* stream sends. This connector negotiates
``AUDIO_MULTI_STREAMS`` by default, and Zoom answers that with an object —
``{"data": "<base64>", "user_id": ..., "user_name": ...}`` — so **every** audio frame
failed validation.

The damage was not one dropped frame. ``ValidationError`` is a ``ValueError``, not an
``RtmsProtocolError``, and the ``model_validate`` call sat outside the handler that
catches malformed audio — so the error escaped ``_enqueue_audio``, unwound the media
pump's task group, and killed the RTMS connection outright. From the outside it
looked like a meeting nobody spoke in: ``rtms.attached`` and then silence forever, on
a stream that had been carrying the caller's voice the whole time.

The envelope also carries the speaker, which is the reason to subscribe
per-participant at all, so it must survive into the frame rather than being discarded
with the shape it arrived in.
"""

from __future__ import annotations

import base64

import pytest

from src.connectors.zoom.exceptions import RtmsProtocolError
from src.connectors.zoom.rtms.enums import RtmsMessageType
from src.connectors.zoom.rtms.mapping import to_audio_frame
from src.connectors.zoom.rtms.models import MediaDataAudio
from src.domain.context import FrameContext
from src.domain.ids import CorrelationId, SessionId
from src.domain.media import AudioFormat, SampleFormat
from src.services.media.clock import MediaClock

PCM = bytes(640)
ENCODED = base64.b64encode(PCM).decode()
FORMAT = AudioFormat(sample_rate_hz=16_000, channels=1, sample_format=SampleFormat.S16LE)


def _frame(message: MediaDataAudio):
    ctx = FrameContext(
        session_id=SessionId("ses_rtms0000000000000000000000000"),
        correlation_id=CorrelationId("cor_rtms0000000000000000000000000"),
    )
    return to_audio_frame(message, audio_format=FORMAT, ctx=ctx, clock=MediaClock())


def test_the_payload_zoom_actually_sent_validates() -> None:
    """Verbatim from the failing session, names and all."""
    wire = MediaDataAudio.model_validate(
        {
            "msg_type": RtmsMessageType.MEDIA_DATA_AUDIO,
            "content": {
                "data": ENCODED,
                "user_id": 16778240,
                "user_name": "Dev Choudhary",
            },
        }
    )

    assert wire.audio_base64() == ENCODED
    assert wire.speaker() == (16778240, "Dev Choudhary")


def test_per_participant_audio_becomes_a_frame_with_its_speaker() -> None:
    wire = MediaDataAudio.model_validate(
        {
            "msg_type": RtmsMessageType.MEDIA_DATA_AUDIO,
            "content": {"data": ENCODED, "user_id": 42, "user_name": "Ada"},
        }
    )

    frame = _frame(wire)

    assert frame.pcm == PCM
    assert frame.participant is not None
    assert frame.participant.display_name == "Ada"


def test_a_bare_string_content_still_works() -> None:
    """The mixed-stream shape must keep working."""
    wire = MediaDataAudio.model_validate(
        {
            "msg_type": RtmsMessageType.MEDIA_DATA_AUDIO,
            "content": ENCODED,
            "user_id": 7,
            "user_name": "Grace",
        }
    )

    frame = _frame(wire)

    assert frame.pcm == PCM
    assert frame.participant is not None
    assert frame.participant.user_id == 7


def test_top_level_attribution_is_used_when_the_envelope_has_none() -> None:
    """An envelope carrying only audio must not erase the outer speaker fields."""
    wire = MediaDataAudio.model_validate(
        {
            "msg_type": RtmsMessageType.MEDIA_DATA_AUDIO,
            "content": {"data": ENCODED},
            "user_id": 9,
            "user_name": "Alan",
        }
    )

    assert wire.speaker() == (9, "Alan")


def test_an_empty_envelope_is_a_protocol_error_not_an_empty_frame() -> None:
    """Zero bytes divides evenly by the frame size, so the length check passes it."""
    wire = MediaDataAudio.model_validate(
        {"msg_type": RtmsMessageType.MEDIA_DATA_AUDIO, "content": {"data": ""}}
    )

    with pytest.raises(RtmsProtocolError, match="empty"):
        _frame(wire)


def test_undecodable_audio_is_still_a_protocol_error() -> None:
    wire = MediaDataAudio.model_validate(
        {
            "msg_type": RtmsMessageType.MEDIA_DATA_AUDIO,
            "content": {"data": "not!valid!base64"},
        }
    )

    with pytest.raises(RtmsProtocolError):
        _frame(wire)


def test_an_unknown_extra_field_does_not_break_validation() -> None:
    """Zoom adds fields; a live session must not die of a new one."""
    wire = MediaDataAudio.model_validate(
        {
            "msg_type": RtmsMessageType.MEDIA_DATA_AUDIO,
            "content": {"data": ENCODED, "user_id": 1, "something_new": True},
            "another_new_field": "x",
        }
    )

    assert wire.audio_base64() == ENCODED
