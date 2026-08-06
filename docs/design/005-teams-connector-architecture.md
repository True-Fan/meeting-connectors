# Microsoft Teams Connector — Architecture

**Status:** Implemented. Bridge side complete and tested; the .NET sidecar's Graph/Media SDK
integration is written but not yet compiled on Windows (§10).
**Builds on:** [003](./003-zoom-v1-architecture.md) (the actual Zoom build target) and
[002](./002-multi-platform-architecture-review.md) (the multi-platform roadmap).
**Wire protocol:** [006](./006-teams-sidecar-ipc-protocol.md).
**Date:** 2026-08-06

---

## 0. Summary

Teams is added as a second, fully independent connector. **Zoom's implementation is
unchanged** — not one file under `connectors/zoom/` was edited. Six shared modules changed,
all additively, and each is justified in §7.

The three findings that shaped it:

| # | Finding | Consequence |
|---|---|---|
| **T1** | App-hosted media is **Windows + .NET Framework only**. | The media runtime is a sidecar on a *different host and OS*, reached over TCP+TLS rather than Zoom's Unix socket. Nothing in Zoom's publishing path is reusable. |
| **T2** | Teams' media platform delivers **PCM 16 kHz mono** to the application. | Doc 002 §3.3 predicted a resampler for Teams. There is none: the format is *exactly* `AVATAR_INPUT_FORMAT`, so the zero-resample property holds for both connectors. |
| **T3** | Receive and send live in **one** `LocalMediaSession` on one call. | Zoom's two independently-recovering legs become one link with one fate. Recovery is a full rejoin. Doc 002 called this `ReconnectScope.FULL`; the enum stayed cut, the behaviour is what mattered. |

One further finding, better than expected: **unmixed (per-participant) audio is available**
on app-hosted media, so `EchoGuard` keeps its precise identity filter rather than falling
back to gating as doc 002 §2.3 assumed.

---

## 1. The platform constraint, and what it forces

Getting frame-level audio and video into a Teams meeting has exactly one official route:
**application-hosted media** via `Microsoft.Graph.Communications.Calls.Media`. That library
ships native media-platform binaries for **Windows x64 on .NET Framework** only — no Linux,
no .NET Core, no 32-bit.

The alternatives were considered and rejected:

| Alternative | Why not |
|---|---|
| Service-hosted media (`playPrompt`, `record`) | No frame-level access, no video send. Cannot carry an avatar. |
| Headless browser in the meeting | Browser automation — out of scope by instruction, and unsupported by Microsoft. |
| Virtual camera / audio device | Needs a desktop client and a logged-in session. Not a service. |

So the Teams media runtime cannot be a library in the Python process, and **none of Zoom's
publishing code applies**: different SDK, different language, different OS, different
transport, different credential type, different pixel format. The two connectors share the
ports in `protocols/` and the pipeline in `services/media/` — and nothing else. `connectors/teams`
does not import `connectors/zoom`, and a test asserts it
(`tests/architecture/test_layering.py::test_the_two_connectors_do_not_import_each_other`).

---

## 2. Shape: one link, where Zoom has two legs

This is the structural difference, and every other difference follows from it.

```
ZOOM — two independent integrations, independent recovery

  Zoom RTMS  ──WSS──▶ RtmsAudioSource ──┐
                                          ├──▶ shared pipeline ──┐
  Zoom Meeting SDK ◀──UDS──  MeetingPublisher ◀──────────────────┘
  (C++ sidecar, same container)

TEAMS — one Graph call, one media session, one link

                        ┌──────────────── TeamsSidecarLink ────────────────┐
  Graph + Media SDK ◀───┤  TeamsAudioSource (view)   TeamsMediaSink (view) ├──▶ shared pipeline
  (.NET, Windows host)  └──────────────── TCP + TLS ──────────────────────┘
```

`TeamsSidecarLink` owns the connection, the join, the roster, and recovery.
`TeamsAudioSource` and `TeamsMediaSink` are thin views onto it that satisfy `AudioSource`
and `MediaSink`, so the shared `MediaRouter` consumes Teams exactly as it consumes Zoom.

Those two adapters being ~70 lines while `RtmsAudioSource` is ~380 is not an
inconsistency — it *is* the platform difference. Zoom's ingest genuinely is an independent
integration with its own socket, handshake, keep-alive, and retry loop. Teams' is a view
onto a session it does not own. Giving Teams a fake ingest lifecycle that could "fail
independently" would be a lie the health report told the operator.

**Health follows the truth**: both Teams legs report the link's state, because there is no
condition in which Teams ingest is healthy and Teams egress is not.

---

## 3. Joining

### 3.1 The sidecar owns the join

Graph needs an app-hosted media blob in the call-creation request, and that blob can only
be produced by `MediaSession.GetMediaConfiguration()` — inside the media platform, on
Windows. So the **sidecar creates the call**; the bridge cannot make the Graph request
itself.

The bridge sends `CONTROL_JOIN` with credentials and a join descriptor, then waits for
`READY`. The first join runs **inline** in `TeamsSidecarLink.start()`, so a rejected
credential or an unparseable URL fails `POST /sessions` with a precise reason instead of
returning 202 and leaving an operator to discover from a health endpoint that the avatar
never arrived.

### 3.2 Two join routes

| Route | Input | Why it exists |
|---|---|---|
| `JoinMode.MEETING_ID` | `meeting_number` + `passcode` | The numeric "Meeting ID" from a Teams invite. It lands in the **existing** request fields, so an operator drives Teams and Zoom through an identical `POST /sessions` body. Preferred for exactly that reason. |
| `JoinMode.CHAT_INFO` | `meeting_url` | Parsed for thread id, tenant, and organizer. Needed because a calendar invite's link is often all anyone has. |

Join-URL parsing is genuinely fiddly — the thread id is a percent-encoded path segment and
the tenant/organizer live in a percent-encoded JSON `context` parameter — so
`graph/join_url.py` handles both encoding layers and names the missing part on every
failure. A URL pasted into `meeting_number` is accepted rather than rejected: it is a natural
mistake and everything needed is present.

### 3.3 Graph notifications terminate in the sidecar

Graph drives call lifecycle by POSTing notifications, and the Calling SDK *consumes* them:
`ProcessNotificationAsync` is what advances call state and delivers roster updates. They
must reach the object that owns the call, which is the Windows process.

**Consequence: `connectors/teams` adds no FastAPI router, and `src/api` gains no endpoint.**
Zoom's webhook stays in Python because its payload is routing data the bridge itself acts
on; Teams' notifications are SDK input, and relaying them through FastAPI would only forward
them straight back out.

### 3.4 There is no join race

Zoom's hardest lifecycle problem does not exist here. On Zoom, *we* initiate the join but
*Zoom* initiates RTMS via webhook, so the two can arrive in either order and doc 003 §3.1's
parking machinery exists to reconcile them. Teams initiates its own call and gets `READY`
back. Nothing to park.

That asymmetry needed one guard in shared code. `SessionRegistry.take_any_pending_rtms()`
claims a parked binding **by arrival order, not identity** — so a Teams session created
while a Zoom webhook sat parked would have swallowed that binding, leaving the Zoom session
it belonged to waiting forever for a webhook already consumed. `MeetingService._claim_pending_rtms`
now returns early unless the platform is Zoom. See §7.

---

## 4. Media

### 4.1 Video: I420 → NV12, converted in the sidecar

The pipeline's canonical pixel format is I420 (what the Zoom SDK wants). Teams' video
socket wants **NV12**. Both are 8-bit 4:2:0 with an identical Y plane; they differ only in
chroma layout — I420 keeps U and V as separate quarter planes, NV12 interleaves them.

The conversion happens **in the sidecar**, not in Python, because the sidecar must copy
every frame into unmanaged memory for the media platform regardless. The interleave rides
along on a copy already being made, instead of adding a per-frame ~1.4 MB shuffle inside the
bridge's event loop.

`domain.PixelFormat` therefore gains nothing: NV12 is a Teams implementation detail and
stays behind the connector boundary. The transform is specified and tested in
`tests/unit/test_teams_pixel_format.py` so the C# has an executable contract rather than an
argument.

### 4.2 Audio: no resampler, and now provably so

Teams' audio socket is configured `AudioFormat.Pcm16K` mono, which **is**
`AVATAR_INPUT_FORMAT`. Doc 002 §3.3 expected a resampler here, reasoning from Teams' network
codecs (SILK/G.722) — but those are transport codecs; the media platform hands the
application decoded PCM at the socket's configured rate.

`ingest/mapping.py` asserts the equality on every frame rather than assuming it, so a
misconfigured sidecar sending 48 kHz is dropped loudly at the boundary instead of feeding
the avatar audio it cannot use. Zoom's zero-resample property was a checked invariant; now
both connectors' are.

Teams' *publish* rate is its own setting (`MC_TEAMS__PUBLISH_SAMPLE_RATE_HZ`, default 16 kHz)
rather than the shared `media.publish_sample_rate_hz`, which is set to 32 kHz for Zoom's SDK.
Two SDKs want different rates; neither should have to move for the other.

### 4.3 Identity and echo suppression

`EchoGuard` is reused unchanged, and adapts by **data**:

| | Zoom | Teams |
|---|---|---|
| Per-participant audio | `AUDIO_MULTI_STREAMS` | Unmixed audio (up to 4 dominant speakers), tagged with a media source id |
| Own identity known | During the join handshake (`READY`) | From the **roster, after** the call is established |
| Fallback | Speaking gate | Speaking gate |

The timing difference is the only thing that needed care. Teams learns the bot's own
identity asynchronously, so `TeamsSessionFactory` subscribes
`link.add_participant_listener(echo_guard.set_own_participant)` — without it the identity
filter would stay disarmed for the entire session and echo suppression would rest on the
gate alone. The gate is exactly what covers the window in between, which is the second
defence layer doc 003 §3.3 built it to be.

If unmixed audio is requested but not granted, the link logs loudly and `EchoGuard`
escalates to strict gating on its own. Capability as data, not a branch.

---

## 5. Security

### 5.1 Transport

The link crosses a host boundary carrying meeting audio and a bearer credential, so TLS is
**on by default** and disabling it is an explicit local-development act. Mutual TLS is
available via `sidecar_client_cert_file`, with a thumbprint allow-list on the sidecar —
chain validity alone would accept any certificate from the same internal CA, including one
issued to a different service.

`TCP_NODELAY` is set explicitly. asyncio sets it for plain TCP but the guarantee does not
survive a TLS transport being layered on, and Nagle would batch 20 ms audio frames into
~40 ms groups — a fifth of the latency budget, spent invisibly.

### 5.2 Credentials travel per join, not per deployment

The Azure AD tenant, client id, and client secret are held by the **bridge** and sent in
`CONTROL_JOIN`. The Windows host is provisioned with infrastructure only — its FQDN, ports,
and certificate thumbprints.

Two consequences worth the design: rotating a client secret is a bridge-side config change
with no Windows deployment, and a compromised sidecar host yields no durable credential.
It mirrors the Zoom sidecar, which likewise never holds a long-lived credential (doc 004 §5.3).

---

## 6. Failure modes and recovery

| Failure | Detection | Response |
|---|---|---|
| Sidecar unreachable | `connect()` raises | Recoverable. Backoff with full jitter, then rejoin. |
| TLS verification fails | `SSLCertVerificationError` | **Fatal.** A bad chain fails identically on every retry; burning the budget only delays diagnosis. |
| Credential rejected / consent missing | Sidecar sends `ERROR fatal:true` | **Fatal.** Session fails with the Graph code and a pointer to the required permissions. |
| Negotiated media ≠ requested | Checked on `READY` | **Fatal.** A silent rate mismatch produces pitch-shifted speech and a geometry mismatch produces a garbled frame — both read as avatar bugs and cost hours to trace across a host boundary. |
| Link drops / sidecar crashes | EOF on read | Full rejoin (§2). Stale queued audio is cleared — replaying it would burst. |
| Framing desync | Bad magic or version | Connection torn down and rebuilt. Never resynchronised: a desynced binary stream cannot be realigned with confidence, and guessing surfaces as corrupt audio in a live meeting. |
| Graph terminates the call | `CALL_STATE = TERMINATED` | Degraded, not failed. The supervisor's grace window decides — the meeting may simply have ended, and an operator may be about to stop the session anyway. |
| Reconnect budget spent | Attempt counter | Leg reported `UNHEALTHY` with the attempt count; `SessionSupervisor` fails the session after its grace window. |
| Malformed inbound frame | `ingest/mapping.py` | One frame dropped and counted. A persistent fault shows as silence plus a climbing counter, not a torn-down call. |
| Link down while publishing | `is_connected` false | **Absorbed**, counted, leg degraded. The shared `Pacer` runs a continuous cadence for both connectors; letting an error escape would tear its task group down mid-reconnect, seconds before the link heals. |

---

## 7. Architecture impact: what changed outside `connectors/teams`

Six shared modules. Every change is additive and every default preserves Zoom's exact
behaviour.

| # | Module | Change | Why it was unavoidable |
|---|---|---|---|
| 1 | `domain/meeting.py` | Added `MeetingPlatform` enum; added `platform` field to `MeetingContext`, **defaulting to `ZOOM`**. | With one connector the platform was a constant folded into code (doc 003 §0.1). With two it is data, and it must be in the domain so `api/` and `services/` can route on it without importing a connector. `with_uuid` carries it, so Zoom's rebinding path cannot silently lose it. |
| 2 | `protocols/connector.py` | **New.** `ConnectorSession`, `ConnectorSessionFactory`. | Doc 003's rule: a protocol earns its place only with a second implementation. `MeetingService` already held `session_factory: object` and `SessionSupervisor` a `zoom_session: object` — honest duck-typing when there was nothing to abstract over. Teams is the second implementation. Both are `Protocol`s, so **`ZoomMeetingSession` satisfies them with zero edits.** |
| 3 | `services/meeting/connector_registry.py` | **New.** Platform → factory lookup. | The alternative is `if platform is ZOOM ... elif` inside `MeetingService` that every future connector edits. Doc 003 §0.1 correctly cut this as "indirection with one entry"; there are now two. |
| 4 | `services/meeting/service.py` | `platform` + `meeting_url` on `CreateSessionCommand` (defaulted); connector resolved from the registry; **`_claim_pending_rtms` gated to Zoom**; `session_factory=` still accepted. | The gate is the one behavioural fix, and it prevents a real cross-connector bug (§3.4). Keeping `session_factory=` means the pre-Teams constructor still works, so this is additive rather than a breaking API change. |
| 5 | `config/settings.py` | **New** `TeamsSettings` block. | Teams needs credentials and sidecar coordinates. Zoom's `zoom` and `sidecar` blocks are untouched — `sidecar` remains Zoom's UDS config. |
| 6 | `containers.py` | Teams providers; registry wiring. | Composition is what this module is for. Two safeguards protect Zoom: the Teams factory is passed as a **callable** so its config validation stays off Zoom's startup path, and a Teams failure is caught and logged so a malformed setting degrades to "Teams unavailable" rather than a service that will not boot. |
| — | `api/dto.py`, `api/routers/sessions.py` | `platform` and `meeting_url` passed through; `platform` on the response. | Two lines in the router. Google Meet will need none. |

### 7.1 The pre-existing bug that had to be fixed

`services/media/router.py` and `services/media/decode_pipeline.py` changed for a reason
unrelated to Teams.

`MediaRouter.run()` starts four legs in one `asyncio.TaskGroup`; two iterate
`decode.decoder.video()` and `.audio()`. But the decoder does not exist until the avatar's
first chunk arrives — `DecodePipeline.feed` starts it lazily — and `FfmpegDecoder.video()`
raises before that. **Both legs raised immediately, the task group unwound, and every
session tore down its own media pipeline microseconds after starting it**, taking the pacer
with it. The avatar's camera would freeze the instant it joined.

It reproduces identically through `ZoomSessionFactory` with no Teams code loaded, so it
predates this connector; it was invisible only because nothing exercised `MediaRouter.run`
end to end. The fix is `DecodePipeline.wait_started()`, awaited by both output legs.
`tests/unit/test_media_router_startup.py` covers both connectors.

This is the only change to shared code that alters behaviour rather than adding to it, and
it changes it from broken to working.

---

## 8. Reuse map

**Reused unchanged by Teams** — the whole point of the boundary:

`domain/` · `protocols/` · `avatar/` (client, transport, framing) ·
`services/media/` (router, pacer, clock, decode_pipeline, echo_guard, queues, idle_source,
decoders/ffmpeg, sinks) · `services/session/` (lifecycle, supervisor, registry) ·
`infrastructure/` (logging, metrics, reconnect, context, prometheus) · `api/` · `config/`

**Zoom-specific, correctly untouched:** everything under `connectors/zoom/` — RTMS ingest,
the webhook and OAuth routers, SDK JWT minting, the UDS sidecar protocol (doc 004, frozen),
`MeetingPublisher`, and the C++ sidecar.

**New, all inside `connectors/teams/`:** `graph/` (join resolution, wire models) ·
`sidecar/` (IPC codec, TCP/TLS transport, `link.py`, the .NET project) · `ingest/` ·
`publisher/` · `session/` · `config.py` · `exceptions.py`

---

## 9. Adding Google Meet, six months from now

The test of whether this worked. Meet is receive-only — there is no official publish API —
which doc 002 §16 identified as the case that breaks a fat connector interface.

What it would take: a `connectors/google_meet/` package implementing `AudioSource` and
`ConnectorSession`, one `MeetingPlatform.GOOGLE_MEET` enum member, one branch in
`build_connector_registry`, and one line in the connector-independence test. `api/`,
`services/`, `avatar/`, `media/`, and both existing connectors are untouched.

The one thing it would surface that two publishing platforms did not: a session with no
egress. `TeamsMediaSink` and `MeetingPublisher` both exist, so `MediaSink` is currently
mandatory. Meet would make it optional — and *that* is the moment to introduce the
capability descriptor doc 002 §2.3 designed, because that is the moment a second behaviour
actually needs it. Not before.

---

## 10. Verification status

| Layer | Status |
|---|---|
| Wire codec (Python) | ✅ 33 conformance tests asserting exact bytes. This is the contract the C# must satisfy. |
| Join URL / descriptor resolution | ✅ 24 tests, including both encoding layers and every documented failure mode. |
| Config validation | ✅ 13 tests, including the 25 fps trap (the repo's shared default, which Teams does not offer). |
| `TeamsSidecarLink` | ✅ 31 tests against an in-process fake sidecar speaking the real protocol: join, demux, attribution, backpressure, full rejoin, budget exhaustion. |
| Session composition | ✅ 18 tests asserting the shared pipeline is reused and both legs move together. |
| Registry / service routing / API | ✅ 40 tests, weighted toward Zoom-safety and backward compatibility. |
| I420 → NV12 | ✅ Specified and tested in Python as the reference for the C#. |
| **.NET Graph + Media SDK integration** | ⚠️ **Written against Microsoft's documented API surface, not yet compiled or run.** No Windows host, Azure tenant, or admin consent was available. `JoinMeetingIdMeetingInfo`, `IUnmixedAudioBuffer.ActiveSpeakerId`, and `Participant.Resource.MediaStreams[].SourceId` are the three names most likely to need reconciling against the SDK version restored. See `connectors/teams/sidecar/dotnet/README.md` §6. |

405 tests pass; ruff clean. Zoom's own suite is unchanged and green.

This mirrors how Zoom was built: the pipeline was proven against a stub before the C++ SDK
build and entitlement existed. `TeamsSessionFactory` accepts a `client_factory`, so the
entire Teams pipeline runs against `tests/fakes/teams_sidecar.py` today, and the remaining
work is confined to one file's SDK call sites.

---

## 11. Risks

| # | Risk | Mitigation |
|---|---|---|
| R1 | **Windows + .NET is a new deployment unit.** Different OS, own lifecycle, cannot live in `docker-compose`. | Accepted — it is the platform constraint (§1). Run as a Windows service with automatic restart; the bridge's reconnect handles the gap. |
| R2 | **One meeting per sidecar process.** `MediaPlatform.Initialize` binds native resources to a port and cannot run twice per process. | Concurrency means more processes on more ports. Documented; the sidecar refuses a second join rather than misbehaving. |
| R3 | Media SDK type names may differ from the version restored. | Confined to `CallHandler.cs` and `CommunicationsClientFactory.cs`; the wire contract either side of them is pinned by tests. |
| R4 | A publicly-trusted certificate and public FQDN are mandatory for the media platform. | Called out in the sidecar README; validated early with a message that names the FQDN/certificate relationship, which the SDK's own error does not. |
| R5 | Unmixed audio is limited to the dominant speakers. | `EchoGuard` degrades to its speaking gate for unattributed frames, which is exactly its designed fallback. |
| R6 | Graph/Azure AD quota or throttling on join. | Non-fatal Graph failures are reported as recoverable so backoff applies; auth failures are fatal and fail fast. |
