"""RTMS wire models.

**These types must never leave ``connectors/zoom/rtms/``.** They are the shape Zoom
puts on the wire — ``msg_type``, ``rtms_stream_id``, base64 envelopes — and letting
them travel inward would make the whole pipeline speak RTMS. ``mapping.py``
translates them into ``src.domain`` models at the boundary, and
``tests/architecture/test_layering.py`` fails CI if this rule is broken.

Models are permissive on input (``extra="allow"``) because Zoom may add fields, and
a new field must not crash a live session.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from src.connectors.zoom.rtms.enums import (
    PROTOCOL_VERSION,
    AudioChannel,
    AudioCodec,
    AudioSampleRate,
    MediaContentType,
    MediaDataOption,
    MediaDataType,
    RtmsMessageType,
)

_WIRE_CONFIG = ConfigDict(extra="allow", populate_by_name=True)


# --------------------------------------------------------------------------- #
# Outbound: handshakes and control
# --------------------------------------------------------------------------- #


class SignalingHandshakeRequest(BaseModel):
    """``msg_type 1`` — sent on the signaling socket."""

    model_config = _WIRE_CONFIG

    msg_type: int = RtmsMessageType.SIGNALING_HAND_SHAKE_REQ
    protocol_version: int = PROTOCOL_VERSION
    meeting_uuid: str
    rtms_stream_id: str
    signature: str
    sequence: int = 0
    buffer_data: bool = False


class AudioMediaParams(BaseModel):
    """``media_params.audio`` in the data handshake."""

    model_config = _WIRE_CONFIG

    content_type: int = MediaContentType.RAW_AUDIO
    sample_rate: int = AudioSampleRate.SR_16K
    channel: int = AudioChannel.MONO
    codec: int = AudioCodec.L16
    data_opt: int = MediaDataOption.AUDIO_MULTI_STREAMS
    send_rate: int = 20


class MediaParams(BaseModel):
    """``media_params`` in the data handshake.

    Audio only. Video, screen share and chat are deliberately not subscribed —
    unrequested media is pure latency and bandwidth cost (doc 003 §3.2).
    """

    model_config = _WIRE_CONFIG

    audio: AudioMediaParams


class DataHandshakeRequest(BaseModel):
    """``msg_type 3`` — sent on the media socket."""

    model_config = _WIRE_CONFIG

    msg_type: int = RtmsMessageType.DATA_HAND_SHAKE_REQ
    protocol_version: int = PROTOCOL_VERSION
    meeting_uuid: str
    rtms_stream_id: str
    signature: str
    media_type: int = MediaDataType.AUDIO
    payload_encryption: bool = False
    media_params: MediaParams


class ClientReadyAck(BaseModel):
    """``msg_type 7`` — sent on the signaling socket once media is handshaken."""

    model_config = _WIRE_CONFIG

    msg_type: int = RtmsMessageType.CLIENT_READY_ACK
    rtms_stream_id: str


class KeepAliveResponse(BaseModel):
    """``msg_type 13`` — echoes the server's timestamp verbatim."""

    model_config = _WIRE_CONFIG

    msg_type: int = RtmsMessageType.KEEP_ALIVE_RESP
    timestamp: int


# --------------------------------------------------------------------------- #
# Inbound
# --------------------------------------------------------------------------- #


class MediaServerUrls(BaseModel):
    model_config = _WIRE_CONFIG

    all: str | None = None
    audio: str | None = None
    video: str | None = None
    transcript: str | None = None

    def resolve(self) -> str | None:
        """The URL to use for an audio-only subscription."""
        return self.all or self.audio


class MediaServer(BaseModel):
    model_config = _WIRE_CONFIG

    server_urls: MediaServerUrls = Field(default_factory=MediaServerUrls)


class SignalingHandshakeResponse(BaseModel):
    """``msg_type 2`` — carries the media socket URL."""

    model_config = _WIRE_CONFIG

    msg_type: int
    status_code: int = 0
    media_server: MediaServer | None = None
    reason: str | None = None

    def media_url(self) -> str | None:
        return self.media_server.server_urls.resolve() if self.media_server else None


class DataHandshakeResponse(BaseModel):
    """``msg_type 4``."""

    model_config = _WIRE_CONFIG

    msg_type: int
    status_code: int = 0
    reason: str | None = None


class KeepAliveRequest(BaseModel):
    """``msg_type 12`` — must be answered inside the server's window or Zoom
    drops the connection."""

    model_config = _WIRE_CONFIG

    msg_type: int
    timestamp: int = 0


class MediaDataAudio(BaseModel):
    """``msg_type 14`` — one audio frame.

    ``content`` is base64-encoded PCM. Decoding happens in ``mapping.py``, not here,
    so the wire model stays a faithful description of the wire.
    """

    model_config = _WIRE_CONFIG

    msg_type: int
    content: str
    user_id: int | None = None
    user_name: str | None = None
    timestamp: int | None = None


class EventUpdate(BaseModel):
    """``msg_type 6`` — participant and speaker events."""

    model_config = _WIRE_CONFIG

    msg_type: int
    event_type: int | None = None
    timestamp: int | None = None
    user_id: int | None = None
    user_name: str | None = None
    event: dict[str, Any] | None = None


class StreamStateUpdate(BaseModel):
    """``msg_type 8`` / ``9`` — stream or session state changed."""

    model_config = _WIRE_CONFIG

    msg_type: int
    state: int | None = None
    reason: str | None = None
    rtms_stream_id: str | None = None


# --------------------------------------------------------------------------- #
# Webhooks
# --------------------------------------------------------------------------- #


class UrlValidationPayload(BaseModel):
    model_config = _WIRE_CONFIG

    plain_token: str = Field(alias="plainToken")


class UrlValidationEvent(BaseModel):
    """``endpoint.url_validation`` — Zoom's endpoint challenge."""

    model_config = _WIRE_CONFIG

    event: str
    payload: UrlValidationPayload


class RtmsStartedPayload(BaseModel):
    model_config = _WIRE_CONFIG

    meeting_uuid: str
    rtms_stream_id: str
    server_urls: str | list[str] | dict[str, Any]
    operator_id: str | None = None

    def signaling_url(self) -> str:
        """Normalise ``server_urls``, which Zoom may send as a string, list or map."""
        raw = self.server_urls
        if isinstance(raw, str):
            return raw
        if isinstance(raw, list):
            if not raw:
                raise ValueError("server_urls list is empty")
            return str(raw[0])
        for key in ("all", "signaling", "signalling"):
            if raw.get(key):
                return str(raw[key])
        first = next((v for v in raw.values() if v), None)
        if first is None:
            raise ValueError(f"no usable url in server_urls: {raw!r}")
        return str(first)


class RtmsStoppedPayload(BaseModel):
    model_config = _WIRE_CONFIG

    meeting_uuid: str
    rtms_stream_id: str | None = None
    stop_reason: int | str | None = None


class RtmsStartedEvent(BaseModel):
    """``meeting.rtms_started``."""

    model_config = _WIRE_CONFIG

    event: str
    event_ts: int | None = None
    payload: RtmsStartedPayload


class RtmsStoppedEvent(BaseModel):
    """``meeting.rtms_stopped``."""

    model_config = _WIRE_CONFIG

    event: str
    event_ts: int | None = None
    payload: RtmsStoppedPayload
