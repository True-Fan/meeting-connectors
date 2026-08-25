# Zoom connectors: `zoom` and `zoom_web`

There are **two** Zoom connectors, and they exist for opposite reasons:

| | `zoom` (Meeting SDK) | `zoom_web` (browser) |
|---|---|---|
| Joins as | The Meeting SDK for Linux, via a C++ sidecar | An ordinary browser participant, via Playwright |
| Ingest | RTMS (WebSocket) only | RTMS **or** tapped from the page (`ingest_mode`) |
| Needs from the meeting's host account | RTMS enabled for this app | Nothing |
| Use when | The bridge's own account hosts the meeting | The meeting is someone else's (an invite) |

**A meeting the bot was only invited to is, by definition, someone else's** — it cannot carry
an entitlement the operator's app was granted. That's the whole reason `zoom_web` exists
alongside `zoom` rather than replacing it: `calendar-orchestrator` always resolves an invited
Zoom meeting to `zoom_web` for exactly this reason (see
[calendar-orchestrator.md](../calendar-orchestrator.md#platforms)).

---

## `zoom` — RTMS ingest + Meeting SDK publish

### Architecture

```
                        Zoom meeting
                     ┌──────┴───────┐
                     │              │
              RTMS (WebSocket)      │ Meeting SDK (native, in-process C++)
                     │              │
                     ▼              ▼
        src/connectors/zoom/rtms/   src/connectors/zoom/publisher/
        (pure Python)               (Python IPC client)
                     │                     │  Unix domain socket, frozen wire protocol (ZMC1)
                     │                     ▼
                     │           C++ sidecar (same container)
                     │           ── Zoom Meeting SDK for Linux ──▶ publishes into the meeting
                     ▼
         (shared media pipeline — LLD.md §7)
```

**These are two independently-recovering legs, not one.** RTMS cannot publish media at all —
every RTMS message type is either inbound media, inbound metadata, or bidirectional control;
none carries application media toward Zoom. So ingest and publish fail and reconnect on
entirely separate schedules, and the session's health is the pair of them, not one number.

### RTMS handshake (ingest)

```
signaling socket:  connect → msg 1 (signature)      ← msg 2 (media server URLs)
media socket:      connect → msg 3 (media_type=AUDIO) ← msg 4 (status 0)
signaling socket:  → msg 7 CLIENT_READY_ACK
media socket:      ← msg 14 MEDIA_DATA_AUDIO (repeating)
```
Both sockets answer `KEEP_ALIVE_REQ` (msg 12) with `KEEP_ALIVE_RESP` (msg 13); a
`KeepAliveWatchdog` treats 60s of silence on either socket as fatal and forces a reconnect.
Transcript and chat, if subscribed, each get their **own** media-socket connection, opened
last — Zoom validates `media_type` as a single value, not a bitmask, so combining audio with
anything else in one handshake gets the whole socket rejected; keeping them separate means a
refused transcript subscription can never take down the audio socket that already works.

RTMS is negotiated for **exactly** the avatar's own input format (16kHz mono S16LE), derived
from `AVATAR_INPUT_FORMAT` rather than hardcoded — so there is zero resampling anywhere on
this ingest path, and a future change to the avatar's contract fails loudly at startup instead
of silently degrading.

**RTMS cannot resume across a reconnect.** Every reconnect is a full re-handshake; audio
during the gap is permanently lost and logged as such.

### Meeting SDK publish (the C++ sidecar)

The sidecar's one job is publishing frames into the meeting via the Zoom Meeting SDK — it
makes no decisions about session state, reconnection policy, or when to speak; all of that is
Python's. Python mints a short-lived (30 min) SDK JWT and hands it to the sidecar inside the
`CONTROL_JOIN` message, so the C++ binary never holds a long-lived credential. The wire
protocol between them (`ZMC1`, 24-byte header, `VIDEO_I420`/`AUDIO_PCM`/`CONTROL_JOIN`/
`CONTROL_LEAVE`/`HEARTBEAT`/`READY`/`ERROR`) is **frozen** — both sides are reference-tested
against it byte-for-byte.

```
Python: CONTROL_JOIN {sdk_jwt, meeting_number, passcode, display_name, video{...}, audio{...}}
Sidecar: InitSDK → SDKAuth(jwt) → HasRawdataLicense() check → READY {participant_id, ...}
                                                             (or fatal ERROR if no license)
```
A missing raw-data license fails the join loudly and immediately — this is the one check that
gates whether publish can ever work at all for this credential.

**Current build status**: the sidecar in this repo builds in **stub SDK mode**
(`ZOOM_SDK_ROOT` unset) — it speaks the real IPC/framing/threading/pacing correctly and is
verified end-to-end against that, but it is not linked against Zoom's real Meeting SDK for
Linux, and the checked-in build artifact targets macOS, not the Linux target a real deployment
needs. Treat "Zoom publish" as protocol-complete and SDK-integration-pending — see
`src/connectors/zoom/publisher/sidecar/README`/`CMakeLists.txt` before assuming a real build
will work unmodified.

### Setup: two Zoom apps

| App | Type | Grants | Settings |
|---|---|---|---|
| **General App** | Zoom Marketplace app with RTMS + Meeting SDK features | Webhook signature (`meeting.rtms_started`/`stopped`), RTMS handshake signature, Meeting SDK JWT signing | `MC_ZOOM__CLIENT_ID`, `MC_ZOOM__CLIENT_SECRET`, `MC_ZOOM__WEBHOOK_SECRET_TOKEN`, `MC_ZOOM__SDK_KEY`, `MC_ZOOM__SDK_SECRET` |
| **Server-to-Server OAuth App** | S2S app with scope `meeting:update:participant_rtms_app_status` | Triggering RTMS to start via REST, without waiting for a manual toggle | `MC_ZOOM__ACCOUNT_ID`, `MC_ZOOM__S2S_CLIENT_ID`, `MC_ZOOM__S2S_CLIENT_SECRET` |

Register a webhook endpoint at `https://<host>/webhooks/zoom` for `meeting.rtms_started` and
`meeting.rtms_stopped`; Zoom's endpoint-validation challenge is answered automatically (signed
with `MC_ZOOM__WEBHOOK_SECRET_TOKEN`) as soon as the bridge is reachable at that URL. The
OAuth "Add App" install flow redirects to `GET /oauth/zoom/callback` — this repo's endpoint is
a landing page only; neither RTMS nor the SDK JWT actually exchanges that code at runtime.

> **Note on `MC_ZOOM__RTMS_AUTO_START`**: `ZoomSettings` and `RtmsTrigger`
> (`src/connectors/zoom/api/rtms_trigger.py`) implement calling Zoom's REST API to start RTMS
> automatically when a session is created. As of this writing that trigger is wired into the
> **`zoom_web`** connector's session start, not into the native `zoom` connector — so for
> `platform: "zoom"`, RTMS must currently be started by an account auto-start rule or a manual
> toggle in the Zoom web portal before (or shortly after) `POST /sessions` is called.

### Run

```bash
.venv/bin/uvicorn src.main:app --port 8000
```

### Join a meeting

```bash
curl -X POST localhost:8000/sessions -H 'content-type: application/json' \
  -d '{"meeting_number": "1234567890"}'
```
`platform` defaults to `"zoom"`, so this is the minimal call. Add `"passcode"` if the meeting
has one, and `"meeting_uuid"` if you need to pin a specific inbound RTMS stream to this exact
session rather than the default "match by arrival order" behaviour.

---

## `zoom_web` — join as a browser participant

### Architecture

Same page-bridge recipe as [Google Meet](google-meet.md) and [Teams-web](teams.md#teams_web),
with one addition specific to Zoom: a persistent Chromium profile with **a microphone already
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
   reuses connectors/zoom/rtms/    tapped from the page's own
   (same as native zoom conn.)     playout graph + DOM (roster,
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

### Setup

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

### Run

```bash
.venv/bin/uvicorn src.main:app --port 8000
```

### Join a meeting

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
| RTMS ingest (`zoom`, `zoom_web` in `rtms` mode) | ✅ built and verified against Zoom's real handshake |
| Meeting SDK publish (`zoom`) | ✅ IPC/control flow verified against a stub; real Linux SDK link is a separate milestone |
| `zoom_web` bridge/join logic | ✅ built and unit-tested |
| `zoom_web` in `browser` ingest mode | ⚠️ selectors not yet verified against a live meeting |

## Troubleshooting

- **Avatar joins, publishes idle video, never speaks or is never heard (Zoom-web)** — the
  single most common cause is a profile with no microphone selected; re-run
  `scripts/zoom_web_login.py` and confirm the selection step specifically.
- **Agent answers its own words in a loop (Zoom-web, `rtms` mode)** — raise
  `MC_ZOOM_WEB__ECHO_GATE_HANGOVER_MS`; this is the exact symptom of too short a value on this
  path.
- **RTMS socket silently stops delivering audio** — check for a `KeepAliveTimeoutError` in the
  logs; RTMS gives no resume, so a reconnect after a gap is expected to show a logged gap
  duration, not silently continue.
- **A `meeting.rtms_started` webhook arrives with no session to bind** — expected if the
  webhook beats `POST /sessions`; it's parked for up to 45s and claimed automatically once the
  session exists. Longer than 45s apart and the binding expires — check ordering upstream.
