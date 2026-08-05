# meeting-connectors

Meeting integration service connecting AI streaming avatar agents with Zoom, Microsoft Teams, and Google Meet through a unified media bridge architecture.

**V1 scope is Zoom only.** Teams and Google Meet are a documented roadmap, not a build target — see [doc 002](docs/design/002-multi-platform-architecture-review.md).

The avatar joins a Zoom meeting as an ordinary participant: it hears the humans, speaks, and shows generated video. This service is only the bridge — it contains no AI, and the existing Streaming Avatar Agent is never modified.

```
Human speaks → Zoom RTMS → bridge → Avatar Agent → fMP4 → decode → Meeting SDK → Zoom
```

| | |
|---|---|
| **Receive** participant audio | Zoom **RTMS** (WebSocket, Python) |
| **Publish** avatar audio + video | Zoom **Meeting SDK for Linux** (C++ sidecar) |

RTMS is receive-only — it cannot publish media — so both integrations are required, in strictly separate directions. Reasoning in [doc 001 §1](docs/design/001-zoom-avatar-bridge.md).

## Status

**Milestone 1 complete** — skeleton, domain model, ports, config, observability, frozen IPC protocol.

| Milestone | Scope | State |
|---|---|:--:|
| M1 | Skeleton, domain, ports, config, logging, metrics, frozen sidecar IPC | ✅ |
| M2 | RTMS ingest — receive participant audio | ⬜ |
| M3 | 
| M6 | End-to-end + measured latency Avatar client — forward PCM, receive streamed fMP4 | ⬜ |
| M4 | Media decoder — synchronized audio/video frames | ⬜ |
| M5 | Meeting SDK publisher — join, publish audio + video | ⬜ || ⬜ |

M1–M4 need no Zoom SDK build, no C++ toolchain, and no account entitlement: `FileSink` makes the avatar's output watchable as a file before the sidecar exists.

## Design documents

Read in order — each supersedes the previous for its own scope.

| Doc | Scope |
|---|---|
| [001](docs/design/001-zoom-avatar-bridge.md) | Zoom mechanics: RTMS vs Meeting SDK, validated against official docs. Authoritative on Zoom. |
| [002](docs/design/002-multi-platform-architecture-review.md) | Multi-platform review (Teams/Meet). Retained as a **roadmap**. |
| [003](docs/design/003-zoom-v1-architecture.md) | **Zoom V1 architecture. The build target.** |
| [004](docs/design/004-sidecar-ipc-protocol.md) | 🔒 Python ↔ C++ sidecar IPC protocol, **frozen**. |

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
├── protocols/        Four ports — see below
├── connectors/zoom/  The only place Zoom concepts exist
│   ├── rtms/         RTMS ingest (M2)
│   └── publisher/    Meeting SDK publish (M5); protocol.py frozen in M1
├── services/         Orchestration + media pipeline (M2–M4)
├── avatar/           Avatar client. Never platform-aware. (M3)
├── infrastructure/   Logging, metrics, ambient context
└── containers.py     DI wiring — the only module knowing concrete types
```

### The four ports

A protocol earns its place only if a **second implementation exists today** (doc 003 §0). That rule is what keeps this from becoming a framework:

| Port | Production | Second implementation |
|---|---|---|
| `AudioSource` | `RtmsAudioSource` (M2) | `ReplayAudioSource` — pipeline from a PCM file, no live meeting |
| `AvatarTransport` | `WebSocketAvatarTransport` (M3) | `FakeAvatarTransport` |
| `MediaDecoder` | `FfmpegDecoder` (M4) | `FakeDecoder` |
| `MediaSink` | `MeetingPublisher` (M5) | `FileSink` / `NullSink` |

### Enforced invariants

`tests/architecture/test_layering.py` walks the import graph and fails CI by name:

- `domain/` imports nothing from `src`
- `protocols/` imports only `domain/`
- `api/`, `services/`, `avatar/` never import `connectors/`
- RTMS wire models never leave `connectors/zoom/rtms/`
- no relative imports anywhere in `src/`

Architecture guarded only by intention decays; these make the boundaries a build error.

### Observability

Every log line and metric sample carries `session_id` and `correlation_id`, injected from ambient context so no call site has to pass them. Every media frame carries a `FrameContext` — one shared reference per session, so the two ids cannot drift apart.

`/metrics` is aggregated and safe to scrape. `/metrics/sessions/{id}` retains the correlation id for debugging one conversation — deliberately not a scrape-path label, since correlation ids are unbounded over time.

## Configuration

`MC_` prefix, `__` nesting delimiter. See [.env.example](.env.example).

```bash
MC_ZOOM__CLIENT_ID=...            # RTMS ingest
MC_ZOOM__SDK_KEY=...              # Meeting SDK publish — a separate credential path
MC_AVATAR__URL=ws://localhost:8100/stream
MC_MEDIA__VIDEO_FPS=25
```

Secrets are `SecretStr` and masked in `repr()`.
