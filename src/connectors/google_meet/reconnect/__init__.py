"""When to rejoin, and when rejoining is the wrong answer.

``classify.py`` holds that decision as data. The rejoin *mechanism* stays in
``bridge/chromium_bridge.py``, because relaunching a browser is inseparable from owning one —
extracting it would only invert the ownership and pass the bridge back in.
"""
