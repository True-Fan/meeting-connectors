"""Platform adapters.

The only place platform concepts — a selector, a join form, a page protocol — may
appear. Nothing outside this package imports it except ``src.containers``; enforced by
``tests/architecture/test_layering.py``.

Three connectors, all browser-based: ``google_meet``, ``zoom_web``, ``teams_web``. Two
others were removed — a Zoom one publishing through a native Meeting-SDK sidecar, and a
Teams one using Graph app-hosted media on a Windows host — because each required
something of the meeting's host that a deployment cannot obtain for meetings other
people book. See each package's ``__init__`` for the measurements behind that.
"""
