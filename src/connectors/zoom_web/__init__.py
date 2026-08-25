"""Zoom, joined with a browser instead of the Meeting SDK.

**Why this exists alongside ``connectors/zoom``.** That connector publishes through a
native C++ sidecar built against the Meeting SDK for Linux — a licensed download,
gated behind an entitlement, buildable only on Linux, and currently a stub. This one
joins the same meetings with Chromium, which needs none of that.

**The split between the two halves is measured, not chosen** (``scripts`` probes,
against live meetings):

* Zoom's web client constructs a standard ``RTCPeerConnection`` and calls
  ``getUserMedia``, so it is drivable.
* It has **no audio transceiver in either direction**. ``replaceTrack`` — the way the
  Google Meet connector publishes — cannot reach it, and a ``track`` handler never
  fires, so the meeting cannot be *heard* through WebRTC either.
* An injected ``MediaStreamTrack`` is taken into Zoom's audio graph and then never
  transmitted, even when it is the only device the page can see.
* A device-level microphone **is** transmitted, audibly.

So the avatar speaks through a virtual microphone, and hears through **RTMS** —
Zoom's own API, already implemented in ``connectors/zoom/rtms``, which carries the
audio and the speaker's name. Each direction uses the mechanism Zoom actually
supports, and only the publish half needs anything from the host.
"""
