"""The ``MediaSink`` port, composing the two synthetic devices.

``media_sink.py`` is what the shared ``Pacer`` publishes into. It owns no transport of its
own — the camera and microphone adapters do — so it is purely the seam that lets the shared
pipeline drive a browser.
"""
