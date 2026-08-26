# Zoom connector (`zoom_web`)

The bot joins a Zoom meeting as an ordinary browser participant — a real, headless Chromium
tab via Playwright, the same recipe used for [Google Meet](google-meet.md) and
[Teams](teams.md#teams_web). Nothing needs to be granted by whoever owns the meeting; this is
what `calendar-orchestrator` always uses for an invited meeting (see
[calendar-orchestrator.md](../calendar-orchestrator.md#platforms)).

## Architecture

One addition specific to Zoom: a persistent Chromium profile with **a microphone already
selected**. Zoom will not start its own capture pipeline until its device menu has a
selection, and that selection lives in the profile's `Default/Preferences` — a throwaway
profile means Zoom publishes nothing, however correct the injected synthetic track is. This
was measured, not assumed, which is why `scripts/zoom_web_login.py` exists and spells out that
step explicitly.

```
Playwright ──▶ Chromium (persistent profile, mic pre-selected)
                    │
        navigates to app.zoom.us/wc/{meeting_number}/join
                    │
        ┌───────────┴────────────┐
   ingest_mode=rtms          ingest_mode=browser
        │                          │
   RTMS WebSocket                  tapped from the page's own
   (structured events)             playout graph + DOM (roster,
        │                          speaker, chat, captions)
        └───────────┬──────────────┘
                     ▼
        loopback WebSocket (ZWB1) ──▶ PageAudioSource / ZoomWebMediaSink
                     │
        (shared media pipeline — LLD.md §7)
```

`ingest_mode` (`MC_ZOOM_WEB__INGEST_MODE`, `browser` by default) is the single most
consequential setting on this connector:

| | `rtms` | `browser` |
|---|---|---|
| Requires | RTMS enabled for this app on the hosting account | Nothing |
| Audio | RTMS stream | Tapped from Zoom's own playout |
| Who's present / speaking / what was said | Structured events, exact | DOM scraping — degrades if Zoom's UI changes |
| Echo gate | **Strict** — RTMS delivers the mix *including* the avatar, round-trip well over a second; hangover needs to be long (see below) | **Backstop only** — the synthetic mic never reaches the tap, so the avatar's voice is structurally absent |
| Barge-in trigger | Zoom's `ACTIVE_SPEAKER_CHANGE` event | Audio energy + DOM |

Because a meeting the bot was invited to can't carry the RTMS entitlement, `browser` is both
the default and, in practice, the only mode that works for an invited meeting — which is also
why `calendar-orchestrator` never needs to know this switch exists.

**If running `rtms` mode**, `MC_ZOOM_WEB__ECHO_GATE_HANGOVER_MS` needs to be set well above the
shared default (200ms) — RTMS here carries the avatar's own voice back in the mix, and a short
hangover was observed, live, causing the agent to transcribe and answer the tail of its own
sentences in a loop. **Leave it unset in `browser` mode** — a long hangover there would make
barge-in detection deaf for no benefit, since the echo it exists to prevent can't happen on
that path.

## Setup

### 0. The browser binary

`pip install -e .` at the repo root already pulls in the `playwright` package — this
connector is Playwright-driven, same as [Google Meet](google-meet.md). What it does not do is
fetch the browser itself, which is a separate, one-time step:

```bash
.venv/bin/playwright install chromium
```

Skip this and the first session fails with "chromium is not installed for playwright" rather
than joining. See [google-meet.md § Setup](google-meet.md#1-install-the-browser-binary) for
why this is a separate step from installing the package.

### 1. The persistent profile

```bash
.venv/bin/python scripts/zoom_web_login.py --profile ~/.mc/zoom-web-profile
```
This launches a **headed** Chromium with real permission prompts (deliberately not faked).
Sign in, join any meeting, click "Join Audio by Computer", and — the step that matters —
**explicitly select a microphone** in the device menu before closing the browser. The profile
only saves cleanly on a normal context close.

```bash
MC_ZOOM_WEB__ENABLED=true
MC_ZOOM_WEB__PROFILE_DIR=~/.mc/zoom-web-profile
```
Opt-in on purpose (`enabled`, default `false`): it takes no credentials of its own to infer
"wanted" from, and it carries a host dependency (a real capture device) that should never
appear in a deployment that didn't ask for it.

### Optional: RTMS ingest mode

Only needed if you're setting `MC_ZOOM_WEB__INGEST_MODE=rtms` for a meeting the bot's own
Zoom account hosts (an invited meeting can never use this — see the table above). It needs a
Zoom Marketplace app plus a webhook endpoint:

| App | Grants | Settings |
|---|---|---|
| **General App** (RTMS feature) | Webhook signature (`meeting.rtms_started`/`stopped`), RTMS handshake signature | `MC_ZOOM__CLIENT_ID`, `MC_ZOOM__CLIENT_SECRET`, `MC_ZOOM__WEBHOOK_SECRET_TOKEN` |
| **Server-to-Server OAuth App** (scope `meeting:update:participant_rtms_app_status`) | Lets the bridge trigger RTMS to start via REST, without a manual toggle | `MC_ZOOM__ACCOUNT_ID`, `MC_ZOOM__S2S_CLIENT_ID`, `MC_ZOOM__S2S_CLIENT_SECRET` |

Register a webhook endpoint at `https://<host>/webhooks/zoom` for `meeting.rtms_started` and
`meeting.rtms_stopped`; Zoom's endpoint-validation challenge is answered automatically (signed
with `MC_ZOOM__WEBHOOK_SECRET_TOKEN`) as soon as the bridge is reachable at that URL.
`MC_ZOOM__RTMS_AUTO_START` (default on) makes `zoom_web`'s own session start trigger RTMS via
that S2S app automatically, rather than waiting on an account auto-start rule or a manual
toggle in the Zoom web portal.

RTMS is negotiated for **exactly** the avatar's own input format (16kHz mono S16LE), so there
is zero resampling anywhere on this ingest path. **RTMS cannot resume across a reconnect** —
every reconnect is a full re-handshake, and audio during the gap is permanently lost and
logged as such.

## Run

```bash
.venv/bin/uvicorn src.main:app --port 8000
```

## Join a meeting

```bash
curl --location 'localhost:8000/sessions' \
  --header 'content-type: application/json' \
  --data '{
    "platform": "zoom_web",
    "meeting_number": "95097700824",
    "passcode": "868339"
  }'
```

## Status

| Leg | State |
|---|---|
| `zoom_web` bridge/join logic | ✅ built and unit-tested |
| `zoom_web` in `browser` ingest mode | ⚠️ selectors not yet verified against a live meeting |
| `zoom_web` in `rtms` ingest mode | ✅ built and verified against Zoom's real handshake |

## Troubleshooting

- **Avatar joins, publishes idle video, never speaks or is never heard** — the single most
  common cause is a profile with no microphone selected; re-run `scripts/zoom_web_login.py`
  and confirm the selection step specifically.
- **Agent answers its own words in a loop (`rtms` mode)** — raise
  `MC_ZOOM_WEB__ECHO_GATE_HANGOVER_MS`; this is the exact symptom of too short a value on this
  path.
- **RTMS socket silently stops delivering audio** — check for a `KeepAliveTimeoutError` in the
  logs; RTMS gives no resume, so a reconnect after a gap is expected to show a logged gap
  duration, not silently continue.
- **A `meeting.rtms_started` webhook arrives with no session to bind** — expected if the
  webhook beats `POST /sessions`; it's parked for up to 45s and claimed automatically once the
  session exists. Longer than 45s apart and the binding expires — check ordering upstream.
