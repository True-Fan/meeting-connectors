# Runbook — bringing everything up

One page, meant to stay open in a terminal. It assumes one-time setup (browser profile
sign-ins, app registrations) is already done — see the [connector docs](README.md#start-here)
for that. This page is about **process order** and **ports**, which is where most first-run
failures actually come from.

## The moving parts, and their ports

| Process | Repo | Port | What breaks if it's missing |
|---|---|---|---|
| **The bridge** (`src.main:app`) | this repo | `8000` | Nothing to `POST /sessions` to |
| **calendar-orchestrator** (`app.main:app`) | `calendar-orchestrator/` | `8200` | No automatic joins — you'd have to `curl` every meeting by hand |
| **avatar_gateway** | external avatar-agent repo | `8100` | The bot joins the meeting, every health check goes green, and it **never speaks** |
| **agent.py** (the LiveKit worker) | external avatar-agent repo | n/a (dispatched by name) | The gateway has nowhere to send audio — same silent-join symptom as above |

**Bring these up avatar-side-out**: agent worker → gateway → orchestrator → bridge. The bridge
is the last thing started because it's the one that starts trying to connect to the avatar
immediately, and it does **not** retry that connection — a session created before the gateway
is up will need to be re-created, not healed.

## 0. One-time setup (per platform, do this once)

| Platform | One-time step |
|---|---|
| Google Meet | `.venv/bin/python tools/meet_signin.py --profile ~/.mc/meet-profile` (headed, interactive) |
| Zoom (`zoom_web`) | `.venv/bin/python scripts/zoom_web_login.py --profile ~/.mc/zoom-web-profile` (headed; **select a microphone**) |
| Teams (`teams_web`) | `.venv/bin/python scripts/teams_web_login.py --profile ~/.mc/teams-web-profile` (optional) |
| calendar-orchestrator | Enable the Calendar API, choose a credential mode, then `python scripts/oauth_bootstrap.py` if using OAuth mode — see [calendar-orchestrator.md § Setup](calendar-orchestrator.md#setup) |

Set the corresponding `MC_*`/`ORCH_*` environment variables (`.env` in each repo) before
starting anything below — see each connector doc and `calendar-orchestrator.md` for the exact
list.

## 1. The avatar agent (external)

Not part of this repo — a separate Streaming Avatar Agent project. Two processes:

```bash
# 1a. the LiveKit agent worker — needs LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET
cd <avatar-agent-repo>
./venv/bin/python agent.py dev

# 1b. the gateway — translates the bridge's fMP4/PCM WebSocket protocol into LiveKit tracks
cd <avatar-agent-repo>
./venv/bin/python avatar_gateway.py        # binds ws://0.0.0.0:8100/stream (loopback-checked)
```

**Before starting the gateway, confirm nothing else owns port 8100:**
```bash
lsof -nP -iTCP:8100 -sTCP:LISTEN
```
Two things are known to squat here and fail *silently in the same way* — the avatar joins the
meeting, every health check is green, and it never answers:
- `calendar-orchestrator` used to default to port 8100 too (now 8200 — see below). If both are
  ever pointed at 8100, the more specific bind wins every `localhost` connection and the
  bridge gets `HTTP 403` from the *wrong* service's `/stream`.
- A leftover mock/test avatar process from an earlier session.

The gateway's `agent_name` (default `gunika`, or `$AGENT_NAME`) must match the value the agent
worker registers under — dispatch only reaches a worker whose name matches exactly.

`MC_AVATAR__URL=ws://localhost:8100/stream` is the bridge-side setting that must point at
this. It's the default, so usually nothing to change.

**Verifying the gateway without a real meeting:**
```bash
<avatar-agent-repo>/verify_gateway.py    # or wherever this repo's equivalent lives
```
drives the bridge's real transport/framer/decoder against a running gateway and checks that
non-silent audio actually comes back — the fastest way to know the gateway itself is healthy
before blaming a connector.

## 2. calendar-orchestrator (optional, but this is what removes the manual `curl`)

```bash
cd calendar-orchestrator
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8200
```

**Port 8200, not 8100** — see [calendar-orchestrator.md](calendar-orchestrator.md#3-configure-and-run)
for exactly why that collision is dangerous and silent. Skip this whole step if you're testing
one meeting by hand with `curl` — see §4.

## 3. The bridge (this repo) — always last

```bash
.venv/bin/uvicorn src.main:app --port 8000
# or, with Poetry:
poetry run uvicorn src.main:app --reload
```

```bash
curl localhost:8000/health
curl localhost:8000/metrics
```

## 4. Join a meeting by hand (skip calendar-orchestrator for a quick test)

**Google Meet:**
```bash
curl --location 'localhost:8000/sessions' \
  --header 'content-type: application/json' \
  --data '{"platform": "google_meet", "meeting_number": "abc-defg-hij"}'
```

**Zoom** (as a guest — no host-side setup needed; see [connectors/zoom.md](connectors/zoom.md)):
```bash
curl --location 'localhost:8000/sessions' \
  --header 'content-type: application/json' \
  --data '{
    "platform": "zoom_web",
    "meeting_number": "95097700824",
    "passcode": "868339"
  }'
```

**Microsoft Teams** (as a guest — see [connectors/teams.md](connectors/teams.md)):
```bash
curl --location 'localhost:8000/sessions' \
  --header 'content-type: application/json' \
  --data '{
    "platform": "teams_web",
    "meeting_number": "9350063851001",
    "passcode": "VH2Er6",
    "display_name": "AI Avatar"
  }'
```

All three return `202 Accepted` immediately — the avatar shows up and starts publishing idle
media right away, before ingest is necessarily confirmed. Check
`GET /sessions/{id}` for `state` (`ACTIVE` once both legs are healthy) and
`GET /metrics/sessions/{id}` for the counters mentioned in troubleshooting below.

## 5. Full sequence, per platform (what the notes at the top of this doc distill to)

### Google Meet
```bash
# one-time
.venv/bin/python tools/meet_signin.py --profile ~/.mc/meet-profile

# every run, in order
cd <avatar-agent-repo> && ./venv/bin/python agent.py dev &
cd <avatar-agent-repo> && ./venv/bin/python avatar_gateway.py &
cd calendar-orchestrator && uvicorn app.main:app --port 8200 &
cd meeting-connectors && .venv/bin/uvicorn src.main:app --port 8000
```
(`scripts/oauth_bootstrap.py` — one-time only, and only if calendar-orchestrator is using
OAuth credential mode; see [calendar-orchestrator.md](calendar-orchestrator.md#2-choose-a-credential-mode).
It's unrelated to the Meet connector itself — Meet's own sign-in is `tools/meet_signin.py`
above.)

### Zoom
```bash
# one-time (per profile)
.venv/bin/python scripts/zoom_web_login.py --profile ~/.mc/zoom-web-profile

# every run
cd <avatar-agent-repo> && ./venv/bin/python agent.py dev &
cd <avatar-agent-repo> && ./venv/bin/python avatar_gateway.py &
cd calendar-orchestrator && uvicorn app.main:app --port 8200 &
cd meeting-connectors && .venv/bin/uvicorn src.main:app --port 8000

curl --location 'localhost:8000/sessions' \
  --header 'content-type: application/json' \
  --data '{"platform": "zoom_web", "meeting_number": "95097700824", "passcode": "868339"}'
```

### Microsoft Teams
```bash
# one-time (optional)
.venv/bin/python scripts/teams_web_login.py --profile ~/.mc/teams-web-profile

# every run
cd <avatar-agent-repo> && ./venv/bin/python agent.py dev &
cd <avatar-agent-repo> && ./venv/bin/python avatar_gateway.py &
cd calendar-orchestrator && uvicorn app.main:app --port 8200 &
cd meeting-connectors && .venv/bin/uvicorn src.main:app --port 8000

curl --location 'localhost:8000/sessions' \
  --header 'content-type: application/json' \
  --data '{"platform": "teams_web", "meeting_number": "9350063851001", "passcode": "VH2Er6", "display_name": "AI Avatar"}'
```

## 6. Reading the gateway's health line

The avatar gateway logs one summary line every 10 seconds — this is the fastest signal for
"which half of the pipeline is broken," independent of which meeting platform is in use:

```
session ses_… — 11.1s: meet→agent 344KiB in 550 frames (dropped 0) ·
agent→meet 696KiB in 371 frames, 1.5s audible / 8.5s silent · fMP4 out 113KiB
```

| Symptom | Meaning | Where to look next |
|---|---|---|
| `meet→agent` frozen at a small number while the session continues | **The single most important failure — looks like success.** The bridge stopped forwarding meeting audio; the agent heard the first seconds and nothing after. | `GET localhost:8000/metrics/sessions/<id>` — a climbing `suppressed` count means the echo gate is stuck shut. |
| `meet→agent` at zero from the start | The bridge isn't sending at all | Check the connector's capture/ingest tap specifically |
| `agent→meet` at zero | The agent is in the room but mute | Check the agent's STT/LLM/TTS keys, not the bridge |
| `audible` near zero while `agent→meet` grows | The agent's track flows but carries only silence — connected, not talking | Agent-side issue; a LiveKit track delivers frames continuously whether or not the agent speaks, which is why this is measured on samples, not frame arrival |
| No "agent is in the room" line within 30s | Dispatch never reached the worker | Check `AGENT_NAME` matches on both gateway and `agent.py` |

## 7. Common first-run failures

- **Avatar joins, every health check is green, never speaks** — almost always the port-8100
  collision (§1) or the bridge having connected to the avatar *before* the gateway was ready
  (the bridge does not retry that connection — create a new session after fixing the gateway,
  don't wait for the old one to heal).
- **`calendar-orchestrator` fires but nothing joins** — check `GET /jobs` on port 8200 for the
  scheduled fire time, then check the bridge's own logs for the `POST /sessions` it should
  have received; `POST /sync` on the orchestrator forces an immediate re-check instead of
  waiting for the next poll.
- **Zoom-web/Teams-web: avatar joins, video shows, no audio either direction** — see each
  connector's own troubleshooting section
  ([Zoom](connectors/zoom.md#troubleshooting), [Teams](connectors/teams.md#troubleshooting)) —
  the most common cause differs per platform (missing mic selection for Zoom, blocked CSP for
  Teams).
- **Session created but stuck in `JOINING`** — check `GET /sessions/{id}` for `components`
  and `errors`; for Zoom running in `rtms` ingest mode specifically, this can mean RTMS hasn't
  attached yet (webhook not received, or not triggered on the Zoom side — see
  [connectors/zoom.md](connectors/zoom.md#optional-rtms-ingest-mode)).

## The avatar agent (external)

Not documented here beyond its contract, since it's a separate repository this bridge only
talks to over one fixed WebSocket protocol (see [LLD.md §8](LLD.md#8-avatar-client-srcavatar--the-contract-with-the-external-agent)).
What's relevant to running it is entirely captured in §1 above. If you have access to that
repo, its own `README-gateway.md` covers configuration knobs (`GATEWAY_PASSTHROUGH`,
`GATEWAY_FRAGMENT_MS`, video placeholder settings, chat-to-speech wiring, and interruption
tuning for headless deployments) in more depth than belongs in this repo's docs.
