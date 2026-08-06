"""Google Meet connector — Chromium as the meeting participant.

**Why this connector has a fundamentally different shape from the other two.** Zoom
and Teams each expose an official, server-side SDK that can *send* media into a
conference: the Zoom Meeting SDK's external video source and virtual microphone, and
``Microsoft.Graph.Communications.Calls.Media``'s app-hosted ``LocalMediaSession``.
Google ships no such thing. Its only real-time media surface, the Meet Media API, is
explicitly receive-only:

    "All conference media streams are 'receive-only'. Currently, the Meet Media API
    does not support sending of media from MediaApiClientInterface into a conference."
    -- developers.google.com/workspace/meet/media-api/reference/cpp/namespace/meet

There is no Meet equivalent of a Meeting SDK, and no ``SpaceConfig`` field, add-on API,
or REST method that accepts inbound media. The full evidence, with citations and launch
stages, is in ``capabilities.py`` — it is recorded in code because it is the premise
the entire design rests on, and a future contributor who does not know it will
reasonably ask why this connector runs a browser.

So the avatar cannot be a *server* on Meet. It has to be a **client**: a real Chromium
instance, signed into a real Google account, joining like a person and publishing
through the ordinary ``getUserMedia`` path. That is what this package builds.

    Meet ──▶ Chromium ──▶ remote audio tracks ──▶ AudioWorklet ──▶ 16 kHz mono PCM
                                                                        │
                                                          WebSocket (loopback)
                                                                        ▼
                                             AvatarClient ──▶ fragmented MP4
                                                                        │
                                                            FfmpegDecoder
                                                                        ▼
                                                    I420 frames + PCM ──▶ Pacer
                                                                        │
                                                          WebSocket (loopback)
                                                                        ▼
    Meet ◀── Chromium ◀── synthetic camera + microphone tracks ◀── the same page

Package layout, and what each part is allowed to know:

* ``capabilities.py``   — the official-API findings that justify the browser. Data.
* ``config.py``         — a flattened, connector-local view of ``Settings``.
* ``browser/``          — Chromium launch flags and the persistent profile on disk.
* ``automation/``       — Playwright lifecycle, the page, and the Meet DOM selectors.
  Named ``automation`` rather than ``playwright`` so that ``import playwright`` inside
  this package can never be ambiguous with the third-party module it wraps.
* ``auth/``             — Google sign-in *into the persistent profile*.
* ``meeting/``          — URL resolution, the join flow, in-call controls, roster.
* ``websocket/``        — the page↔bridge wire codec and the per-session loopback
  server. ``protocol.py`` is connector-private and must not leave this package.
* ``js/``               — the assets injected into the page: the bridge script and the
  two AudioWorklet processors. The only JavaScript in this repository.
* ``audio_capture/``    — the ``AudioSource`` port, plus the inbound anti-corruption
  boundary.
* ``virtual_camera/``   — I420 video frames to the page's synthetic camera track.
* ``virtual_microphone/`` — PCM to the page's synthetic microphone track.
* ``egress/``           — the ``MediaSink`` port, composing the two adapters above.
* ``monitoring/``       — browser and page liveness.
* ``reconnect/``        — rejoin supervision.
* ``session/``          — composition: ``GoogleMeetSession`` and its factory.

**The Chromium bridge carries no business logic.** ``bridge/chromium_bridge.py`` and
everything under ``automation/`` and ``js/`` move bytes and drive a browser. They hold
no avatar knowledge, no decoding, no metrics, and no logging — the page reports events
and the Python side decides what they mean and records them. That separation is what
keeps the browser layer replaceable.

**Nothing here is shared with ``connectors/zoom`` or ``connectors/teams``.** No import
crosses between connectors; ``tests/architecture/test_layering.py`` enforces it. What
the three genuinely share is the ports in ``src/protocols`` and the media pipeline in
``src/services/media`` — reused here without a line changing in either of them.
"""
