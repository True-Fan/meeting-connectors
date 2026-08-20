# meeting-connectors

Meeting integration service connecting AI streaming avatar agents with Zoom, Microsoft Teams, and Google Meet through a unified media bridge architecture.

**Zoom and Microsoft Teams are implemented.** Google Meet remains a documented roadmap — see [doc 002](docs/design/002-multi-platform-architecture-review.md).

The avatar joins a meeting as an ordinary participant: it hears the humans, speaks, and shows generated video. This service is only the bridge — it contains no AI, and the existing Streaming Avatar Agent is never modified.

```
Human speaks → platform → bridge → Avatar Agent → fMP4 → decode → platform → meeting
```

The avatar contract is fixed and identical for every connector: **PCM 16 kHz mono in, fragmented MP4 out**. Both platforms happen to deliver audio in exactly that format, so nothing resamples on either ingest path — a checked invariant, not a coincidence.

### Connectors

| | Receive | Publish | Media runtime |
|---|---|---|---|
| **Zoom** | **RTMS** (WebSocket, Python) | **Meeting SDK for Linux** | C++ sidecar, Unix socket, same container |
| **Teams** | **Graph app-hosted media** — one session, both directions | | .NET sidecar, **Windows host**, TCP + TLS |

The shapes differ because the platforms do. Zoom has two independent integrations that fail and recover separately (RTMS cannot publish media — [doc 001 §1](docs/design/001-zoom-avatar-bridge.md)). Teams has one `LocalMediaSession` carrying both directions, and it runs only on Windows/.NET — so its media runtime is a separate deployment unit, not a sibling process ([doc 005 §1](docs/design/005-teams-connector-architecture.md)).

Everything between the two — avatar client, decoder, pacer, echo suppression, media clock, session lifecycle — is shared, unmodified, written once for Zoom.

## Status

| Layer | State |
|---|:--|
| Session API, lifecycle, health, metrics | ✅ |
| Shared media pipeline — router, decoder, pacer, echo guard, clock | ✅ |
| Avatar leg — send PCM, receive streamed fMP4 | ✅ |
| **Zoom** ingest (RTMS) | ✅ built and verified against Zoom's real handshake |
| **Zoom** publish (Meeting SDK, C++ sidecar) | ✅ IPC and control flow verified against a stub; real SDK build is a separate milestone |
| **Teams** connector — bridge side | ✅ built and tested against an in-process fake sidecar |
| **Teams** publish (.NET sidecar, Graph + Media SDK) | ⚠️ written, not yet compiled on Windows — see [doc 005 §10](docs/design/005-teams-connector-architecture.md) |
| Google Meet | ⬜ roadmap only |

Neither connector's pipeline needs its native SDK to be tested: `FileSink` makes the avatar's output watchable as a file, and both sidecars have in-repo fakes that speak their real wire protocols.

## Design documents

Read in order — each supersedes the previous for its own scope.

| Doc | Scope |
|---|---|
| [001](docs/design/001-zoom-avatar-bridge.md) | Zoom mechanics: RTMS vs Meeting SDK, validated against official docs. Authoritative on Zoom. |
| [002](docs/design/002-multi-platform-architecture-review.md) | Multi-platform review (Teams/Meet). Retained as a **roadmap**. |
| [003](docs/design/003-zoom-v1-architecture.md) | **Zoom V1 architecture. The build target.** |
| [004](docs/design/004-sidecar-ipc-protocol.md) | 🔒 Zoom: Python ↔ C++ sidecar IPC protocol, **frozen**. |
| [005](docs/design/005-teams-connector-architecture.md) | **Teams connector architecture**, and every shared change it required. |
| [006](docs/design/006-teams-sidecar-ipc-protocol.md) | Teams: Python ↔ .NET sidecar IPC protocol. Independent of 004 by design. |
| [007](docs/design/007-google-meet-connector-architecture.md) | **Google Meet connector architecture** — a browser as the whole media path. |
| [008](docs/design/008-zoom-web-meeting-awareness.md) | Zoom-web meeting awareness over RTMS. Authoritative for `INGEST_MODE=rtms`. |
| [009](docs/design/009-zoom-web-browser-ingest.md) | **Zoom-web without RTMS** — audio tapped from the page, so any account works. Supersedes 008 §2/§4 for `INGEST_MODE=browser`. |

## Quick start

Requires Python 3.12.

```bash
poetry install
cp .env.example .env          # credentials only needed from M2
poetry run uvicorn src.main:app --reload
```

```bash
curl localhost:8000/health
curl localhost:8000/metrics
```

Docker:

```bash
docker compose -f docker/docker-compose.yml up --build
```

## Development

```bash
poetry run ruff check .                 # lint
poetry run pytest                       # all tests
poetry run pytest tests/architecture    # layering rules only
```

Without Poetry:

```bash
python3.12 -m venv .venv
.venv/bin/pip install fastapi 'uvicorn[standard]' websockets pydantic pydantic-settings \
    dependency-injector structlog orjson pytest pytest-asyncio ruff httpx
.venv/bin/python -m pytest
```

## Layout

```
src/
├── api/              HTTP edge. Never imports connectors.
├── config/           Settings (pydantic-settings, MC_ prefix)
├── domain/           Canonical model. Depends on nothing.
├── protocols/        Six ports — see below
├── connectors/       Adapters. The ONLY place platform SDKs may appear.
│   ├── zoom/         RTMS ingest · Meeting SDK publish (protocol.py frozen)
│   └── teams/        Graph join · one media session · .NET sidecar under sidecar/dotnet/
├── services/         Orchestration + the shared media pipeline
├── avatar/           Avatar client. Never platform-aware.
├── infrastructure/   Logging, metrics, ambient context, reconnect policy
└── containers.py     DI wiring — the only module knowing concrete types
```

Connectors are independent: neither imports the other, and a test enforces it. Adding Google Meet means a new folder, one enum member, and one line in `containers.py` — no existing connector changes.

### The ports

A protocol earns its place only if a **second implementation exists today** (doc 003 §0). That rule is what keeps this from becoming a framework. It was four ports with one connector; Teams supplied the second implementation the last two had been waiting for:

| Port | Implementation | Second implementation |
|---|---|---|
| `AudioSource` | `RtmsAudioSource` | `TeamsAudioSource` · `ReplayAudioSource` |
| `AvatarTransport` | `WebSocketAvatarTransport` | `FakeAvatarTransport` |
| `MediaDecoder` | `FfmpegDecoder` | `FakeDecoder` |
| `MediaSink` | `MeetingPublisher` (Zoom) | `TeamsMediaSink` · `FileSink` / `NullSink` |
| `ConnectorSession` | `ZoomMeetingSession` | `TeamsMeetingSession` |
| `ConnectorSessionFactory` | `ZoomSessionFactory` | `TeamsSessionFactory` |

The last two are `Protocol`s, so **`ZoomMeetingSession` satisfied them without a single edit** — structural typing is what made adding Teams a zero-touch change for the connector already in production.

### Enforced invariants

`tests/architecture/test_layering.py` walks the import graph and fails CI by name:

- `domain/` imports nothing from `src`
- `protocols/` imports only `domain/`
- `api/`, `services/`, `avatar/` never import `connectors/`
- RTMS wire models never leave `connectors/zoom/`
- Teams' Graph models and IPC codec never leave `connectors/teams/`
- **the two connectors never import each other**
- no relative imports anywhere in `src/`

Architecture guarded only by intention decays; these make the boundaries a build error.

### Observability

Every log line and metric sample carries `session_id` and `correlation_id`, injected from ambient context so no call site has to pass them. Every media frame carries a `FrameContext` — one shared reference per session, so the two ids cannot drift apart.

`/metrics` is aggregated and safe to scrape. `/metrics/sessions/{id}` retains the correlation id for debugging one conversation — deliberately not a scrape-path label, since correlation ids are unbounded over time.

## Configuration

`MC_` prefix, `__` nesting delimiter. See [.env.example](.env.example).

```bash
MC_ZOOM__CLIENT_ID=...            # Zoom RTMS ingest
MC_ZOOM__SDK_KEY=...              # Zoom Meeting SDK publish — a separate credential path

MC_TEAMS__TENANT_ID=...           # Azure AD app registration
MC_TEAMS__CLIENT_ID=...
MC_TEAMS__CLIENT_SECRET=...
MC_TEAMS__SIDECAR_HOST=...        # the Windows media host

MC_AVATAR__URL=ws://localhost:8100/stream
MC_MEDIA__VIDEO_FPS=25
```

Secrets are `SecretStr` and masked in `repr()`.

**A connector is registered only when configured.** With no `MC_TEAMS__*` values the service is Zoom-only and carries no Teams surface at all; requesting `"platform": "teams"` then returns a precise "no connector registered" rather than failing inside a join. A broken Teams configuration is logged and skipped — it can never stop the service booting.

Selecting a platform per session:

```bash
curl -X POST localhost:8000/sessions -H 'content-type: application/json' \
  -d '{"meeting_number": "1234567890"}'                                   # Zoom (default)

curl -X POST localhost:8000/sessions -H 'content-type: application/json' \
  -d '{"platform": "teams", "meeting_number": "123456789012", "passcode": "abc123"}'

curl -X POST localhost:8000/sessions -H 'content-type: application/json' \
  -d '{"platform": "teams", "meeting_url": "https://teams.microsoft.com/l/meetup-join/..."}'
```

`platform` defaults to `zoom`, so requests written before Teams existed behave identically.
