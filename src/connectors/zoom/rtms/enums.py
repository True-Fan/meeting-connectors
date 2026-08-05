"""RTMS protocol constants.

Transcribed from Zoom's published data-type definitions
(https://developers.zoom.us/docs/rtms/data-types/). These are Zoom's numbers, not
ours — nothing here may be invented, and nothing here leaves
``connectors/zoom/rtms/``.
"""

from __future__ import annotations

from enum import IntEnum, IntFlag


class MediaDataType(IntFlag):
    """``media_type`` bitmask in the data handshake."""

    AUDIO = 0x01
    VIDEO = 0x01 << 1
    DESKSHARE = 0x01 << 2
    TRANSCRIPT = 0x01 << 3
    CHAT = 0x01 << 4
    ALL = 0x01 << 5


class MediaContentType(IntEnum):
    """``content_type`` inside ``media_params``."""

    RTP = 1
    RAW_AUDIO = 2
    RAW_VIDEO = 3
    FILE_STREAM = 4
    TEXT = 5


class AudioSampleRate(IntEnum):
    """``sample_rate`` inside ``media_params.audio``."""

    SR_8K = 0
    SR_16K = 1
    SR_32K = 2
    SR_48K = 3

    @property
    def hz(self) -> int:
        return {
            AudioSampleRate.SR_8K: 8_000,
            AudioSampleRate.SR_16K: 16_000,
            AudioSampleRate.SR_32K: 32_000,
            AudioSampleRate.SR_48K: 48_000,
        }[self]

    @classmethod
    def from_hz(cls, hz: int) -> AudioSampleRate:
        for candidate in cls:
            if candidate.hz == hz:
                return candidate
        raise ValueError(f"RTMS does not offer a {hz} Hz sample rate")


class AudioChannel(IntEnum):
    """``channel`` inside ``media_params.audio``."""

    MONO = 1
    STEREO = 2


class AudioCodec(IntEnum):
    """``codec`` inside ``media_params.audio``."""

    L16 = 1
    G711 = 2
    G722 = 3
    OPUS = 4


class MediaDataOption(IntEnum):
    """``data_opt`` inside ``media_params``."""

    AUDIO_MIXED_STREAM = 1
    AUDIO_MULTI_STREAMS = 2
    VIDEO_SINGLE_ACTIVE_STREAM = 3
    VIDEO_SINGLE_INDIVIDUAL_STREAM = 4


class RtmsMessageType(IntEnum):
    """``msg_type`` on both the signaling and media sockets.

    Note the shape of this list: every value is inbound media, inbound metadata, or
    bidirectional *control*. There is no value that carries application media toward
    Zoom — which is the protocol-level reason RTMS cannot publish (doc 001 §1.2).
    """

    SIGNALING_HAND_SHAKE_REQ = 1
    SIGNALING_HAND_SHAKE_RESP = 2
    DATA_HAND_SHAKE_REQ = 3
    DATA_HAND_SHAKE_RESP = 4
    EVENT_SUBSCRIPTION = 5
    EVENT_UPDATE = 6
    CLIENT_READY_ACK = 7
    STREAM_STATE_UPDATE = 8
    SESSION_STATE_UPDATE = 9
    SESSION_STATE_REQ = 10
    SESSION_STATE_RESP = 11
    KEEP_ALIVE_REQ = 12
    KEEP_ALIVE_RESP = 13
    MEDIA_DATA_AUDIO = 14
    MEDIA_DATA_VIDEO = 15
    MEDIA_DATA_SHARE = 16
    MEDIA_DATA_TRANSCRIPT = 17
    MEDIA_DATA_CHAT = 18
    STREAM_STATE_REQ = 19
    STREAM_STATE_RESP = 20
    STREAM_CLOSE_REQ = 21
    STREAM_CLOSE_RESP = 22
    META_DATA_AUDIO = 23
    META_DATA_VIDEO = 24
    META_DATA_SHARE = 25
    META_DATA_TRANSCRIPT = 26
    META_DATA_CHAT = 27
    VIDEO_SUBSCRIPTION_REQ = 28
    VIDEO_SUBSCRIPTION_RESP = 29


class RtmsEventType(IntEnum):
    """``event_type`` inside ``EVENT_UPDATE``."""

    FIRST_PACKET_TIMESTAMP = 1
    ACTIVE_SPEAKER_CHANGE = 2
    PARTICIPANT_JOIN = 3
    PARTICIPANT_LEAVE = 4
    SHARING_START = 5
    SHARING_STOP = 6
    MEDIA_CONNECTION_INTERRUPTED = 7
    PARTICIPANT_VIDEO_ON = 8
    PARTICIPANT_VIDEO_OFF = 9
    CHAT_GROUP_CREATE = 10
    CHAT_GROUP_DELETE = 11
    CHAT_GROUP_MEMBERS_ADD = 12
    CHAT_GROUP_MEMBERS_DELETE = 13
    CHAT_GROUP_MEMBER_STATUS_UPDATE = 14


class RtmsStatusCode(IntEnum):
    """Handshake ``status_code`` values we act on. ``0`` is success."""

    OK = 0


PROTOCOL_VERSION = 1
"""``protocol_version`` sent in both handshakes."""

WEBHOOK_EVENT_RTMS_STARTED = "meeting.rtms_started"
WEBHOOK_EVENT_RTMS_STOPPED = "meeting.rtms_stopped"
WEBHOOK_EVENT_URL_VALIDATION = "endpoint.url_validation"
