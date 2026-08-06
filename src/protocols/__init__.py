"""Ports.

The scope-down rule from doc 003 §0 still governs: *a protocol earns its place only
if a second implementation exists in this repository today.*

Four ports were earned by the Zoom build itself. Two more were earned when the Teams
connector arrived and supplied the second implementation the rule demands — see
``connector.py`` for why they were correctly absent before that.

=============================  ===================================  ==============================
Port                           Production implementation            Second implementation
=============================  ===================================  ==============================
``AudioSource``                ``RtmsAudioSource``                  ``TeamsAudioSource`` · replay
``AvatarTransport``            ``WebSocketAvatarTransport``         ``FakeAvatarTransport``
``MediaDecoder``               ``FfmpegDecoder``                    ``FakeDecoder``
``MediaSink``                  ``MeetingPublisher`` (Zoom)          ``TeamsMediaSink`` · file/null
``ConnectorSession``           ``ZoomMeetingSession``               ``TeamsMeetingSession``
``ConnectorSessionFactory``    ``ZoomSessionFactory``               ``TeamsSessionFactory``
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
