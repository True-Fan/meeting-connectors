"""The loopback channel between the Python bridge and the Chromium page.

* ``protocol.py``  — the wire codec. Connector-private; enforced by
  ``tests/architecture/test_layering.py``.
* ``server.py``    — one WebSocket server per session, bound to loopback, token-gated.
* ``channel.py``   — ``PageChannel``: the accepted connection, framed both ways.
"""
