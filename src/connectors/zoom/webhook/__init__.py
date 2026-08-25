"""Zoom webhook endpoint. Owned by the connector so ``api/`` stays platform-blind."""

from src.connectors.zoom.webhook.router import build_router

__all__ = ["build_router"]
