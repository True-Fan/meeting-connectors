# Low-Level Design

This is the internals reference for `meeting-connectors` itself. Read [HLD.md](HLD.md) first
if you haven't — this doc assumes you know why the system is shaped the way it is and focuses
on how. Platform-specific mechanics (RTMS handshakes, Graph calls, browser injection) live in
the [connector docs](README.md#start-here); this doc covers everything shared, plus how the
per-platform pieces plug into it.

File paths are relative to the repo root; `file.py:12` means "see that file around line 12" —
line numbers drift, the shape doesn't.

## 1. Module map

```
src/
├── api/              HTTP edge. Never imports connectors.
├── config/           Settings (pydantic-settings, MC_ prefix, __ nesting)
├── domain/           Canonical model. Depends on nothing else in src/.
├── protocols/        The ports every connector implements — six of them
├── connectors/       Adapters. The ONLY place a platform SDK/API may appear.
│   ├── zoom/         RTMS ingest · Meeting SDK publish via C++ sidecar (wire frozen)
│   ├── zoom_web/     Browser join · RTMS or page-tapped ingest
│   ├── teams/        Graph app-hosted media via a .NET sidecar on Windows
│   ├── teams_web/    Browser join · page-tapped ingest
│   └── google_meet/  Browser join · page-tapped ingest (Meet has no publish API at all)
├── services/
│   ├── session/      Session state machine, registry, health supervision
│   ├── meeting/      MeetingService (the one entry point) + ConnectorRegistry
│   └── media/        The shared pipeline: router, clock, decoder, pacer, echo guard, sinks
├── avatar/           WebSocket client to the external Streaming Avatar Agent
├── infrastructure/   Logging, metrics, Prometheus rendering, reconnect policy, ambient context
├── containers.py     DI wiring — the only module allowed to import src.connectors
└── main.py           `app = create_app()` — the ASGI entrypoint
```

Enforced (by an architecture test, not just convention): `domain/` imports nothing from
`src`; `protocols/` imports only `domain/`; `api/`, `services/`, `avatar/` never import
`connectors/`; each connector's wire models never leave that connector's package; connectors
never import each other; no relative imports anywhere in `src/`.

## 2. API layer (`src/api/`)

`app.py` — `create_app(container=None)` factory (no module-level `app`, so tests get real
isolation). Mounts `CorrelationIdMiddleware`, then `health`, `metrics`, `sessions`,
`participants` routers, then two connector-owned routers resolved **from the container**
rather than imported (`container.zoom_webhook_router()` at `/webhooks/zoom`,
`container.zoom_oauth_router()`) — so `api/` never names a connector even though one of its
mounted routes is Zoom-specific. Lifespan: on shutdown, drains every session
(`meeting_service().stop_all()`) so a redeploy never leaves a bot talking to an empty room.

### `routers/sessions.py` — lifecycle (prefix `/sessions`)

| Method | Path | Returns | Does |
|---|---|---|---|
| `POST` | `/sessions` | 202 | Builds a `CreateSessionCommand`, calls `MeetingService.create_session`. 409 if the meeting is already joined or the platform isn't registered. |
| `GET` | `/sessions` | 200 | `SessionListResponse{sessions, total}` |
| `GET` | `/sessions/{id}` | 200/404 | Session + its `HealthReport`, as `SessionDetailResponse` |
| `DELETE` | `/sessions/{id}` | 200/404/500 | Tears the session down |

**Request body** (`dto.py::CreateSessionRequest`):

```jsonc
{
  "meeting_number": "1234567890",   // required; also accepts a full join URL for Teams
  "passcode": "abc123",             // optional
  "display_name": "AI Avatar",      // optional
  "platform": "zoom",               // MeetingPlatform enum; defaults to "zoom"
  "meeting_url": null,               // Teams/Meet/Zoom-web use this instead of/with meeting_number
  "meeting_uuid": null               // Zoom only — pins an inbound RTMS stream to this exact session
}
```

`SessionResponse` reports `audio_attached` — platform-dependent: for Zoom, `True` only once
`meeting_uuid` is bound (i.e. RTMS attached); for everything else, `state.is_running`.

### `routers/participants.py` — attendance/speakers/transcript (also `/sessions` prefix)

Pull-only by design: the only channel to the avatar is chat, and everything sent on it is
spoken aloud, so a live roster push would make the avatar narrate every join/leave.

| Method | Path | Returns |
|---|---|---|
| `GET` | `/sessions/{id}/participants` | present/departed/never-joined, plus an `agent_context` prose brief. 404 if the connector keeps no ledger. |
| `GET` | `/sessions/{id}/speakers` | current speaker, talk-time, turn history |
| `GET` | `/sessions/{id}/transcript` | attributed lines from captions/chat |
| `POST` | `/sessions/{id}/invitees` | seed an invite list (additive, idempotent) so "who never showed up" is answerable |

These are duck-typed against whatever the connector happens to expose (`getattr(session,
"attendance", None)`) rather than widened onto the `ConnectorSession` protocol — today only
the browser connectors implement them.

### `routers/health.py`, `routers/metrics.py`

`GET /health` reports **process** health (empty report ⇒ healthy), not an aggregate of every
session — one bad session must never make the whole service look unready. `GET /metrics` is
Prometheus text, aggregated, no unbounded labels (`correlation_id` is deliberately never a
metric label). `GET /metrics/sessions/{id}` gives per-session detail including the
correlation id, for debugging one call.

### `middleware.py`, `dependencies.py`

`CorrelationIdMiddleware` reads/mints `X-Correlation-ID`, binds it to ambient context for the
request, echoes it on the response. `dependencies.py` resolves everything from
`request.app.state.container` via plain functions (not `dependency_injector`'s `@inject`
decorator) so the wiring stays visible at each call site.

## 3. Domain model (`src/domain/`)

- **`session.py`** — `SessionState`: `CREATED → JOINING → {ACTIVE ⇄ DEGRADED} → STOPPING →
  STOPPED`, or `→ FAILED` from anywhere non-terminal. `derive_state(ingest, publish)`: both
  healthy ⇒ `ACTIVE`; either impaired while running ⇒ `DEGRADED`; either `UNKNOWN` ⇒ stays
  `JOINING`. `SessionContext` is the **one mutable domain object** — session id, correlation
  id, `MeetingContext`, state, timestamps, an error log.
- **`meeting.py`** — `MeetingPlatform`: `ZOOM, TEAMS, GOOGLE_MEET, ZOOM_WEB, TEAMS_WEB`
  (identity only — no URLs, ports, or SDK hints leak into this enum). `MeetingContext` bundles
  the join parameters; `.with_uuid()` returns a copy bound once Zoom's webhook arrives.
  `ParticipantRef(user_id, display_name)` is what `EchoGuard` matches the avatar's own audio
  against. `ChatMessage` and `HandRaise` are kept as distinct types on purpose: a chat message
  waits its turn, a raised hand interrupts.
- **`media.py`** — the shape contract. `SampleFormat.S16LE` and `PixelFormat.I420` are
  currently the *only* members of their enums (Zoom Meeting SDK's required formats).
  `AudioFormat`/`VideoFormat` validate positive (and, for video, even — I420 needs 2×2 chroma
  subsampling) dimensions. `AudioFrame`/`VideoFrame`/`MediaChunk` are the frame types that
  flow through every queue in the system; each carries a `FrameContext` rather than duplicating `session_id`/`correlation_id`.
- **`avatar.py`** — the fixed contract with the external agent. `AVATAR_INPUT_FORMAT` = PCM
  16 kHz mono S16LE. `AvatarProtocolVersion` compatibility rule: **major must match exactly**,
  minor is additive. Current version `1.2`; chat needs `≥1.1`; meeting-context pushes need
  `≥1.2`. `check_handshake()` returns the negotiated (minimum) version or raises
  `AvatarProtocolMismatchError` — the one avatar-side error that is never treated as
  recoverable-by-reconnect.
- **`health.py`** — `ComponentState`: `UNKNOWN < DEGRADED < HEALTHY`, `UNHEALTHY` worst.
  `HealthReport` aggregates worst-of; an empty report is `HEALTHY`.
- **`ids.py`** — `SessionId`/`CorrelationId` as distinct string types; `ses_<uuid4hex>` /
  `cor_<uuid4hex>`.
- **`exceptions.py`** — `DomainError` → `InvalidFrameError`, `IllegalStateTransitionError`,
  `AvatarProtocolMismatchError`.

## 4. Protocols — the six ports (`src/protocols/`)

Every connector implements a subset of these; nothing outside `connectors/` and
`containers.py` cares which platform is behind them.

| Port | Contract | Who supplies it |
|---|---|---|
| `ConnectorSession` / `ConnectorSessionFactory` | `start()`/`stop()` (idempotent), `health()`, `leg_states() -> (ingest, publish)`. Factory's `build(session)` must not do I/O. | One pair per connector |
| `AudioSource` | `async frames() -> AsyncIterator[AudioFrame]` — an async-iterator, not a callback, so backpressure is structural | Every connector |
| `MediaSink` | `publish_audio/publish_video(frame)`, `own_participant() -> ParticipantRef \| None` (feeds `EchoGuard`) | Every connector |
| `ChatSource` | `messages() -> AsyncIterator[ChatMessage]` — optional; chat must never be dropped, unlike audio | Google Meet, Zoom-web, Teams-web |
| `HandRaiseSource` | `events() -> AsyncIterator[HandRaise]` — optional, and kept separate from chat because a raised hand **interrupts**, a chat message waits | Google Meet, Zoom-web, Teams-web |
| `AvatarTransport` / `MediaDecoder` | The avatar-side legs — see §7 | Shared implementation, not connector-specific |

A protocol earned its place only once a **second implementation existed** — this is why
`ConnectorSession` didn't exist until Teams needed to satisfy the same shape Zoom already
had, and why `ZoomMeetingSession` needed zero edits to become compatible: `Protocol`
structural typing means "satisfies the interface" is a fact about the class, not a declared
relationship.

## 5. Session services (`src/services/session/`)

### State machine

```
CREATED --(MeetingService.create_session)--> JOINING --(both legs healthy)--> ACTIVE
                                                 |                              |  ^
                                                 |                       (either impaired)
                                                 |                              v  |
                                                 |                          DEGRADED
                                                 |                              |
                                                 +-----(unhealthy > grace_s)----+
                                                 |                              |
                                                 v                              v
                                              FAILED  <---------------------STOPPING --> STOPPED
```

`SessionLifecycle` is the **only** place state changes (`transition()` — no-op on
re-entering the current state, raises on an illegal jump; `apply_health()` — derives the
health-driven transitions, special-casing a component dropping back to `UNKNOWN` after
running as `DEGRADED` rather than `JOINING`, which is unreachable from `ACTIVE`; `fail()` —
records a fatal error and moves to `FAILED`).

`SessionSupervisor` runs one polling task per session (default every 1s), calling only
`ConnectorSession.leg_states()` — it is platform-blind. `_should_fail()` is exempt while
`JOINING` (first-attach latency has its own, longer timeout inside the connector); once
impaired, a grace clock starts and the session only fails after `DEFAULT_UNHEALTHY_GRACE_S`
(60s) of continued impairment — an outer backstop above whatever reconnect logic lives inside
the component itself. **Reconnect is always the component's own job** (e.g.
`AvatarClient.reconnect()` under a `ReconnectPolicy`); the supervisor only judges when a
session is *beyond* recovery.

`SessionRegistry` is in-memory (not persisted), indexed by id, meeting number, and (for
Zoom/Zoom-web) RTMS `meeting_uuid`. It also holds the **join/RTMS race** table: a
`PendingRtmsBinding` can arrive (via webhook) before the session that should claim it exists,
or vice versa. `DEFAULT_PENDING_TTL_S = 45.0`, deliberately under Zoom's own ~60s
auto-teardown window — a documented production incident used 300s and attached sessions to
already-dead streams.

## 6. Meeting service (`src/services/meeting/`)

`MeetingService` is the **single entry point** the API calls; it never touches a media frame.
`create_session()`:

1. Reject if the meeting number is already joined.
2. Resolve the connector factory for `platform` from `ConnectorRegistry` — **before**
   allocating anything, so an unsupported/unconfigured platform fails in one step with a
   named error instead of partway through a join.
3. Build a fresh `SessionContext`/`MeetingContext`.
4. Zoom/Zoom-web only: try to claim any RTMS binding already parked in the registry
   (`_claim_pending_rtms`).
5. Register the session, call `factory.build(session)`, transition to `JOINING`, `await
   connector_session.start()`. Any exception here fails the session and re-raises as a
   service error (→ the API's 409/500).
6. Hand the running session to `SessionSupervisor.supervise()` and return.

`ConnectorRegistry` is a plain `{MeetingPlatform: ConnectorSessionFactory}` lookup table —
`register()` refuses to silently replace an existing entry, `get()` raises a named
`UnsupportedPlatformError` listing what *is* supported. No lifecycle or negotiation logic
lives here; adding a connector is one `register()` call, made from `containers.py`.

Zoom's webhook handler calls back into this service too: `bind_rtms(binding)` looks the
session up by uuid, falls back to "the one session currently waiting for RTMS" if there's
exactly one, or parks the binding if neither exists yet; `handle_rtms_stopped()` tears the
matching session down.

## 7. The shared media pipeline (`src/services/media/`)

This is the part every connector reuses unmodified. The contract it exists to serve: **PCM 16
kHz mono S16LE in, fragmented MP4 out** — asserted, not converted, at the boundary
(`AvatarClient.send` raises rather than silently resampling a mismatched frame).

```
AudioSource.frames() ──▶ EchoGuard ──▶ AvatarClient.send() ──(PCM, WebSocket)──▶ avatar agent
                                                                                      │
                                                                                (fMP4 fragments)
                                                                                      ▼
MediaSink.publish_audio/video() ◀── Pacer (on MediaClock) ◀── DecodePipeline ◀── AvatarClient.chunks()
```

`MediaRouter` runs this as **four concurrent legs inside one task group**, plus the
`Pacer`'s own independent loop (so publishing never stops even when every other leg is idle —
idle media keeps flowing). Direct calls over bounded queues, not an event bus — a fan-out bus
has no coherent backpressure story.

### Building blocks

- **`MediaClock`** — one monotonic clock rooted at session start; every PTS is expressed on
  it. Exists because a connector's ingest and publish are separate paths with real desync
  risk (decode-completion time ≠ presentation time) — RTMS frames get their PTS stamped from
  this clock, never from the platform's own timestamp, for the same reason.
- **`DecodePipeline`** — owns one `MediaDecoder` and its restart policy (max 5 restarts).
  Caches the fMP4 **init segment** (`ftyp+moov`) and replays it on every restart — without
  that, a restarted ffmpeg process runs but emits nothing, i.e. permanently black video with
  no error.
- **`FfmpegDecoder`** — one `ffmpeg` subprocess per session: fMP4 on stdin, raw I420 video on
  stdout, raw PCM on a dynamically-chosen inherited file descriptor (never hardcoded — a past
  bug hardcoded fd 3 and lost audio silently on some platforms). Subprocess isolation is
  deliberate: a malformed fragment kills a restartable child process, not the event loop
  running a live meeting.
- **`EchoGuard`** — stops the avatar hearing itself. Two layers: (1) an own-participant filter
  by `user_id`, precise but needs per-participant attribution; (2) a **speaking gate** — while
  the avatar is audibly publishing, plus a hangover window afterward (default 200ms, tunable
  per connector), drop all inbound audio regardless of attribution. Runs in strict mode
  automatically when no per-participant attribution exists (e.g. a mixed RTMS stream, or any
  browser connector where the avatar's own audio never reaches the tap at all — see the
  per-connector docs for which case applies where).
- **`Pacer`** — releases decoded frames at their PTS (late video is dropped, not
  burst-released; audio, being a continuum with no second copy, is **never** dropped for
  lateness — only queued *silence* is trimmed once backlog exceeds 200ms). Also runs the
  **idle-continuity** loop: when the avatar has nothing queued, `IdleFrameSource` supplies a
  held last-frame / looping clip / silence so the publish leg never stalls. `interrupt(hold_ms)`
  clears both queues and mutes for a bounded window — the mechanism behind every barge-in path
  (see §6.7 below via the router).
- **`BoundedFrameQueue`** — fixed depth, never blocks the producer; `DROP_OLDEST` (prefer
  fresh media — video, avatar-bound audio) or `DROP_NEWEST` (preserve sequence — fMP4
  fragments, where dropping an older one corrupts everything after it). Every drop is counted,
  never silent.
- **`SpeechDetector`** — RMS-energy voice-activity trigger for barge-in on connectors where
  the echo gate can stay open (i.e. where the avatar's own voice is structurally absent from
  what's tapped). Adaptive noise floor, minimum duration, hysteresis, release window — not a
  real VAD, just a threshold plus arithmetic to avoid clipping the room's silence.
- **`FileSink` / `NullSink`** — `FileSink` muxes raw output into a playable file, which is how
  decode/clock/av-sync correctness was proven **before any platform SDK/sidecar existed**.
  `NullSink` counts and discards, for load testing and as the default in unit tests.

### Barge-in unification

A raised hand and a detected voice do the **same** thing: `Pacer.interrupt(hold_ms)` (local,
immediate — drops queued avatar media) followed by `AvatarClient.send_hand_raise(event)`
(tells the agent, over the chat channel, to yield — delivered as a chat frame deliberately, so
it works against an unmodified agent that only understands `chat`). Voice-triggered
interruption only fires while the avatar is actually speaking; a silent avatar has nothing to
interrupt.

## 8. Avatar client (`src/avatar/`) — the contract with the external agent

This is the one WebSocket every connector's `MediaRouter` talks to, and the reason "the
avatar" can be swapped without touching a connector.

**Handshake** — `AvatarClient.start()` sends `AvatarClientHello{session_id, correlation_id,
audio=AVATAR_INPUT_FORMAT, expects_container="fmp4"}` as JSON text; the agent replies
`AvatarServerHello{protocol_version, accepted, container}`. `check_handshake()` enforces exact
major-version match and returns the negotiated (minimum) minor. A version too old to support
chat or meeting-context silently **withholds** that feature (with one warning logged) rather
than erroring — an agent that ignores unknown control frames keeps working either way.

**Steady state** — binary frames both ways: PCM 16kHz mono out (`send_pcm`, via a bounded,
drop-oldest queue so a slow write never stalls the ingest reader), fMP4 fragments in
(`chunks()`, parsed by `Fmp4Framer`). Control frames (`send_control`) — chat, hand-raise
context, meeting-context briefs — go out as JSON text on a **separate path that is never
dropped**, guarded by the same send lock as the PCM writer so frames can't interleave
mid-write.

**`Fmp4Framer`** re-establishes MP4 box structure over arbitrary WebSocket message
boundaries — the agent's stream is not guaranteed to align chunks with box boundaries. Emits
one `is_init_segment=True` chunk for the leading `ftyp…moov`, then one chunk per
`[styp?] moof mdat` fragment. A **non-fragmented** MP4 (moov at EOF) is detected and raised as
an error immediately rather than hanging forever waiting for a box that will never come.

**Reconnect** — `AvatarClient.reconnect()` retries `start()` under a `ReconnectPolicy`
(exponential backoff with full jitter); the cached init segment survives a reconnect so decode
can resume without another cold-start black window. `AvatarProtocolMismatchError` is always
re-raised, never retried — a version mismatch cannot fix itself.

`WebSocketAvatarTransport` is the concrete `AvatarTransport`: `max_size=None` on the socket
(the framer, not the transport, bounds fragment size), 20s ping interval, 10s open timeout by
default.

## 9. Infrastructure (`src/infrastructure/`)

- **`logging.py`** — structlog, JSON in production-like envs, colorized console locally;
  every line gets `session_id`/`correlation_id` injected from ambient context automatically.
- **`metrics.py` / `prometheus.py`** — `MetricsCollector` (single-threaded-asyncio, lock-free
  by that assumption) records bounded-ring-buffer latency histograms (exact percentiles
  computed on snapshot, not on every observation) and counters, keyed by metric name + sorted
  labels. `correlation_id` is deliberately never a Prometheus label (unbounded cardinality
  over time) — it's only available via `/metrics/sessions/{id}`. `prometheus.py` hand-rolls
  the text exposition format rather than depending on `prometheus_client`, to avoid a
  process-global registry.
- **`reconnect.py`** — `ReconnectPolicy(initial_delay_s, max_delay_s, max_attempts,
  multiplier)`, exponential backoff with **full jitter** (a uniform draw, not a fixed
  fraction) to decorrelate simultaneous reconnect storms. Shared by the avatar client, both
  sidecar links, and Google Meet's browser relaunch logic.
- **`context.py`** — `ContextVar`-based ambient session/correlation id, propagated into child
  asyncio tasks automatically, read by the logging processor. Frames never rely on this
  ambient state for correctness (they carry an explicit `FrameContext`, since a frame can
  outlive the task that created it) — it exists purely so call sites don't have to thread ids
  through every function signature.

## 10. Dependency injection (`src/containers.py`)

The **only** module allowed to import `src.connectors` — enforced by an architecture test.
Everything else composes against protocols/domain types. `Container` (a
`dependency_injector.DeclarativeContainer`) wires, per connector, a structurally parallel and
independent block: `<platform>_config` (built via `<Config>.from_settings(settings)`) →
`<platform>_session_factory`. `build_connector_registry()` registers Zoom **unconditionally**
(the production default, preserved exactly as it was before any other connector existed);
every other connector registers only if `settings.<platform>.is_configured()`, and is passed
as a **callable factory** (`providers.Delegate(...)`) rather than a resolved instance — this
defers construction (and config validation) of optional connectors so a malformed optional
config can never appear on Zoom's startup path. A failure building one optional connector is
caught, logged, and the connector is simply absent — never a boot failure.

`main.py` is one line: `app = create_app()`, imported once by `uvicorn src.main:app`.

## 11. Cross-connector wire protocol comparison

Two connectors (Zoom, Teams) reach their platform through a **native SDK behind a sidecar
process**; three (Google Meet, Zoom-web, Teams-web) reach it through a **browser + injected
JS + loopback WebSocket**. Both families converge on the same idea — a small binary header,
platform-specific magic bytes, media as raw payload, JSON for control/observations — arrived
at independently each time.

| | Zoom sidecar (`zoom/publisher/protocol.py`) | Teams sidecar (`teams/sidecar/protocol.py`) | Browser bridges (`*/page/protocol.py`, `google_meet/websocket/protocol.py`) |
|---|---|---|---|
| Transport | Unix domain socket, same container | TCP + TLS (optionally mTLS), separate Windows host | Loopback TCP WebSocket, same container, ephemeral port |
| Magic | `ZMC1` | `TMC1` | `GMC1` (Meet) / `ZWB1` (Zoom-web) / `TWB1` (Teams-web) — deliberately distinct so a captured frame names its origin |
| Header | 24 bytes, big-endian: magic, version, type, flags, reserved, seq(u32), pts_us(i64), length(u32) | identical shape | identical shape |
| Media direction | Sidecar receives audio+video (publish only — Zoom ingest is a separate RTMS socket) | **Bidirectional** on one link — same `AUDIO_PCM` type used both ways | Bidirectional; audio only (video egress exists only for Google Meet) |
| Framing errors | Fatal, no resync — link torn down and rebuilt | Same | Same |
| Backpressure | Video dropped over a size threshold (~4MB queued); audio always drains | Same policy | Same policy |
| Auth | SDK JWT minted in Python, handed over inside `CONTROL_JOIN`; sidecar holds no long-lived credential | Azure AD client secret forwarded inside `CONTROL_JOIN`; TLS/mTLS on the transport | Per-session random token, compared with `hmac.compare_digest`, checked before the WS handshake completes |

Every one of these codecs is deliberately hand-rolled rather than using an existing framing
library — each is small enough to fit in one file, and a byte-for-byte match between the two
sides (Python ⇄ C++, Python ⇄ C#, Python ⇄ JS) is exactly the kind of thing worth pinning with
a direct conformance test rather than trusting a shared dependency to stay in sync.

## 12. Testing seams

Every external dependency has an in-repo double that speaks its real wire protocol, which is
what lets each connector be built and verified before its real infrastructure exists:

| Real thing | Stand-in |
|---|---|
| Streaming Avatar Agent | `FakeAvatarTransport` (in-process); `FileSink`/`NullSink` on the publish side make output watchable/countable without any platform SDK |
| Zoom Meeting SDK (C++) | The sidecar builds in a **stub SDK mode** (`MC_WITH_ZOOM_SDK` unset) that speaks the real IPC/framing/threading/pacing logic without linking the real SDK |
| Teams Graph + Media SDK (.NET) | An in-process fake sidecar (`tests/fakes/teams_sidecar.py`) speaking the real wire protocol; the wire codec itself is pinned by a direct Python↔C# conformance test |
| A real browser | `BrowserDriver` is a `Protocol`; tests substitute a fake driver, so join/selector/observer logic is exercised without Playwright installed |
| Zoom RTMS | `JsonWebSocket` is a `Protocol` seam; the handshake state machine is tested against an in-memory fake |

This is also why the top-level README's status table can honestly say things like "IPC and
control flow verified against a stub; real SDK build is a separate milestone" — the protocol
layer and the platform-integration layer are verified separately, on purpose.

## 13. Adding a sixth connector

1. New package under `src/connectors/<name>/`.
2. Implement `ConnectorSession`/`ConnectorSessionFactory` (a `Protocol`, so no base class to
   subclass) plus whichever of `AudioSource`/`MediaSink`/`ChatSource`/`HandRaiseSource` the
   platform can supply.
3. Add one `MeetingPlatform` enum member (`src/domain/meeting.py`) — identity only, no
   platform-specific fields.
4. In `containers.py`: one `<name>_config` provider, one `<name>_session_factory` provider,
   one line in `build_connector_registry()` using `_register_optional` (unless it should be
   unconditional like Zoom).
5. Nothing in `src/services/`, `src/api/`, or `src/avatar/` changes. This has held for every
   connector added after the first.
