"""The Chromium bridge: one browser, in one meeting, for one session.

``chromium_bridge.py`` owns the browser, the profile, the page channel, the join, and
recovery. It carries no avatar knowledge, no decoding, and no media accounting — the
``AudioSource`` and ``MediaSink`` adapters are thin views onto it, and they are where
frames are counted.
"""
