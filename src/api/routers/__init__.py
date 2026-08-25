"""HTTP routers.

M1: health and metrics. ``sessions.py`` (POST/DELETE /sessions) arrives in M2 with
``MeetingService``, and the Zoom webhook router lives in ``connectors/zoom/webhook/``
so that platform-specific signature verification stays out of ``api/``.
"""
