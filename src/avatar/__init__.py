"""Streaming Avatar Agent client.

The avatar contract is fixed (PCM 16 kHz mono in, fragmented MP4 out) and this package
is platform-blind: it must never import ``connectors/``.
"""

from src.avatar.client import AvatarClient
from src.avatar.framing import Fmp4Framer, Fmp4FramingError
from src.avatar.ws_transport import AvatarTransportError, WebSocketAvatarTransport

__all__ = [
    "AvatarClient",
    "AvatarTransportError",
    "Fmp4Framer",
    "Fmp4FramingError",
    "WebSocketAvatarTransport",
]
