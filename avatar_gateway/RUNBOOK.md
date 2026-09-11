# Local Runbook — realtime-product + avatar_gateway + meeting-connectors

Everything needed to bring the whole stack up on a fresh laptop, in order, with every command.
Seven processes across two repos; see [`README.md`](README.md) for what each one is and why.

```
Google Meet/Zoom/Teams  ⇄  meeting-connectors  ⇄  avatar_gateway  ⇄  LiveKit room  ⇄  agent-worker
                            (:8001)                (:8300)                            (:8080, assigned
                                                                                        via Init → WH)
                                                                    Initialisation (:8000) ⇄ Worker Handling (:8100)
```

---

## 0. One-time installs (do these once per machine)

```bash
# Postgres, Redis, ffmpeg
brew install postgresql@16 redis ffmpeg
brew services start postgresql@16
brew services start redis

# Python package manager used by all three realtime-product modules and avatar_gateway
curl -LsSf https://astral.sh/uv/install.sh | sh
# uv installs to ~/.local/bin — make sure that's on PATH, or use ~/.local/bin/uv below
export PATH="$HOME/.local/bin:$PATH"
```

Confirm:
```bash
psql --version && redis-cli ping && ffmpeg -version | head -1 && uv --version
```

Python ≥ 3.12 is required by every module (`python3 --version`).

---

## 1. One-time per-repo setup

Run these once; skip on every subsequent boot.

### 1a. Database

Every module's `DATABASE_URL` in this doc assumes a `postgres`/`postgres` role — create one if
your Postgres install doesn't already have it (Homebrew's default role is your macOS username,
not `postgres`):
```bash
createuser -s postgres 2>/dev/null   # ok if it already exists
psql -c "ALTER USER postgres PASSWORD 'postgres';"
```

```bash
createdb -U postgres realtime_worker_handler
# or: psql -U postgres -c 'CREATE DATABASE realtime_worker_handler;'
```

### 1b. Worker Handling (`worker-handling-service-module/`)

```bash
cd /Users/dev/work/realtime-product/worker-handling-service-module
uv sync --group dev
cp .env.example .env
# .env.example's defaults already match this doc (DATABASE_URL, INTERNAL_API_SECRET=
# internal-secret-key, INIT_SERVICE_WEBHOOK_URL=http://localhost:8000/...) — nothing to edit
# for a pure local run.

uv run alembic upgrade head          # migrates the shared DB — do this before Init
uv run python scripts/seed_agents.py # loads the 4 demo agents from scripts/seed_data/*.json
```

### 1c. Initialisation (`initialisation-service-module/`)

```bash
cd /Users/dev/work/realtime-product/initialisation-service-module
uv sync --group dev
cp .env.example .env
# Same story — .env.example's INTERNAL_API_KEY already equals agent-worker's, which is what
# lets agent-worker's GET /internal/agents/{id} call succeed. Nothing to edit for local.
```

Seed the demo tenant (one-time, shared DB — run after WH has migrated):

```bash
psql "postgresql://postgres:postgres@localhost:5432/realtime_worker_handler" <<'SQL'
INSERT INTO tenants (
  id, name, status, plan, default_region, webhook_url,
  max_requests_per_minute, max_concurrent_sessions, max_waiting_sessions,
  max_session_duration_seconds, max_wait_seconds, reserved_worker_count,
  zero_data_retention
) VALUES (
  '11111111-1111-1111-1111-111111111111', 'local-dev', 'active', 'enterprise',
  'ap-south-1', NULL, 60, 5, 20, 1800, 120, 0, false
) ON CONFLICT (id) DO NOTHING;
SQL
```
(Full block with `master_avatars`/`master_voices`/`avatars`/`voices` rows:
[`initialisation-service-module/docs/REBUILD/PHASE_04_LOCAL_SEED.md`](/Users/dev/work/realtime-product/initialisation-service-module/docs/REBUILD/PHASE_04_LOCAL_SEED.md) —
only needed if you'll create tenant-scoped avatars/voices; the tenant row above is enough for
what this runbook does.)

### 1d. agent-worker (`agent-worker/`)

```bash
cd /Users/dev/work/realtime-product/agent-worker
uv sync
cp .env.example .env
```
Edit `.env` — these are **required**, nothing else is for a local run:
| Variable | Value |
|---|---|
| `LIVEKIT_URL` / `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` | your LiveKit project's credentials |
| At least one STT/LLM/TTS vendor key the seeded agents use | e.g. `SONIOX_API_KEY`, `CEREBRAS_API_KEY`, `SMALLEST_AI_API_KEY` — check `scripts/seed_data/*.json` → `models` for which providers a given `agent_id` needs |

Everything else in `.env.example` (`WORKER_HANDLER_SERVICE_BASE_URL=http://localhost:8100`,
`PLATFORM_API_URL=http://localhost:8000`, `INTERNAL_API_KEY`/`INTERNAL_API_SECRET`, ports)
already matches this runbook's layout.

### 1e. meeting-connectors (`meeting-connectors/`)

```bash
cd /Users/dev/work/meeting-connectors
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
playwright install chromium
deactivate
```
`.env` should already exist in this repo with at least Zoom credentials filled in
(`MC_ZOOM__*`) if you plan to test Zoom, and `MC_GOOGLE_MEET__PROFILE_DIR` pointing at a
Chromium profile dir for Google Meet (default `.meet/profile`, relative to this repo).

**Google Meet's one-time sign-in** (skip if `.meet/profile/` already exists and is signed in):
```bash
.venv/bin/python tools/meet_signin.py --profile .meet/profile
```
This opens a real (headed) Chromium window — sign into the Google account the avatar should
join meetings as, then close it. `MC_GOOGLE_MEET__HEADLESS` can be `true` for every run after
this; the sign-in step itself needs a head.

### 1f. avatar_gateway (`meeting-connectors/avatar_gateway/`)

```bash
cd /Users/dev/work/meeting-connectors/avatar_gateway
uv sync
cp .env.example .env
```
Edit `.env` — `LIVEKIT_URL`/`LIVEKIT_API_KEY`/`LIVEKIT_API_SECRET` **must be the same LiveKit
project as agent-worker's `.env`** (step 1d) — this gateway and agent-worker have to land in
the same room. `AGENT_ID` picks which seeded agent (`1`-`4`) gets used; everything else
(`INIT_URL`, `INIT_TENANT_ID`, `INIT_USERNAME`/`PASSWORD`, `GATEWAY_PORT=8300`) already
matches steps 1b/1c.

Point the bridge at it — `meeting-connectors/.env`:
```bash
echo 'MC_AVATAR__URL=ws://localhost:8300/stream' >> /Users/dev/work/meeting-connectors/.env
# or edit the existing MC_AVATAR__URL line if one's already there
```

---

## 2. Every run — bring everything up, in this order

Seven terminals (or seven `&`-backgrounded commands in one, per below). **Order matters**:
each step depends on the one before it being reachable.

```bash
# 1. Infra (skip if brew services already has these running)
brew services start postgresql@16
brew services start redis
pg_isready && redis-cli ping

# 2. Worker Handling — :8100
cd /Users/dev/work/realtime-product/worker-handling-service-module
uv run uvicorn app.main:app --host 0.0.0.0 --port 8100
```
```bash
# 3. Initialisation — :8000 (needs WH reachable)
cd /Users/dev/work/realtime-product/initialisation-service-module
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```
```bash
# 4. agent-worker — :8080 (needs WH + Init reachable; registers with WH on boot)
cd /Users/dev/work/realtime-product/agent-worker
uv run uvicorn app.main:app --port 8080
```
```bash
# 5. avatar_gateway — ws://127.0.0.1:8300/stream (needs Init + WH + agent-worker up)
cd /Users/dev/work/meeting-connectors/avatar_gateway
uv run python gateway.py
```
```bash
# 6. meeting-connectors bridge — :8001 (last, per the original RUNBOOK.md: it connects
#    outward on every session and does not retry, so everything upstream must already be up)
cd /Users/dev/work/meeting-connectors
.venv/bin/uvicorn src.main:app --port 8001
```

If `uv` isn't found, either `export PATH="$HOME/.local/bin:$PATH"` first, or call the repo's
own venv binary directly, e.g. `worker-handling-service-module/.venv/bin/uvicorn ...`.

### Health check every service before touching a real meeting

```bash
curl -s localhost:8100/health                                   # {"status":"ok"}
curl -s localhost:8000/health                                    # {"status":"ok"}
curl -s localhost:8080/healthz                                   # process up
curl -s localhost:8080/readyz                                    # 200 = registered with WH
curl -s -o /dev/null -w '%{http_code}\n' localhost:8300           # 426 = alive (WS-only, expected)
curl -s localhost:8001/health                                     # {"status":"healthy",...}
```

Agent-worker's `/readyz` returning non-200 means the whole chain is a dead end — nothing
downstream of it will ever get an agent into a room.

---

## 3. Join a Google Meet

```bash
curl --location 'localhost:8001/sessions' \
  --header 'content-type: application/json' \
  --data '{"platform": "google_meet", "meeting_number": "xxx-yyyy-zzz"}'
```
`meeting_number` is the Meet code from `meet.google.com/xxx-yyyy-zzz` (create the meeting from
the *same* Google account signed into the profile in step 1e, or from any account that can
admit a guest — the bot needs to be let in either way if lobby/knock is on).

Returns `202 Accepted` immediately. Then:
```bash
curl -s localhost:8001/sessions/<id>          # state: JOINING → ACTIVE
curl -s localhost:8001/metrics/sessions/<id>  # audio/video counters
```

Watch `avatar_gateway`'s terminal for its 10s stats line —
`meet→agent … · agent→meet … · video N real / M placeholder` — that's the fastest signal for
which half of the pipeline (if any) is stuck. See [`README.md` § Reading the logs](README.md#reading-the-logs).

End it:
```bash
curl -X DELETE localhost:8001/sessions/<id>
```

**Zoom or Teams instead** — same `POST /sessions`, different body; see
[`meeting-connectors/docs/RUNBOOK.md` §4](/Users/dev/work/meeting-connectors/docs/RUNBOOK.md).

---

## 4. Shutting everything down

```bash
for port in 8001 8300 8080 8000 8100; do
  lsof -nP -iTCP:$port -sTCP:LISTEN -t | xargs -r kill
done
```
Postgres/Redis can stay running (`brew services stop postgresql@16 redis` if you want them
down too).

---

## 5. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `address already in use` on any port above | a leftover process from a previous run | `lsof -nP -iTCP:<port> -sTCP:LISTEN` then `kill` the pid |
| avatar_gateway: `could not start an agent-worker session ... no_worker_capacity` | agent-worker isn't registered, or is stuck reporting `busy` | `curl localhost:8080/readyz`; if `lifecycle_state` in `/internal/worker/report` is stuck `busy` with no active call, restart agent-worker (kill :8080, rerun step 2.4) |
| agent-worker: `UnsupportedConfigError` / `UnsupportedToolError` / `MissingToolCredentialError` refusing every job | the seeded agent's config names a model/tool/credential this worker build doesn't have | check `curl localhost:8000/internal/agents/<id> -H 'X-Internal-Key: internal-secret-key'` against agent-worker's README §"Registered vendors"/"Tools" — this is a content problem, not a gateway one |
| meeting-connectors: avatar joins, health green, never speaks | avatar_gateway wasn't up yet when the session was created (`MC_AVATAR__URL` connects once, no retry) | fix the gateway, then create a **new** session — don't wait for the old one |
| Google Meet: bridge sits in `JOINING` | not signed in / profile stale, or the meeting has a lobby that never admitted the bot | re-run `tools/meet_signin.py`; check the meeting itself for a pending "knock" |
| `playwright` / `chromium is not installed` | step 1e's `playwright install chromium` was skipped | re-run it inside the bridge's venv |
