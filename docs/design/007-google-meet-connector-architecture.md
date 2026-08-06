# 007 — Google Meet connector architecture

**Status:** implemented
**Supersedes:** nothing. **Depends on:** 002 (multi-platform review), 003 (Zoom v1), 005 (Teams).

---

## 0. The finding that determined the entire design

Google publishes **no way to send media into a Meet conference.**

> "All conference media streams are \"receive-only\". Currently, the Meet Media API does not
> support sending of media from MediaApiClientInterface into a conference."
> — [Meet Media API C++ reference](https://developers.google.com/workspace/meet/media-api/reference/cpp/namespace/meet)

That single sentence is why this connector looks nothing like the other two. It is recorded
in code, with citations, in `connectors/google_meet/capabilities.py`, and asserted by
`tests/unit/test_google_meet_capabilities.py` — so a future contributor who believes they
have found an official publish path has to delete a test that quotes the Google sentence
contradicting them.

### 0.1 The full capability review (August 2026)

Google ships exactly three Meet developer surfaces
([overview](https://developers.google.com/workspace/meet/overview)):

| Surface | Stage | Can it publish media? |
|---|---|---|
| Meet add-ons SDK for Web | GA | **No.** An iframe in the Meet UI. Its shipped type definitions contain no `MediaStream`, track, camera, or capture API at all. Co-Watching only *notifies* Meet of playout state so peers can sync their own local players; media never traverses Meet. |
| Meet REST API v2 | GA | **No.** `SpaceConfig` has `moderation`, `moderationRestrictions`, `artifactConfig`, `attendanceReportGenerationType`, `accessType`, `entryPointAccess`. No media ingest, no Media-API toggle. |
| Meet Media API | Developer Preview | **No.** Receive-only, per the quote above. |

Answers to the questions the design review asked:

| Question | Answer |
|---|---|
| Can an app officially join a conference? | Yes — `spaces.connectActiveConference` (v2beta), Developer Preview only. |
| Can it officially receive participant audio? | Yes — WebRTC/SRTP, Opus 48 kHz on the wire, **exactly 3** virtual streams, Developer Preview. |
| Can it officially receive participant video? | Yes — VP8/VP9/AV1 required, 1–3 streams, needs an explicit video-assignment request first. |
| Can it officially publish custom video? | **No.** See §0. |
| Can it officially publish custom microphone audio? | **No.** Same sentence; it covers media generically. Protocol-level confirmation: the client offers `a=recvonly` and Meet answers `a=sendonly`. |
| Is there an official Media SDK? | No product by that name. Google ships **reference clients** (C++ and TypeScript, `googleworkspace/meet-media-api-samples`) that its own docs describe as "not intended to be a complete SDK". |
| Is there an official Meeting SDK? | **No.** There is no Meet equivalent of the Zoom Meeting SDK or of Graph app-hosted media. |
| If publishing is unsupported, what is the officially supported architecture? | For *consumption*: the Meet Media API (Preview) or, post-hoc, REST artifacts + Workspace Events over Pub/Sub (both GA). For *publishing*: **nothing exists.** A client — a real browser — is the only path. |

### 0.2 Why the Media API is not used even for ingest

It is technically a fit, and it was rejected for a non-technical reason quoted on every one
of its doc pages: the Cloud project, the OAuth principal, **and every participant in the
conference** must be enrolled in the Developer Preview Program. An external candidate joining
an interview breaks the session. Add one client per conference, hard blocks on encrypted,
watermarked, and minor-account meetings, and a consent dialog any participant can revoke
mid-call.

Since egress needs a browser regardless, taking audio from *the same browser* costs nothing
extra and removes every one of those gates.

---

## 1. Consequence: Chromium is the participant

```
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
Meet ◀── Chromium ◀── synthetic camera + microphone ◀── the same page
```

The browser is a real signed-in Chromium, driven by Playwright, joining the way a person
does. Meet's own admission controls gate it, which is the correct place for that decision to
be made.

### 1.1 Structural comparison with the other two connectors

| | Zoom | Teams | Google Meet |
|---|---|---|---|
| Ingest | RTMS WebSocket | Graph app-hosted media | Chromium `RTCPeerConnection` tap |
| Egress | Meeting SDK, C++ sidecar | same media session | Chromium synthetic devices |
| Independent legs | **Two** — either can heal alone | One media session | **One browser tab** |
| Recovery | Per-leg reconnect | Full rejoin (re-create the call) | Full relaunch (new browser) |
| Credential | SDK JWT + webhook secret | Azure AD client secret | **a browser profile on disk** |
| Native runtime | C++ sidecar over UDS | .NET sidecar over TCP/TLS | Chromium, loopback WebSocket |
| Per-participant audio | Yes (`AUDIO_MULTI_STREAMS`) | Yes (unmixed) | **No** — mixed by construction |

The asymmetry is the architecture, not an inconsistency. Flattening it would mean inventing
independence Meet does not have and telling the operator something untrue in the health
report.

---

## 2. What is reused, unchanged

Not one line of shared code was modified to accommodate a browser:

`AvatarClient` · `WebSocketAvatarTransport` · `MediaRouter` · `DecodePipeline` ·
`FfmpegDecoder` · `Pacer` · `EchoGuard` · `IdleFrameSource` · `MediaClock` ·
`BoundedFrameQueue` · `ReconnectPolicy` · `SessionSupervisor` · `SessionLifecycle` ·
`MeetingService` · `ConnectorRegistry` · `MetricsCollector` · logging · the `AudioSource`,
`MediaSink`, `ConnectorSession` and `ConnectorSessionFactory` ports · the whole domain model.

`tests/integration/test_google_meet_end_to_end.py` proves the pipeline runs through the real
`GoogleMeetSessionFactory` with only the browser, the avatar agent and the decoder doubled.

### 2.1 Shared infrastructure that *was* changed, and why

Five files. Every change is additive, and Zoom and Teams have **zero** code changes.

| File | Change | Why Meet cannot avoid it | Zoom / Teams impact |
|---|---|---|---|
| `domain/meeting.py` | `MeetingPlatform.GOOGLE_MEET = "google_meet"` | `MeetingService`, `ConnectorRegistry` and `api/dto.py` all route on this enum. It is the domain's identity type for a platform; a connector cannot be requested without a member. | None. Existing members unchanged; `MeetingContext.platform` still defaults to `ZOOM`. Guarded by `test_google_meet_registration.py::TestPlatformEnum`. |
| `config/settings.py` | `GoogleMeetSettings` + one field on `Settings` | Config-driven with no globals; nothing reads `os.environ` directly. | None. Defaults leave it unconfigured. Guarded by `test_google_meet_config.py::TestBackwardCompatibility`, which asserts Zoom's, Teams' and the shared media defaults are untouched. |
| `containers.py` | Meet providers + one registration branch; the guard-and-catch extracted to `_register_optional` | The only module permitted to import connectors. Its own docstring predicted "one more branch here". | None. Zoom still registers unconditionally and first. Extraction removed a *duplication* rather than adding one — two copies of the guard is how a broken Teams config starts taking Zoom's startup with it. `google_meet_factory` is keyword-only with a default, so pre-Meet callers are unchanged. Guarded by 14 tests in `test_google_meet_registration.py`. |
| `pyproject.toml` | `playwright` as an **optional extra** | The connector's entire media path is a browser. | None. Not installed by default, so a Zoom-only deployment pulls no browser. `automation/driver.py` imports it lazily and raises `PlaywrightUnavailableError` naming the install command. |
| `tests/architecture/test_layering.py` | `"google_meet"` added to the connector tuple and the enum assertion; three new rules | Both assertions enumerate the platform set, and the file's own comment says "Adding `google_meet` adds one line to this list." | None — and it now *also* guards Meet: the page codec cannot escape the connector, and `playwright` may only be imported by `automation/driver.py`. |

One existing test needed a one-line edit: `test_api_platforms.py::test_openapi_advertises_the_platform_enum` asserts the OpenAPI enum equals `["teams", "zoom"]`. It enumerates the platform set, so no implementation choice avoids it. The two assertions around it — that `platform` defaults to Zoom, and that an unknown platform is a 422 — were untouched.

---

## 3. Package layout

```
connectors/google_meet/
    capabilities.py       the §0 findings, as data with citations
    config.py             flattened connector-local view of Settings
    exceptions.py         recoverable vs fatal, which decides rejoin
    browser/              launch flags; the profile and its per-session copies
    automation/           Playwright lifecycle; every Meet DOM selector
    auth/                 Google sign-in — verified every session, done almost never
    meeting/              URL resolution, join flow, in-call controls, roster
    websocket/            the page wire codec and the per-session loopback server
    js/                   bridge.js + two AudioWorklets — the only JS in the repo
    audio_capture/        AudioSource port + inbound anti-corruption boundary
    virtual_camera/       I420 → the page's canvas-backed track
    virtual_microphone/   PCM → the page's AudioWorklet-backed track
    egress/               MediaSink port, composing the two adapters
    monitoring/           the media watchdog
    reconnect/            which failures are worth retrying
    session/              composition: GoogleMeetSession + factory
```

`automation/` rather than `playwright/`: a package by that name would resolve unambiguously
under this repo's absolute-import rule, but every reader of
`from playwright.async_api import ...` inside it would have to stop and work that out.

---

## 4. Decisions worth recording

### 4.1 Synthetic devices in-page, not OS-level

Rejected: `v4l2loopback` + a PulseAudio null sink. It needs a kernel module and therefore a
privileged container with matching kernel headers; it is Linux-only where the rest of this
connector runs anywhere Chromium does; it adds a second process boundary carrying 78 MB/s;
and **its failure mode is silent** — a loopback device that stops being written keeps
presenting as a healthy camera and Chromium publishes its last frame forever.

Chosen: `bridge.js` intercepts `getUserMedia` and returns tracks backed by a canvas and an
`AudioWorklet`. The "device" exists only inside the renderer that consumes it.

`--use-fake-device-for-media-stream` is deliberately **not** set. It would hand Meet
Chromium's own test pattern if the patch ever failed to install, which sounds like insurance
and is the worst available outcome: the avatar would appear as a rolling colour bar and the
session would look healthy.

### 4.2 No resampler exists anywhere in this repository

The capture `AudioContext` is constructed with `{sampleRate: 16000}`, so Web Audio
downsamples the conference's 48 kHz audio inside the browser's own graph, in native code, and
the worklet's render quantum is already at `AVATAR_INPUT_FORMAT`. Zoom gets the same property
from RTMS being natively `L16/16 kHz/mono`; Teams from the media platform's `Pcm16K`. Doc 002
§3.3 predicted a resampler for Teams and none was needed; the same holds here, by choosing
the graph's rate rather than by luck. `audio_capture/mapping.py` asserts the equality rather
than trusting it.

### 4.3 The video path is I420 end to end

The shared decoder emits packed I420; `WebCodecs.VideoFrame` accepts I420 natively. So a
frame crosses the bridge and reaches a real `MediaStreamTrack` with no colour conversion in
either language. Strides travel on the wire so the page can pass an explicit layout — an
inferred stride that is wrong produces a sheared image, which is slow to diagnose from
outside a headless browser.

`captureStream(0)` + `requestFrame()` gives an exact 1:1 mapping from a Pacer-paced frame to
a published frame. Letting the canvas sample on its own timer would resample that cadence and
reintroduce the jitter the Pacer exists to remove.

### 4.4 One WebSocket server per session, token-gated

The page can only connect outward, so the direction is forced. Per-session rather than shared
because the port can then be ephemeral — two concurrent sessions cannot collide — and because
the only page that can reach it is the one we launched.

Loopback is shared with every process on the host, so binding is not authentication: the URL
carries a `secrets.token_urlsafe` token checked with `compare_digest` *before* the handshake
completes. A replacement connection is accepted, and must be: an init script runs afresh on
every full navigation, and the bridge navigates at least twice before joining.

### 4.5 Echo is structurally impossible, not filtered

`bridge.js` taps audio from `RTCPeerConnection`'s `track` event, which fires for **inbound**
transceivers only. The avatar's outbound microphone track can never enter the capture graph.
So `own_participant()` returns `None` — correctly, not incompletely — and `EchoGuard` is
configured with `per_participant_audio=False`, running its speaking gate in strict mode. That
gate covers the remaining risk, which is acoustic (a host with real speakers), not a software
loop.

The roster is therefore **observability only** on this connector, unlike the other two where
it supplies the identity `EchoGuard` filters on. Worth stating plainly, because the natural
assumption from Zoom and Teams is the opposite.

### 4.6 Recovery is a full relaunch

A crashed renderer takes the peer connection, both `AudioContext` graphs, the canvas and the
synthetic tracks with it; none can be reattached. So a rejoin closes the browser, discards the
working profile, takes a fresh copy from the template, launches, and joins again.

Because that is expensive, the backoff is slower than the other connectors' (2 s → 30 s, five
attempts, full jitter) and the fatal/recoverable split is centralised in
`reconnect/classify.py` so the two branches that consult it cannot drift.

**Denial and ejection are classed fatal even though a retry could technically succeed.** An
automated Google account that repeatedly asks to enter a meeting it was thrown out of is
indistinguishable from abuse, and losing the account breaks every session rather than one.
This is the one place where the right engineering answer and the right operational answer
diverge, which is why it is written down in three places.

### 4.7 Profiles are templates, and sessions get copies

A Chromium profile is a single-writer resource: two browsers on one directory do not share it,
the second corrupts the first, and the failure mode is a profile that silently lost its Google
session. Treating the configured directory as a template and seeding a throwaway copy per
session is what allows more than one concurrent meeting. Only the identity-bearing files are
copied — `Cookies` plus the `Local State`/`Preferences` that hold its encryption key, without
which the cookie database decrypts to nothing and the profile presents as signed out.

### 4.8 The watchdog watches media, not the process

Every *structural* failure is already visible: a crashed renderer closes the channel, a denied
join arrives as a state message. What is not visible is the browser staying alive while the
audio stops — a suspended `AudioContext`, or remote tracks that all ended without a
renegotiation. The tab runs, the channel is connected, the pacer publishes, every health check
is green, and the avatar cannot hear.

Silence alone is not the trigger: an avatar alone in a meeting legitimately receives nothing.
The trigger is silence **plus** at least one other participant, which is the only reason the
roster is collected at all. It degrades rather than fails, because the signal is inferential.

---

## 5. Page bridge wire protocol — version 1

Reference implementation: `websocket/protocol.py`. The encoder in `js/bridge.js` must match
byte for byte; `tests/unit/test_google_meet_protocol.py` pins the vector and
`tests/unit/test_google_meet_js_assets.py` parses the constants out of the JavaScript and
compares them.

**Header — 24 bytes, big-endian throughout:**

| Offset | Size | Field |
|---:|---:|---|
| 0 | 4 | magic `0x474D4331` (`'GMC1'`) |
| 4 | 1 | wire version (`1`) |
| 5 | 1 | message type |
| 6 | 1 | flags |
| 7 | 1 | reserved |
| 8 | 4 | sequence |
| 12 | 8 | pts, microseconds, signed |
| 20 | 4 | payload length |

**Messages:** `VIDEO_I420` 0x01 (→page) · `AUDIO_PCM` 0x02 (**both ways**) · `HELLO` 0x03 ·
`CONFIG` 0x04 · `READY` 0x05 · `LEAVE` 0x06 · `HEARTBEAT` 0x07 · `ERROR` 0x08 ·
`PARTICIPANTS` 0x09 · `MEET_STATE` 0x0A · `PAGE_EVENT` 0x0B

**Flags:** `NONE` 0x00 · `KEYFRAME` 0x01 · `SILENCE` 0x02 · `MIXED` 0x04

**Audio sub-header (12 B):** `rate:u32 | channels:u8 | format:u8 | frame_ms:u16 | source:u32`
**Video sub-header (12 B):** `w:u16 | h:u16 | stride_y:u16 | stride_uv:u16 | fps:u16 | rsv:u16`

There is **no incremental decoder**, unlike Teams'. WebSocket is message-oriented, so the
transport delivers exactly the frames the sender wrote and the whole resynchronisation problem
— and its class of desync bug — does not exist. The length field is kept as a
self-consistency check against a page running a mismatched script.

**Nothing on this wire is a credential.** The Google session lives in the browser profile and
never crosses it.

### 5.1 Join sequence

1. Resolve the URL and the profile — a bad code or missing profile costs nothing.
2. Bind the bridge server; its port is baked into an init script, so it must exist first.
3. Take a working profile from the template.
4. Launch Chromium, **then** inject. Injection must precede any navigation or Meet captures a
   pristine `getUserMedia`.
5. Verify the Google session (`myaccount.google.com`) — before the meeting, so "not signed in"
   is a precise error rather than an unexplained join timeout.
6. Navigate and join. Lobby waits run on their own, longer budget.
7. **Then** attach to the page channel — navigating replaced the socket from step 5.
8. Send `CONFIG`, await `READY`, verify the page's rates and geometry match the request.
9. Unmute and turn the camera on. Meet does not publish tracks it was handed until told to,
   and no other layer can observe that it did not.

---

## 6. Risks

| Risk | Severity | Mitigation |
|---|---|---|
| **Meet UI changes break selectors** | High, and inevitable | Every selector in one file, several candidates per concept, ARIA preferred over generated class names, text only for terminal states. Which candidate matched is logged so a rename is visible before it breaks. |
| **Google account restricted for automation** | High | Never retry a refusal (§4.6). `--disable-blink-features=AutomationControlled`. Sign in once interactively rather than scripting it. Leave the call cleanly. |
| Terms-of-service exposure | Medium | Google publishes no clause naming browser automation, but the Workspace developer policy forbids monitoring "without consent" and using apps to "bypass Meet account limitations". A deployment must have participants' consent; this is a legal review, not an engineering control. |
| Resource cost | Medium | One Chromium per session, ~300–500 MB. Bounded by 1080p publish ceiling and `--disable-dev-shm-usage`. |
| Concurrency limited by profiles | Low | Per-session copies (§4.7). |
| Headless Chromium throttling | Low | Three backgrounding flags, asserted by name in tests. |

## 7. Known gap, pre-existing and shared

**No connector in this repository calls `AvatarClient.start()`** — not Zoom, not Teams, not
Google Meet. So the `AvatarClientHello`/`AvatarServerHello` handshake in `domain/avatar.py` is
never exchanged during a live session, and `WebSocketAvatarTransport`'s writer task (created in
`connect()`) is never started, which means PCM offered to its queue would not be written.

Found while writing `tests/integration/test_google_meet_end_to_end.py`, which initially
asserted the handshake had happened. It is **not** introduced by this connector: Meet's
`start()` ordering is identical to Zoom's and Teams', and the behaviour reproduces on all
three. Fixing it means changing shared code on the startup path of two deployed connectors,
which is out of scope for an additive changeset — recorded here so it is not rediscovered as a
Meet bug.
