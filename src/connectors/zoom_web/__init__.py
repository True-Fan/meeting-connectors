"""Zoom, joined with a browser.

**The one Zoom connector.** There was a second — a native C++ sidecar built against the
Meeting SDK for Linux, publishing media, with Zoom's RTMS API for ingest. It has been
removed: the SDK is a licensed download gated behind an entitlement and buildable only on
Linux, and RTMS requires the meeting to be hosted on an account with RTMS enabled for the
app, which a deployment cannot arrange for meetings other people book. This connector joins
the same meetings with Chromium and needs neither.

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

So the avatar speaks through a virtual microphone, and hears by tapping Zoom's own
**playout** graph — where every transport has to converge, whether Zoom decodes over
WebRTC or in WebAssembly off a WebSocket. See ``js/inject.js`` for why the tap is placed
there rather than on the peer connection, and ``ingest/`` for what it collects.

Everything else the avatar knows about the meeting — the roster, the active speaker, the
chat, the captions, a raised hand — is read off the page and crosses into Python as this
connector's own observation types (``observations.py``).
"""
