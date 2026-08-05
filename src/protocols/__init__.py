"""Ports.

Exactly four, per the scope-down rule in doc 003 §0: *a protocol earns its place
only if a second implementation exists in this repository today.*

============================  ===================================  =========================
Port                          Production implementation            Second implementation
============================  ===================================  =========================
``AudioSource``               ``RtmsAudioSource`` (M2)             ``ReplayAudioSource``
``AvatarTransport``           ``WebSocketAvatarTransport`` (M3)    ``FakeAvatarTransport``
``MediaDecoder``              ``FfmpegDecoder`` (M4)               ``FakeDecoder``
``MediaSink``                 ``MeetingPublisher`` (M5)            ``FileSink`` / ``NullSink``
============================  ===================================  =========================

Depends only on ``src.domain``. Enforced by ``tests/architecture/test_layering.py``.
"""

from src.protocols.audio_source import AudioSource
from src.protocols.avatar import AvatarTransport
from src.protocols.decoder import MediaDecoder
from src.protocols.sink import MediaSink

__all__ = ["AudioSource", "AvatarTransport", "MediaDecoder", "MediaSink"]
