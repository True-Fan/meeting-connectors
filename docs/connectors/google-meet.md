# Google Meet connector (`platform: "google_meet"`)

## Why this connector looks the way it does

Google publishes no server-side way to send media into a Meet conference — its Meet Media
API is receive-only, and says so explicitly — and there is no Meet equivalent of Zoom's
Meeting SDK or Teams' Graph app-hosted media. So the avatar cannot be a server-side
integration here; it has to be a **client** — a real, signed-in Chromium browser, joining
like a person. That single fact shapes everything below: the "credential" is a browser
profile on disk rather than an API key, and the rest of the connector's settings are browser
lifecycle rather than API parameters (`src/connectors/google_meet/capabilities.py` records
the evidence for this, deliberately, so nobody "fixes" the architecture later without
re-deleting it).

## Architecture

```
Playwright ──launches──▶ Chromium (persistent profile, signed in to Google)
                              │
                    injects js/bridge.js before navigation
                              │
                    navigates to meet.google.com/<code>
                              │
        ┌─────────────────────┴─────────────────────┐
        │                                             │
  patched getUserMedia()                    tapped RTCPeerConnection
  → canvas video track (egress)             → mixed remote audio (ingest)
  → AudioWorklet mic track (egress)                   │
        │                                             │
        └──────────────┬──────────────────────────────┘
                        │  loopback WebSocket (127.0.0.1, per-session token)
                        ▼
              src/connectors/google_meet/websocket/*.py
                        │
              MeetAudioSource / ChromiumMediaSink
                        │
              (shared media pipeline — see LLD.md §7)
```

The injected script (`js/bridge.js`) is the connector: it patches `getUserMedia` so Meet
receives a synthetic camera and microphone instead of real hardware, taps every inbound
`RTCPeerConnection` audio track for ingest, watches the roster/chat/caption DOM, and opens the
loopback channel everything above travels on. Python drives the browser (join, mute state,
recovery) and owns every decision; the page only reports facts.

**The echo gate is open, not strict**, for a structural reason: the WebRTC tap is
inbound-only, so the avatar's own published audio cannot loop back into it — there's nothing
for `EchoGuard` to filter.

### Features this connector can supply (each independently switchable)

Attendance, live speaker tracking (audio-level + DOM, merged into one turn history), a
transcript that folds together Meet's live captions and chat, an `@mention`-gated chat
bridge, hand-raise and voice barge-in (both hand off to the shared `Pacer.interrupt()` +
"go ahead" prompt mechanism), and a periodic "meeting context" push so the agent knows who's
in the room without a round trip. All of these are DOM observers feeding Python-side ledgers
— turning them all off leaves the connector scanning nothing beyond the audio tap itself. See
the `google_meet` block of `src/config/settings.py` for every switch and its default; each
field's docstring explains a real trade-off, not a hypothetical one.

## Setup

### 1. Install the browser binary

`pip install -e .` (or `poetry install`) at the repo root already pulls in the `playwright`
package — it's a hard dependency now, shared with `zoom_web` and `teams_web`, which drive the
same Chromium automation. What that install does **not** do is fetch the browser itself:

```bash
.venv/bin/playwright install chromium
# or, with Poetry:
poetry run playwright install chromium
```

`connectors/google_meet/automation/driver.py` still imports Playwright lazily and raises
`PlaywrightUnavailableError` naming this exact command, so a missing browser binary fails
with a clear error at session start rather than an `ImportError` at boot.

### 2. Sign in, once, interactively

The profile is durable, persistent state — authenticate it once, by hand. Google's sign-in
can present a second factor or a device-verification challenge that no script should attempt
repeatedly (that's what gets an automated account flagged).

```bash
.venv/bin/python tools/meet_signin.py --profile ~/.mc/meet-profile
# opens a real, headed Chromium window — sign in as the bot account, then close it
```

`--check` re-runs headlessly against an existing profile to confirm the session is still
valid, without touching it. If a live join ever fails with `GoogleAuthError`, this is the
first thing to re-run — Google sessions do expire.

`tools/meet_inspect.py` is the companion debugging tool: it harvests the live DOM against a
running (or failed) join and reports which selector groups matched, which is far faster than
attaching DevTools to a headless browser mid-failure.

### 3. Point the bridge at the profile

```bash
MC_GOOGLE_MEET__PROFILE_DIR=~/.mc/meet-profile
MC_GOOGLE_MEET__HEADLESS=true     # false only for the interactive sign-in step above
```

`profile_dir` is the **only required setting** — `GoogleMeetSettings.is_configured()` is
`True` once it's set, and that's what makes `containers.py` register this connector at all.
Everything else has a sane default (see `GoogleMeetSettings` in `src/config/settings.py` for
the full list — video/audio geometry, join/lobby timeouts, chat/hand-raise/attendance
switches, etc.).

## Run

```bash
.venv/bin/uvicorn src.main:app --port 8000
```

The avatar agent must already be reachable at `MC_AVATAR__URL` (default
`ws://localhost:8100/stream`) — see [RUNBOOK.md](../RUNBOOK.md) for bringing that up. Without
it, the bridge still joins the meeting; it just never speaks.

## Join a meeting

```bash
curl --location 'localhost:8000/sessions' \
  --header 'content-type: application/json' \
  --data '{
    "platform": "google_meet",
    "meeting_number": "abc-defg-hij"
  }'
```

`meeting_number` takes the dashed meeting code; a full `meet.google.com/...` URL also works
(pass it as `meeting_number` or `meeting_url` — either is auto-detected). `passcode` is
accepted by the request schema but **ignored** here — Meet has no joiner-supplied secret.
`display_name` only matters if the profile has somehow lost its Google session and Meet falls
back to asking for a name; a signed-in profile joins under the account's own name regardless
of what's sent.

In practice this call is normally made by `calendar-orchestrator`, not by hand — see
[calendar-orchestrator.md](../calendar-orchestrator.md).

## Status

Google Meet is now the most exercised connector in practice, ahead of its original
"roadmap only" design status — see the avatar-agent repo's own `README-gateway.md` for the
specific run sequence used to verify it end to end (also reproduced in
[RUNBOOK.md](../RUNBOOK.md)).

## Troubleshooting

- **Join fails with `GoogleAuthError`** — the profile's Google session expired; re-run
  `tools/meet_signin.py`.
- **Avatar joins but the room hears nothing / the avatar hears nothing** — check
  `tools/meet_inspect.py`'s selector report first; Meet's DOM changes over time and this
  connector degrades to silence on a missed selector rather than raising.
- **Headless + no speakers**: `MC_MEDIA__ECHO_GATE_HANGOVER_MS` doesn't need to be raised here
  the way it does on RTMS-mode Zoom-web — the tap is structurally echo-free. If barge-in feels
  unresponsive in a fully headless deployment, set it to `0` so interruption relies entirely
  on the agent's own handling rather than the gate.
