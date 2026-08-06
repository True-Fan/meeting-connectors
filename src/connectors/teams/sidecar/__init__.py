"""Teams media sidecar: IPC codec, transport, and the media session link.

``protocol.py`` is the wire codec — see ``docs/design/006-teams-sidecar-ipc-protocol.md``
— and is pure, so the whole boundary is testable with no Windows host. ``link.py`` owns
the one media session that carries both directions. ``dotnet/`` is the Windows .NET bot
that terminates the link.
"""

from src.connectors.teams.sidecar.link import TeamsSidecarLink
from src.connectors.teams.sidecar.protocol import (
    HEADER_SIZE,
    MAGIC,
    MAX_PAYLOAD_BYTES,
    MIXED_SOURCE,
    WIRE_VERSION,
    AudioWireHeader,
    CallState,
    TeamsFlags,
    TeamsFrameDecoder,
    TeamsMessage,
    TeamsMessageType,
    VideoWireHeader,
    decode_audio_payload,
    decode_video_payload,
    encode_audio,
    encode_header,
    encode_json,
    encode_video,
)
from src.connectors.teams.sidecar.tcp_client import TeamsSidecarClient, build_ssl_context

__all__ = [
    "HEADER_SIZE",
    "MAGIC",
    "MAX_PAYLOAD_BYTES",
    "MIXED_SOURCE",
    "WIRE_VERSION",
    "AudioWireHeader",
    "CallState",
    "TeamsFlags",
    "TeamsFrameDecoder",
    "TeamsMessage",
    "TeamsMessageType",
    "TeamsSidecarClient",
    "TeamsSidecarLink",
    "VideoWireHeader",
    "build_ssl_context",
    "decode_audio_payload",
    "decode_video_payload",
    "encode_audio",
    "encode_header",
    "encode_json",
    "encode_video",
]
