"""Zoom connector.

Two independent integrations, in strictly separate directions (doc 003 §1.1):

* ``rtms/``      — RTMS WebSocket ingest. Receive only. **M2.**
* ``publisher/`` — Meeting SDK publish via the C++ sidecar. Send only. **M5**,
  except ``publisher/protocol.py``, whose wire format is frozen in M1.

RTMS cannot publish media and the Meeting SDK is not used to receive it. That
separation is deliberate and load-bearing, not incidental.
"""
