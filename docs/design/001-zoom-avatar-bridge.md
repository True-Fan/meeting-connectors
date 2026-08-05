# Zoom ↔ Streaming Avatar Agent Bridge — Technical Design

**Status:** Phase 1 — Awaiting approval
**Author:** Architecture
**Date:** 2026-08-05
**Scope:** Proof of Concept. Bridge only. No AI, no avatar rendering, no speech processing.

---

## 0. Executive Summary — The Architecture Verdict

> **RTMS alone is NOT sufficient.** It is a strictly receive-only pipeline.

| Requirement | Technology | Verdict |
|---|---|---|
| Receive participant audio | **Zoom RTMS** | ✅ Sufficient, and near-ideal |
| Publish avatar audio + video | **Zoom Meeting SDK for Linux** (raw data send) | ⚠️ Required, C++ only |

The PoC therefore has **two Zoom integrations**, not one, joined by a Python media router:

```
Zoom Meeting ──RTMS(WSS)──► Python Bridge ──WS──► Avatar Agent
     ▲                          │                      │
     │                          │◄──── streaming fMP4 ─┘
     └── Meeting SDK Bot ◄──IPC─┘
        (C++ sidecar)
```

**The single biggest external unknown is not Zoom — it is the avatar's MP4.** See §8 and §12.1.

---

## 1. Why RTMS — and Where It Stops

### 1.1 Why RTMS for ingest

RTMS is Zoom's first-class realtime media pipeline. For the *receive* half of this bridge it is the correct choice, for four concrete reasons:

1. **No media client to operate.** Zoom pushes media to us over WebSocket. There is no headless browser, no virtual sound card, no X server, no Chromium in a container just to hear audio.
2. **The native audio format is an exact match for the avatar.** RTMS emits uncompressed PCM `L16`, and `sample_rate: SR_16K (1)`, `channel: MONO (1)` are directly selectable in the handshake. The avatar wants PCM/16 kHz/mono. **Zero resampling on the ingest path** — no quality loss, no added latency, no SciPy/`libsamplerate` in the hot path.
3. **Per-participant streams are available.** `data_opt: AUDIO_MULTI_STREAMS (2)` yields separate tracks per `user_id`. This is not a nicety — it is what makes echo suppression tractable (§6.3).
4. **Horizontal scaling is trivial.** Ingest is stateless WebSocket fan-in; sessions key off `rtms_stream_id`. No per-meeting media process on the receive side.

### 1.2 Where RTMS stops

RTMS is documented and confirmed as a **data pipeline** — "gives your app access to live audio, video, and transcript data." Every message type in the protocol is either inbound media or bidirectional *control*:

- Signaling control: `1–13, 19–22, 28, 29` (handshake, keep-alive, stream state, subscription)
- Inbound media: `14–18` (audio, video, share, transcript, chat)
- Inbound metadata: `23–27`

**There is no message type that carries application media toward Zoom.** The only frames we ever send are handshakes, `CLIENT_READY_ACK`, and `KEEP_ALIVE_RESP`. Zoom's own guidance is explicit that an RTMS app cannot speak, post chat, or otherwise interact in real time; a meeting bot is the stated path for that.

**Conclusion:** RTMS answers PoC goals 1–4. It cannot answer goal 5 (publish). Anything claiming otherwise would be an invented API.

---

## 2. The Publish Path — Options Evaluated

Four candidates were assessed. Only one survives.

### Option A — Zoom Meeting SDK for Linux, raw data send ✅ **CHOSEN**

Officially documented raw-data send surface:

**Video**
- `GetRawdataVideoSourceHelper()` → `IZoomSDKVideoSourceHelper`
- `setExternalVideoSource(IZoomSDKVideoSource* source)`
- Implement `IZoomSDKVideoSource`: `onInitialize(IZoomSDKVideoSender* sender, ...)`, `onStartSend()`
- Push frames via `sender->sendVideoFrame(...)`, **YUV420 (I420)**, conforming to `support_cap_list`

**Audio**
- `IZoomSDKAudioRawDataHelper::setExternalAudioSource(...)` — a *virtual microphone*
- Implement the virtual-mic event interface; feed **PCM**

The bot joins as an ordinary participant. Its camera is our avatar video, its microphone is our avatar audio. To every other participant it is simply a person on the call. This is Zoom's sanctioned mechanism and the foundation of every commercial meeting-bot product.

**The cost:** the Linux Meeting SDK is a **C++** SDK. Zoom publishes no official Python binding. This is the dominant engineering constraint on the whole project (§3).

### Option B — Zoom Video SDK ❌

Video SDK genuinely supports bidirectional custom media, and RTMS supports Video SDK sessions. But **Video SDK sessions are not Zoom Meetings.** A Video SDK client cannot join a regular `zoom.us/j/<id>` meeting that ordinary users are in. If the target is real Zoom Meetings — which the RTMS `meeting.rtms_started` webhook premise implies — this option does not apply. Viable only if the product pivots to Zoom-powered custom sessions.

### Option C — Meeting SDK Web in headless Chromium + virtual devices ❌

Works, in the way that duct tape works. PulseAudio null sinks, `v4l2loopback`, one browser per meeting. CPU cost per session is high, failure modes are opaque, A/V sync is unmanageable, and it is not a production posture. Rejected.

### Option D — Third-party Python bindings (`py-zoom-meeting-sdk`) ⚠️ Tactical only

nanobind bindings over the official C++ SDK. Attractive because it keeps everything in Python. But it is **community-maintained, not Zoom-official**, and pins to specific SDK versions. Given the standing instruction to use only officially documented Zoom SDKs, it is not the production recommendation. It is however a legitimate accelerator for a PoC spike, and the design keeps it swappable (§3.3).

---

## 3. Resolving the C++/Python Boundary

The bridge must be Python (FastAPI/asyncio, per requirements). The publisher must be C++ (Meeting SDK). These are reconciled with a **sidecar process**, not an in-process binding.

### 3.1 Topology

```
┌────────────────────────────────────────────┐   ┌───────────────────────┐
│  bridge (Python 3.12, asyncio)             │   │ zoom-bot (C++)        │
│                                            │   │                       │
│  RTMS subscriber ──► MediaRouter ──► Pub ──┼──►│ UDS ──► VideoSource   │
│         ▲                  │      Adapter  │   │        VirtualMic     │
│         │                  ▼               │   │           │           │
│    Zoom RTMS         AvatarClient (WS)     │   └───────────┼───────────┘
└────────────────────────────────────────────┘               ▼
                                                        Zoom Meeting
```

### 3.2 Why a sidecar rather than a Python binding

- **Crash isolation.** The Meeting SDK is a large native library with a documented history of crashes under specific conditions. A segfault must not take down the FastAPI process, the RTMS sockets, or other sessions.
- **Lifecycle independence.** A bot restart/rejoin should not drop RTMS ingest, which is separately recoverable.
- **The GIL.** Video pacing at 25–30 fps alongside 20 ms audio framing wants a native thread that never contends with the event loop.
- **Constraint compliance.** "No platform-specific code outside `connectors/zoom`" is satisfied structurally: all C++ and all Zoom SDK surface lives in `connectors/zoom/sidecar/`, behind a Python `Protocol`.

### 3.3 The seam that makes this safe

```python
class MediaPublisher(Protocol):
    async def start(self, meeting: MeetingCredentials) -> None: ...
    async def publish_video(self, frame: VideoFrame) -> None: ...  # I420
    async def publish_audio(self, frame: AudioFrame) -> None: ...  # PCM
    async def stop(self) -> None: ...
```

Three implementations, one interface:

| Implementation | Purpose |
|---|---|
| `SidecarPublisher` | Production. UDS → C++ Meeting SDK bot. |
| `FileSinkPublisher` | **PoC day 1.** Writes I420 + PCM to disk / muxes to MP4 for eyeball verification. Requires no Zoom entitlement, no meeting, no SDK build. |
| `NullPublisher` | Load and latency testing; counts and timestamps frames, discards payload. |

This is what lets goals 1–4 and 6 be proven **before** the C++ bot exists. It de-risks the schedule: the Python bridge is fully testable against a real Zoom meeting on day one, with the avatar's output verified by watching a file.

### 3.4 IPC wire format (Unix domain socket, `SOCK_STREAM`)

Length-prefixed binary. No JSON, no base64, in the media path.

```
┌────────┬────────┬────────────┬──────────┬─────────────┐
│ magic  │ type   │ pts_us     │ length   │ payload     │
│ u32    │ u8     │ i64        │ u32      │ bytes       │
└────────┴────────┴────────────┴──────────┴─────────────┘
```

`type`: `1=VIDEO_I420, 2=AUDIO_PCM, 3=CONTROL_JOIN, 4=CONTROL_LEAVE, 5=HEARTBEAT`.
`pts_us` is on the **shared media clock** (§7.1) — this is what makes A/V sync possible at the SDK boundary.

---

## 4. Authentication Flow

Two independent credential paths. They are not interchangeable.

### 4.1 RTMS (ingest)

**a. Webhook validation.** Zoom cryptographically signs every webhook; production apps must verify before processing. Zoom's URL-validation challenge and the `x-zm-signature` header are both handled, using `hmac.compare_digest` — never `==`.

**b. Handshake signature.** Per the protocol spec:

```
signature = HMAC_SHA256(
    key     = ZOOM_CLIENT_SECRET,
    message = f"{ZOOM_CLIENT_ID},{meeting_uuid},{rtms_stream_id}",
).hexdigest()
```

The same signature is presented on both the signaling and media handshakes.

**c. Scopes.** RTMS scopes on a Server-to-Server OAuth or General app, with the `meeting.rtms_started` / `meeting.rtms_stopped` event subscriptions enabled.

### 4.2 Meeting SDK (publish)

Entirely separate: a **Meeting SDK JWT**, signed with the Meeting SDK app's key/secret, containing `appKey`, `sdkKey`, `mn` (meeting number), `role: 0`, `iat`, `exp`, `tokenExp`.

**Signed in Python, never in C++.** The sidecar receives a short-lived JWT over the `CONTROL_JOIN` message. Secrets therefore live in exactly one process, and the C++ binary holds no long-lived credential.

On raw-data entitlement: Zoom developer support states there is no separate raw-data license and the SDK exposes raw data as-is; `HasRawdataLicense()` is nonetheless checked at bot startup and surfaced as a loud, actionable structured-log error rather than a silent no-video failure. Verified against the account before the C++ milestone begins.

---

## 5. Join Flow

RTMS ingest and bot join are **independent, concurrently-initiated** state machines. Neither blocks the other; each recovers separately.

```
meeting.rtms_started webhook
        │
        ├─ verify signature ──► reject on failure
        ├─ parse: meeting_uuid, rtms_stream_id, server_urls
        │
        ▼
  SessionManager.create(meeting_uuid)
        │
        ├──────────────────────────┬───────────────────────────┐
        ▼                          ▼                           ▼
  RTMS subscriber            AvatarClient                Publisher
        │                          │                           │
  connect server_urls        connect WS to agent         CONTROL_JOIN → sidecar
  ─► msg_type 1 (sig)        ─► await ready              ─► bot joins meeting
  ◄─ msg_type 2 + media_server                           ─► register video source
  connect media_server.server_urls.all                   ─► register virtual mic
  ─► msg_type 3 (media_params)                           ─► await onStartSend
  ◄─ msg_type 4 status_code 0
  ─► msg_type 7 (signaling) START_MEDIA
        │
        ▼
  ◄─ msg_type 14 MEDIA_DATA_AUDIO  ← flowing
```

Session reaches `ACTIVE` when ingest is streaming **and** the publisher reports ready. Partial readiness is a first-class, observable state — not a hang.

---

## 6. Audio Receive Flow

### 6.1 Subscription

```json
{
  "msg_type": 3,
  "protocol_version": 1,
  "meeting_uuid": "...",
  "rtms_stream_id": "...",
  "signature": "...",
  "media_type": 1,
  "payload_encryption": false,
  "media_params": {
    "audio": {
      "content_type": 2,      // RAW_AUDIO
      "sample_rate": 1,       // SR_16K   ← matches avatar exactly
      "channel": 1,           // MONO
      "codec": 1,             // L16 (PCM)
      "data_opt": 2,          // AUDIO_MULTI_STREAMS (see §6.3)
      "send_rate": 20         // ms — minimum, lowest latency
    }
  }
}
```

`media_type: 1` (AUDIO) only. We deliberately do **not** subscribe video, screen share, or chat — unrequested media is pure latency and bandwidth cost.

`send_rate: 20` is a deliberate choice. The samples commonly show `100`; that is 80 ms of avoidable latency donated at the very first hop. The protocol permits 20 ms increments, so we take the floor.

### 6.2 Decode path

`MEDIA_DATA_AUDIO (14)` arrives as JSON with base64 `content`. Per frame:

1. `orjson` parse (measurably faster than stdlib at this rate)
2. `base64.b64decode` → raw `L16` bytes
3. Wrap in `AudioFrame(pcm, pts_us, user_id)` — **no copy, no conversion, no resample**
4. Hand to `MediaRouter`

At 16 kHz/mono/20 ms this is 640 bytes of payload per frame, 50 frames/sec/participant. Trivial load; the JSON+base64 envelope costs more than the audio itself, which is a protocol fact we accept.

### 6.3 The echo/feedback problem — and why `AUDIO_MULTI_STREAMS`

**This is the failure mode most likely to be discovered late and painfully.**

The bot publishes avatar audio into the meeting. Zoom mixes it. RTMS `AUDIO_MIXED_STREAM` would then deliver the avatar's own voice straight back to us, we forward it to the avatar, and the avatar responds to itself. An infinite feedback loop, on the happy path.

Mitigation, layered:

1. **`data_opt: AUDIO_MULTI_STREAMS (2)`** — per-participant tracks. Drop every frame whose `user_id` is the bot's own participant ID (learned from the SDK on join and pushed back to the router). This is the primary, structural fix.
2. **Speaking gate** — while the publisher is actively emitting avatar audio, apply a router-level gate with a short hangover (~200 ms). Defends against mixed-stream fallback and any `user_id` attribution gap.
3. **Never fall back to mixed stream silently.** If `AUDIO_MULTI_STREAMS` is unavailable, the session logs a warning and engages the gate in strict mode.

---

## 7. Media Pipeline

```
 RTMS audio (PCM 16k mono, 20ms)
        │
        ▼
 ┌──────────────┐   echo gate + own-user filter
 │ MediaRouter  │   bounded queue, drop-oldest
 └──────┬───────┘
        ▼
 ┌──────────────┐   WebSocket, binary frames
 │ AvatarClient │   backpressure-aware
 └──────┬───────┘
        │  streaming fragmented MP4  ◄── the interesting part
        ▼
 ┌──────────────┐   ffmpeg / PyAV, incremental demux
 │ Mp4Decoder   │   → I420 frames + PCM frames, both stamped
 └──────┬───────┘
        ▼
 ┌──────────────┐   monotonic clock, exact-rate emission
 │  Pacer       │   video @ fps, audio @ 20ms
 └──────┬───────┘
        ▼
 ┌──────────────┐
 │ Publisher    │ ──UDS──► C++ bot ──► Zoom
 └──────────────┘
```

### 7.1 Shared media clock

Zoom's send-audio and send-video paths are separate, and A/V desync there is a known, reported class of problem. Sending "as decoded" guarantees drift, because decode completion time has nothing to do with presentation time.

Therefore: at session start the router captures `t0 = time.monotonic_ns()`. Every decoded frame carries the **PTS the decoder assigned it**, rebased onto `t0`. The pacer releases a frame when `now >= t0 + pts`, and the same `pts_us` crosses the IPC boundary. Audio and video are paced from one clock, not two.

Frames arriving more than one period late are dropped (video) or gap-filled with silence (audio) rather than emitted in a burst. Bursting is what turns a small hiccup into visible desync that never recovers.

### 7.2 Buffering policy

"Buffer minimally" is a requirement, so buffers are explicit, small, and bounded — with a stated drop policy rather than unbounded growth:

| Stage | Depth | On overflow |
|---|---|---|
| RTMS → router | 50 frames (1 s) | drop oldest |
| Router → avatar WS | 25 frames (500 ms) | drop oldest + warn |
| Decoder → pacer (video) | 3 frames | drop oldest |
| Decoder → pacer (audio) | 10 frames (200 ms) | drop oldest |
| Pacer → publisher | 2 frames | block briefly, then drop |

Every drop increments a labeled counter. Silent drops are how latency bugs become unfalsifiable.

---

## 8. MP4 Handling — The Critical Assumption

### 8.1 The hard requirement

**A plain MP4 cannot be decoded while streaming.** Standard `mp4` places the `moov` atom — the index — at the end of the file. A decoder cannot start until it has that atom, i.e. until the avatar stops speaking. That would defeat the entire purpose.

The stream **must** be **fragmented MP4 (fMP4/CMAF)**:
- `ftyp` + `moov` (init segment) sent **first**
- then a sequence of `moof` + `mdat` fragments, each independently decodable

In ffmpeg terms the avatar must emit:
```
-movflags +frag_keyframe+empty_moov+default_base_moof
```

**Action required:** confirm with the avatar team. This is Assumption A1 (§12.1) and the top project risk. If the avatar emits non-fragmented MP4, streaming decode is impossible and the avatar side must change — no amount of bridge cleverness fixes it.

### 8.2 Decoder strategy

Two implementations behind one `Protocol`, selected by config:

**`FfmpegMp4Decoder` (PoC default).** One `ffmpeg` subprocess per session, fMP4 on `stdin`, two outputs:
- video → `-f rawvideo -pix_fmt yuv420p` (already the I420 the SDK wants)
- audio → `-f s16le -ar 32000 -ac 1` (resampled once, to the publish rate)

Robust, battle-tested, handles malformed input without taking down the process. Costs one process and roughly a frame of extra latency.

**`PyAvMp4Decoder` (evaluated alternative).** In-process libav via PyAV, `av.open(stream, mode='r')` on a non-seekable file-like object. Lower latency, direct PTS access, no subprocess. But it decodes on the event loop thread unless carefully offloaded, and a malformed fragment raises inside our process.

Recommendation: ship `FfmpegMp4Decoder` for the PoC, measure, and switch only if the latency budget demands it. The `Protocol` makes that a one-line config change.

### 8.3 Format conversion summary

| Hop | Format | Conversion |
|---|---|---|
| Zoom → RTMS | PCM L16 16 kHz mono | — |
| RTMS → Avatar | PCM L16 16 kHz mono | **none** ✅ |
| Avatar → Bridge | fMP4 (H.264 + AAC) | — |
| Decoder → video out | I420 | H.264 decode |
| Decoder → audio out | PCM 32 kHz mono | AAC decode + resample |
| Bridge → Zoom | I420 + PCM | — |

The exact sample rate the virtual mic wants is confirmed against the SDK headers during the C++ milestone; the resample target is a single config value, not a hardcoded constant.

---

## 9. Latency Budget

Additive, one-way, participant-speech → avatar-visible. Bridge-controlled hops marked ●.

| Hop | Expected | Notes |
|---|---|---|
| Zoom capture → RTMS media socket | 100–300 ms | Zoom-internal. Not controllable. |
| ● `send_rate` framing | 20 ms | Floor of the protocol. Would be 100 ms at sample defaults. |
| ● Router + echo gate | < 2 ms | In-memory, zero-copy. |
| ● Bridge → Avatar WS | 2–20 ms | Same-host UDS/loopback ideal; LAN acceptable. |
| Avatar processing | 200–800 ms | **Not ours.** Dominant term. First-fragment latency is what matters. |
| ● fMP4 demux (first fragment) | 20–60 ms | Fragment duration dependent. Ask avatar for short fragments. |
| ● Pacer | 0–40 ms | One frame period at 25 fps. |
| ● IPC → sidecar | < 2 ms | UDS, binary, length-prefixed. |
| Meeting SDK encode + upload | 100–200 ms | Zoom-internal. |
| Zoom distribution to participants | 100–200 ms | Zoom-internal. |

**Total: ≈ 550 ms (best) → 1.65 s (typical-worst).**
**Bridge-attributable: ≈ 45–125 ms** — under 10% of the total.

The honest conclusion: this architecture is not the latency bottleneck. Zoom's own two-way pipeline (~300–700 ms) and the avatar's time-to-first-fragment (200–800 ms) dominate. Optimization effort belongs there, and the PoC's instrumentation is designed to *prove* that attribution rather than assert it.

**Measurement method.** Every frame carries a correlation ID and per-hop monotonic timestamps, emitted as structured log events. A `/metrics` endpoint exposes per-hop histograms (p50/p95/p99). Goal 6 is answered with measured numbers from a real meeting, not estimates from this table.

---

## 10. Failure Recovery

| Failure | Detection | Recovery |
|---|---|---|
| RTMS signaling drop | WS close / keep-alive timeout | Reconnect with exponential backoff + jitter. **Note: RTMS sessions cannot be resumed** — audio during the gap is permanently lost. Log the gap duration explicitly. |
| RTMS media drop | WS close | Re-handshake media socket; signaling socket retained if healthy. |
| Missed `KEEP_ALIVE_REQ (12)` | No `13` sent within window | Zoom drops us. Watchdog treats a missed response as fatal and forces reconnect. |
| Avatar WS drop | close / ping timeout | Reconnect with backoff. Publish a hold-frame (last frame or idle loop) so the avatar doesn't freeze on a stale image. |
| Avatar backpressure | send queue high-water | Drop oldest audio, increment counter, warn. **Never block the RTMS reader** — blocking there causes Zoom-side drops. |
| Decoder crash / bad fragment | subprocess exit / stderr | Restart decoder, re-request init segment, hold last video frame. |
| Sidecar crash (segfault) | UDS EOF / process exit | Supervisor restarts, re-issues `CONTROL_JOIN` with a fresh JWT. Ingest is unaffected — this is the payoff of §3.2. |
| Bot ejected / meeting ended | SDK callback | Tear down session cleanly, release resources, no reconnect storm. |
| `meeting.rtms_stopped` | webhook | Graceful ordered shutdown: publisher → avatar → RTMS. |

**Cross-cutting rules**
- All reconnects: exponential backoff, full jitter, capped attempts, then terminal-with-alert.
- Idempotent session teardown; every path releases resources exactly once.
- A single session's failure never affects another session (per-session task groups, `asyncio.TaskGroup`).
- No bare `except:`; every handler logs with the session correlation ID.

---

## 11. Project Structure

Matches the requested layout; C++ isolated under the Zoom connector.

```
meeting-connectors/
├── src/
│   ├── connectors/zoom/
│   │   ├── auth.py           # webhook verify, RTMS sig, SDK JWT
│   │   ├── client.py         # signaling+media WS lifecycle
│   │   ├── subscriber.py     # msg 14 → AudioFrame
│   │   ├── publisher.py      # MediaPublisher impls (Sidecar/File/Null)
│   │   ├── session.py        # per-meeting state machine
│   │   ├── bridge.py         # ZoomBridge facade
│   │   ├── models.py         # Pydantic v2: protocol + webhooks
│   │   ├── config.py         # ZoomSettings
│   │   ├── exceptions.py
│   │   └── sidecar/          # ← ALL C++ / Zoom SDK code lives here
│   │       ├── src/*.cpp     #   video source, virtual mic, UDS server
│   │       └── CMakeLists.txt
│   ├── bridge/
│   │   ├── media_router.py   # routing, echo gate, pacing hand-off
│   │   ├── stream_manager.py # bounded queues, drop policy
│   │   ├── session_manager.py# registry, lifecycle, TaskGroups
│   │   └── heartbeat.py      # keep-alive watchdogs
│   ├── avatar/
│   │   ├── protocols.py      # AvatarAgent, Mp4Decoder, MediaPublisher
│   │   ├── websocket_client.py # transport, reconnect, backpressure
│   │   └── avatar_client.py  # PCM out / fMP4 in
│   ├── media/
│   │   ├── mp4_decoder.py    # Ffmpeg + PyAv impls
│   │   ├── pacer.py          # shared media clock
│   │   └── frames.py         # AudioFrame, VideoFrame
│   ├── api/                  # FastAPI: webhooks, health, metrics
│   ├── config/               # Pydantic BaseSettings
│   ├── database/             # session persistence (optional in PoC)
│   └── containers.py         # Dependency Injector wiring
├── tests/
├── docker/
├── docs/design/
└── pyproject.toml
```

**Dependency rule:** `bridge/`, `avatar/`, and `media/` depend only on `Protocol` definitions — never on `connectors/zoom`. Zoom is a plug-in, satisfying DIP and making a future Teams/Meet connector a matter of addition, not modification.

---

## 12. Assumptions & Open Questions

### 12.1 Blocking — needed before the media pipeline is finalized

| # | Assumption | Impact if wrong |
|---|---|---|
| **A1** | Avatar emits **fragmented** MP4 (`moof`/`mdat`, `empty_moov`) | 🔴 **Streaming decode impossible.** Avatar side must change. Highest risk. |
| **A2** | Avatar WS protocol: binary PCM in, binary fMP4 out, with a defined framing/turn signal | 🟠 Client needs rework; PoC assumes binary frames + a documented end-of-utterance marker. |
| **A3** | Target is **real Zoom Meetings**, not Video SDK sessions | 🟠 Publish path changes entirely (Option B becomes correct and much simpler). |

### 12.2 Non-blocking — resolved during implementation

| # | Question |
|---|---|
| B1 | Meeting SDK virtual-mic exact sample rate — read from SDK headers; currently a config value. |
| B2 | `support_cap_list` resolution/fps the account permits — probed at bot init. |
| B3 | Bot's own `user_id` correlation between SDK and RTMS `user_id` space — verified empirically; §6.3 layer 2 covers the gap. |
| B4 | Whether the avatar needs an explicit turn/VAD signal or handles continuous audio itself. |

### 12.3 Known limitations of this PoC

- Single meeting per bot process. Multi-meeting needs a sidecar pool.
- RTMS gaps are unrecoverable by protocol design — audio lost during reconnect is lost.
- No acoustic echo cancellation; relies on stream separation + gating (§6.3).
- Screen share, chat, and participant video are deliberately out of scope.
- Meeting SDK for Linux requires x86_64 and specific glibc/ALSA/PulseAudio deps — hence the container.

---

## 13. Implementation Plan

**Milestone 1 — Skeleton.** Poetry, Ruff, pytest, Docker, DI container, config, structured logging, FastAPI health. *Exit: container boots, `/health` green, lint+tests pass.*

**Milestone 2 — RTMS ingest.** Webhook verify, signaling+media handshake, keep-alive, `msg 14` → `AudioFrame`. *Exit: **PoC Goal 1 proven** — real audio from a real meeting, logged with frame counts and timing.*

**Milestone 3 — Avatar client.** WS transport, reconnect, backpressure, PCM forwarding. Mock avatar server for tests. *Exit: **Goal 2 proven**.*

**Milestone 4 — MP4 decode.** fMP4 → I420 + PCM, pacer, shared clock. `FileSinkPublisher` writes a playable file. *Exit: **Goals 3 & 4 proven** — the avatar's response is watchable.*

**Milestone 5 — Publish.** C++ sidecar, UDS server, video source + virtual mic, `SidecarPublisher`. *Exit: **Goal 5 proven** — avatar visible and audible in Zoom.*

**Milestone 6 — Latency & hardening.** Per-hop histograms, chaos tests on every §10 row. *Exit: **Goal 6 answered with measurements**.*

Milestones 1–4 need **no Zoom SDK build and no entitlement** — four of six goals are provable while the C++ dependency is still being sorted. That sequencing is deliberate.

---

## 14. Approval Gate

Requested decisions:

1. ✅/❌ **Two-technology architecture** — RTMS ingest + Meeting SDK publish
2. ✅/❌ **C++ sidecar over Python bindings** (§3.2), or accept `py-zoom-meeting-sdk` for PoC speed
3. ✅/❌ **`AUDIO_MULTI_STREAMS` + gating** for echo control (§6.3)
4. ✅/❌ **`FfmpegMp4Decoder` first**, PyAV as optimization
5. ⚠️ **Confirm A1** — is the avatar's MP4 fragmented?
6. ⚠️ **Confirm A3** — real Zoom Meetings, not Video SDK sessions?

On approval: Milestone 1 (structure), then module-by-module implementation.

---

## References

- [Realtime Media Streams — Overview](https://developers.zoom.us/docs/rtms/)
- [RTMS — Getting started](https://developers.zoom.us/docs/rtms/meetings/getting-started/)
- [RTMS — Data type definitions](https://developers.zoom.us/docs/rtms/data-types/)
- [RTMS for Video SDK](https://developers.zoom.us/docs/rtms/video-sdk/)
- [zoom/rtms — official SDK (Python/Node/Go)](https://github.com/zoom/rtms)
- [zoom/rtms-samples — protocol flow & media params](https://github.com/zoom/rtms-samples)
- [Zoom Meeting SDK for Linux](https://developers.zoom.us/docs/meeting-sdk/linux/)
- [zoom/meetingsdk-linux-raw-recording-sample](https://github.com/zoom/meetingsdk-linux-raw-recording-sample)
- [Zoom Video SDK for Linux](https://developers.zoom.us/docs/video-sdk/linux/)
- [What is Zoom RTMS? — receive-only constraint](https://www.recall.ai/blog/what-is-zoom-rtms)
- [Streaming video into a meeting via the Zoom SDK](https://www.recall.ai/blog/zoom-sdk-streaming-video-to-meeting)
- [Devforum: syncing send video and send audio](https://devforum.zoom.us/t/syncing-send-video-and-send-audio-in-meeting-sdk/110080)
- [Devforum: raw data entitlement](https://devforum.zoom.us/t/request-to-enable-meeting-sdk-raw-data-entitlement-sending-external-video-audio/144868)
