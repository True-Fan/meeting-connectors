# avatar_gateway

The translation layer between `meeting-connectors` and `realtime-product/agent-worker`.

```
Zoom/Teams/Meet  ⇄  meeting-connectors  ⇄  avatar_gateway  ⇄  LiveKit room  ⇄  agent-worker
                     (this repo)            (ws://:8300)                       (assigned via
                                                                                 Init → WH)
```

`meeting-connectors` speaks one fixed avatar protocol and knows nothing about LiveKit;
`agent-worker` speaks LiveKit and knows nothing about meetings. This gateway is the
translation, and it lives on this side of the boundary so the bridge stays platform- and
AI-agnostic — same reason as always.

**Bringing up the whole stack from nothing?** See [`RUNBOOK.md`](RUNBOOK.md) — every command,
in order, dependencies included, ending with a real Google Meet/Zoom/Teams session. This page
covers what this one piece is and how it differs from the old gateway.

## What changed from the old gateway

This is adapted from the Streaming Avatar Agent's own `avatar_gateway.py`
(`/Users/dev/work/test/avatar_gateway.py`), which paired with a bare LiveKit worker
(`agent.py`) dispatched **by name**. That pairing is retired. `agent-worker` is never
dispatched by name — it only takes a job when Initialisation asks Worker Handling to assign a
registered worker pod to a room.

So the wire protocol, the LiveKit room join, the fMP4 muxer and the silence padding are all
unchanged (see `gateway.py`'s module docstring for why the continuous timelines are
load-bearing, not cosmetic). Two things are different: how the agent gets into the room, and
now, what the video leg of the fMP4 actually carries.

| | Old gateway | This gateway |
|---|---|---|
| Getting an agent into the room | `lk.agent_dispatch.create_dispatch(agent_name="gunika")` | `POST` a session to Initialisation (`init_client.py`), the same way `agent-worker/devtools/local_session.py` does it by hand |
| A stuck/wedged worker | re-dispatch once, then give up loudly | give up loudly (there is no equivalent "redispatch" — Init has already asked WH once) |
| Config knob that named the agent | `AGENT_NAME` (must match `agent.py`) | `AGENT_ID` (must be seeded — see below) |
| Video leg of the outgoing fMP4 | always the synthetic placeholder | the agent's **real** video once an avatar backend (anam/truefan/musetalk) publishes one — placeholder until then, and again if that track ever drops |

## Real avatar video, not just a placeholder

`_on_track_subscribed` now subscribes to both audio and video from the room. `_read_agent_video`
keeps only the *latest* real frame (a live face has no backlog worth preserving), and
`_pump_video` writes it into the muxer on every tick — the same continuous-timeline discipline
`_pump_muxer` already used for audio, now applied to video too, because an avatar backend starts
rendering after the job begins and can drop a frame under load; a real ffmpeg video input can't
tolerate a gap the way a placeholder generator never had one. The moment that real track goes
away, frames fall back to the placeholder rather than freezing on the avatar's last expression.

The muxer's video leg is now a raw I420 pipe (`GATEWAY_VIDEO_WIDTH/HEIGHT/FPS` fixed at ffmpeg
startup, unchanged knobs) instead of an ffmpeg-generated pattern — real frames get resized onto
that same fixed geometry (nearest-neighbour, cheap) so ffmpeg never sees a frame size it didn't
start with. Raise those three if you want more than a placeholder-sized picture of the avatar's
face; the connector-facing contract doesn't change either way, only what's actually drawn.

Whether this ever fires locally depends entirely on the assigned agent's own `avatar` feature
slice (`provider: anam|truefan|musetalk`) — with no avatar backend configured, agent-worker
publishes audio only and every tick stays on the placeholder, same as before this change.

## Before running it

1. **Postgres + Redis** running locally.
2. **Worker Handling** migrated to head and seeded:
   ```bash
   cd worker-handling-service-module
   uv run alembic upgrade head
   uv run python scripts/seed_agents.py
   ```
3. **Initialisation** running on `:8000` with `INTERNAL_API_KEY` set to the same value as
   `agent-worker/.env`'s `INTERNAL_API_KEY` (Init reads its agent-config route with this key).
4. **Worker Handling** running on `:8100`.
5. **agent-worker** running on `:8080`, registered with WH — `curl localhost:8080/readyz`
   should be `200`.

## Run it

```bash
cd meeting-connectors/avatar_gateway
uv sync
cp .env.example .env   # fill in LIVEKIT_* the same as agent-worker/.env; defaults cover the rest
uv run python gateway.py     # ws://127.0.0.1:8300/stream
```

Then point the bridge at it — `meeting-connectors/.env`:
```
MC_AVATAR__URL=ws://localhost:8300/stream
```

**Before starting, confirm nothing else owns port 8300** (and that Worker Handling, not this
gateway, owns 8100 — the old gateway's default):
```bash
lsof -nP -iTCP:8300 -sTCP:LISTEN
```

## Reading the logs

Same health line as the old gateway, unchanged:
```
session ses_… — 11.1s: meet→agent 344KiB in 550 frames (dropped 0) ·
agent→meet 696KiB in 371 frames, 1.5s audible / 8.5s silent · fMP4 out 113KiB
```

| Symptom | Meaning | Where to look |
|---|---|---|
| `meet→agent` frozen while the session continues | The bridge stopped forwarding meeting audio | meeting-connectors' own echo-gate / capture tap |
| `agent→meet` at zero | agent-worker is in the room but mute | agent-worker's STT/LLM/TTS keys and logs |
| No "agent is in the room" log within `GATEWAY_AGENT_JOIN_TIMEOUT_S` (default 30s) | Init accepted the session but no AGENT participant joined | `curl localhost:8080/readyz`, then agent-worker's own logs — a worker can stay registered with WH while its LiveKit signalling connection is dead |
| `could not start an agent-worker session` at handshake time | Init/WH refused the session outright | the exception text names the HTTP status; check the demo tenant/credentials and that `AGENT_ID` is seeded |
| Agent joins the room and it's deleted again within ~1s, every time | agent-worker's own `[PIPELINE] refusing job ...` — the assigned agent's config names a model/tool/credential this worker build doesn't have | agent-worker's logs name the exact field; this is a config problem, not a gateway one |

**Fixed, worth knowing about:** this gateway used to join the room (with a `room_create=True`
grant) *before* asking Init to start the agent session. Occasionally agent-worker's own
room-existence check would run before it could see the room this gateway had just created, so
it fell through to its own `create_room(metadata=...)` on a room that already (silently)
existed — and LiveKit ignores the metadata on an already-existing room. The job then connected
to a room with **empty** metadata, couldn't resolve `agent_id`, and refused instantly — which
looked exactly like the row above, but wasn't fixable from agent-worker's config at all. `run()`
now always starts the agent session (which makes agent-worker create the room) before this
gateway joins it — see `_token()`'s and `_setup_agent_and_room()`'s docstrings in `gateway.py`.
The placeholder still starts flowing to the bridge immediately regardless (`_pump_muxer`,
`_pump_video`, `_pump_room_to_meet` don't wait on this), so this fix cost no latency.

## What's out of scope here

- **This gateway doesn't stand up an avatar backend** (anam/truefan/musetalk) — it forwards
  whatever video that backend publishes, if the assigned agent has one enabled. Getting a real
  face on screen locally means that agent's config needs a working avatar slice and the
  matching credentials; this gateway has no opinion on that.
- **No meeting video ever goes *into* the avatar.** That direction is unchanged and is a
  platform-wide design invariant, not a gap of this gateway's — see
  `meeting-connectors/docs/input-output-format.md` §1. Only the avatar's own outgoing video is
  new here.
- **Chat and meeting-context forwarding** (`lk.chat` / `meeting.context`) are still sent
  unconditionally, same as the old gateway, but whether `agent-worker` has a handler
  registered for either topic is independent of this gateway — if it doesn't, LiveKit drops
  the stream with "no callback attached" and the avatar just doesn't react, which is the same
  graceful-miss the protocol was designed around.
