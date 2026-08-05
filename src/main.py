"""ASGI entrypoint.

``uvicorn src.main:app`` in the container. The factory is called once here so that
importing this module is the only place a process-wide app exists.
"""

from __future__ import annotations

from src.api.app import create_app

app = create_app()
