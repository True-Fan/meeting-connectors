"""Graph join resolution.

``models.py`` holds the wire shapes and must not be imported outside this package —
the Teams half of the anti-corruption rule, enforced by
``tests/architecture/test_layering.py``. ``join_url.py`` is the translation layer, and
is what the rest of the connector imports.
"""

from src.connectors.teams.graph.join_url import (
    looks_like_join_url,
    normalise_meeting_id,
    parse_join_url,
    resolve_join_descriptor,
)

__all__ = [
    "looks_like_join_url",
    "normalise_meeting_id",
    "parse_join_url",
    "resolve_join_descriptor",
]
