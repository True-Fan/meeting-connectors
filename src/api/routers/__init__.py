"""HTTP routers.

M1: health and metrics. ``sessions.py`` (POST/DELETE /sessions) arrives in M2 with
``MeetingService``. There is no platform-specific router left — the Zoom webhook one went
with the Meeting-SDK connector
so that platform-specific signature verification stays out of ``api/``.
"""
