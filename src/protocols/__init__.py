"""Ports.

The scope-down rule from doc 003 §0 still governs: *a protocol earns its place only
if a second implementation exists in this repository today.*

Every port below has at least two implementations across the three connectors, which is
what keeps them earned. The set was established when a second connector arrived — see
``connector.py`` for why the last two were correctly absent before that.

=============================  ===================================  ==============================
Port                           Production implementation            Second implementation
=============================  ===================================  ==============================
``AudioSource``                ``PageAudioSource`` (zoom_web)       ``MeetAudioSource`` · replay
``AvatarTransport``            ``WebSocketAvatarTransport``         ``FakeAvatarTransport``
``MediaDecoder``               ``FfmpegDecoder``                    ``FakeDecoder``
``MediaSink``                  ``ZoomWebMediaSink``                 ``TeamsWebMediaSink`` · file
``ConnectorSession``           ``ZoomWebSession``                   ``TeamsWebSession``
``ConnectorSessionFactory``    ``ZoomWebSessionFactory``            ``TeamsWebSessionFactory``
=============================  ===================================  ==============================

Depends only on ``src.domain``. Enforced by ``tests/architecture/test_layering.py``.
"""

from src.protocols.audio_source import AudioSource
from src.protocols.avatar import AvatarTransport
from src.protocols.connector import ConnectorSession, ConnectorSessionFactory
from src.protocols.decoder import MediaDecoder
from src.protocols.sink import MediaSink

__all__ = [
    "AudioSource",
    "AvatarTransport",
    "ConnectorSession",
    "ConnectorSessionFactory",
    "MediaDecoder",
    "MediaSink",
]
