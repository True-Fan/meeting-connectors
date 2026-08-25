"""Teams audio ingest.

``mapping.py`` is the inbound anti-corruption boundary: sidecar wire types and Graph
roster shapes stop there and ``src.domain`` begins.
"""

from src.connectors.teams.ingest.audio_source import TeamsAudioSource

__all__ = ["TeamsAudioSource"]
