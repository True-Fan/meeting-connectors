"""HTTP edge.

Depends on ``services/``, ``domain/`` and ``protocols/`` only. Must never import
``connectors/`` — platform-specific routing lives with its connector, and the rule
is enforced by ``tests/architecture/test_layering.py``.
"""

from src.api.app import create_app

__all__ = ["create_app"]
