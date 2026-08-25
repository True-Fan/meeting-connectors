"""Zoom Meeting SDK publisher.

M1 ships only the frozen IPC codec — see ``docs/design/004-sidecar-ipc-protocol.md``.
``MeetingPublisher``, ``SidecarUdsClient`` and the C++ sidecar itself arrive in M5.
"""

from src.connectors.zoom.publisher.protocol import (
    HEADER_SIZE,
    MAGIC,
    MAX_PAYLOAD_BYTES,
    WIRE_VERSION,
    AudioPayload,
    SidecarFlags,
    SidecarFrameDecoder,
    SidecarMessage,
    SidecarMessageType,
    SidecarProtocolError,
    VideoPayload,
    decode_audio_payload,
    decode_video_payload,
    encode,
    encode_audio,
    encode_json,
    encode_video,
)

__all__ = [
    "HEADER_SIZE",
    "MAGIC",
    "MAX_PAYLOAD_BYTES",
    "WIRE_VERSION",
    "AudioPayload",
    "SidecarFlags",
    "SidecarFrameDecoder",
    "SidecarMessage",
    "SidecarMessageType",
    "SidecarProtocolError",
    "VideoPayload",
    "decode_audio_payload",
    "decode_video_payload",
    "encode",
    "encode_audio",
    "encode_json",
    "encode_video",
]
