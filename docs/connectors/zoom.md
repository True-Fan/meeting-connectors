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
        tapped from the page's own playout graph (audio)
        + DOM (roster, speaker, chat, captions)
                    │
                    ▼
        loopback WebSocket (ZWB1) ──▶ PageAudioSource / ZoomWebMediaSink
                    │
        (shared media pipeline — LLD.md §7)
```

**There used to be a second ingest leg here: Zoom's RTMS API.** It was a strictly better
signal — audio, transcript, chat and participant events all arriving as data with a name
attached, nothing depending on markup Zoom can rename. It required the meeting to be hosted
on an account with RTMS enabled for the app, and a meeting the bot was merely *invited* to can
never carry that entitlement. Since that is the case this bridge exists for, the leg was
removed along with the `MC_ZOOM__*` credentials, the `/webhooks/zoom` endpoint and the
Meeting-SDK connector it shared them with.

One consequence worth knowing: `MC_ZOOM_WEB__ECHO_GATE_HANGOVER_MS` should be left **unset**.
It existed because RTMS carried the avatar's own voice back in the mix. The page tap does not
— Zoom never plays a participant their own microphone — so a long hangover now only makes
barge-in detection deaf, for no benefit.

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

**No Zoom Marketplace app, credentials, or webhook endpoint is needed.** Those were only ever
required by the removed RTMS leg; this connector joins as a guest and needs nothing registered
on Zoom's side.

### 2. Captions, if you want a transcript

```bash
MC_ZOOM_WEB__CAPTIONS_AUTO_ENABLE=true
```
Turning captions on is a **visible action in somebody else's meeting**, which is why it is a
setting rather than default behaviour. It is also the only way this connector can answer "what
did they say" — there is no invisible per-participant transcription available to a guest.

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
| Page tap (audio ingest + publish) | ✅ confirmed attaching by the Web Audio path in a live meeting |
| DOM observers (roster, speaker, chat, captions) | ⚠️ selectors not yet verified against a live meeting |

## Troubleshooting

- **Avatar joins, publishes idle video, never speaks or is never heard** — the single most
  common cause is a profile with no microphone selected; re-run `scripts/zoom_web_login.py`
  and confirm the selection step specifically.
- **Avatar never reacts to a raised hand, chat, or somebody speaking** — these are DOM
  observers, so a Zoom UI change is the first suspect. Run with
  `MC_ZOOM_WEB__HEADLESS=false` and watch whether the participants/chat panel actually opens;
  the selectors are data, in `connectors/zoom_web/automation/selectors.py`.
- **Barge-in never fires** — check `MC_ZOOM_WEB__ECHO_GATE_HANGOVER_MS` is unset. A large
  value left over from the removed RTMS leg withholds inbound audio during exactly the window
  an interruption arrives in.
- **No transcript** — captions have to be on in the meeting; see
  `MC_ZOOM_WEB__CAPTIONS_AUTO_ENABLE` above.
