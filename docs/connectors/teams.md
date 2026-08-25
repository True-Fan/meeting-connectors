# Microsoft Teams connectors: `teams` and `teams_web`

Same split as Zoom, for the same underlying reason:

| | `teams` (Graph app-hosted media) | `teams_web` (browser) |
|---|---|---|
| Joins as | A bot via Microsoft Graph's app-hosted media, through a .NET sidecar | An anonymous guest, via Playwright |
| Needs | A tenant-consented Azure AD app **plus a Windows host** | Nothing |
| Use when | The organization owns the meeting and can consent the app | The meeting is someone else's (an invite) |

An invited meeting can supply neither a tenant-consented Azure app nor is guaranteed to even
belong to a tenant at all (personal "Teams for Life" meetings have none) — so
`calendar-orchestrator` always resolves an invited Teams link to `teams_web`. See
[calendar-orchestrator.md](../calendar-orchestrator.md#platforms).

---

## `teams` — Graph app-hosted media

### Why this one needs a separate Windows machine

The only officially supported way to get frame-level audio/video into a Teams call is
`Microsoft.Graph.Communications.Calls.Media` — a **Windows x64, .NET Framework 4.7.2** library
with native media-platform binaries. No Linux build, no .NET Core build, no 32-bit build
exists. The bridge itself runs in a Linux container, so unlike Zoom's C++ sidecar (same
container, shared volume, Unix socket), the Teams sidecar **must** be a separate machine —
this is a platform constraint, not a design choice.

```
Linux container                              Windows host
┌─────────────────────────┐   TCP + TLS     ┌───────────────────────────────────┐
│ meeting-connectors        │◀───────────────▶│ mc-teams-sidecar (.NET)            │
│ (pipeline, pacing, avatar) │   'TMC1' wire   │ Graph Communications Calls Media   │──▶ Graph API
└─────────────────────────┘                  │  • one LocalMediaSession,          │
                                              │    both directions                 │
                                              │  • /api/calls notification listener│◀── Graph notifications
                                              └───────────────────────────────────┘
```

**One `LocalMediaSession` carries both directions** — unlike Zoom's two independently-failing
legs (RTMS ingest, sidecar publish), Teams' ingest and publish share one Graph call and
therefore report identical health. Recovery is always a **full rejoin**: a media session
cannot be reattached to a call whose signalling already ended, so any recoverable failure
re-creates the Graph call from scratch rather than resuming.

Graph call-lifecycle notifications terminate **on the Windows sidecar**, not in Python — so
this connector adds no FastAPI route; `src/api` is untouched.

### Setup

**1. Azure AD app registration** (application permissions, admin-consented — the bot acts as
itself, not on a signed-in user's behalf):
- `Calls.JoinGroupCall.All` — join a scheduled meeting
- `Calls.AccessMedia.All` — app-hosted media; **without this, the join succeeds and no media
  ever flows**, which is the failure mode to watch for first
- Register the app as a **bot** with the Teams channel enabled; its calling webhook is
  `https://<sidecar-fqdn>:<media-public-port>/api/calls`

**2. A Windows host** (Server 2019+, or Windows 10/11 for dev), x64, .NET Framework 4.7.2+,
with:
- A **publicly resolvable DNS name** and a **publicly trusted TLS certificate** matching it —
  a self-signed cert fails opaquely inside `MediaPlatform.Initialize`
- Two distinct certificates: one publicly trusted (media platform + Graph notifications), one
  that can be internally issued (the bridge↔sidecar IPC link, pinned via
  `MC_TEAMS__SIDECAR_CA_FILE`)
- Inbound TCP open on the media-public port and the notification port

**3. Build and run the sidecar** (`src/connectors/teams/sidecar/dotnet/`):
```powershell
msbuild MeetingConnectors.Teams.Sidecar.csproj /p:Configuration=Release /p:Platform=x64
.\bin\x64\Release\mc-teams-sidecar.exe `
  --service-fqdn <your-fqdn> `
  --media-cert-thumbprint <thumbprint> `
  --ipc-cert-thumbprint <thumbprint> `
  --ipc-listen 0.0.0.0 --ipc-port 8445 --ipc-require-client-cert
```

**4. Configure the bridge:**
```bash
MC_TEAMS__TENANT_ID=...
MC_TEAMS__CLIENT_ID=...
MC_TEAMS__CLIENT_SECRET=...
MC_TEAMS__SIDECAR_HOST=<windows-host>
MC_TEAMS__SIDECAR_PORT=8445
MC_TEAMS__SIDECAR_CA_FILE=/path/to/ca.pem
```

### Join sequence

`POST /sessions {"platform": "teams", ...}` runs the Teams join **synchronously inside session
creation** (unlike Zoom, which waits asynchronously on a webhook) — a bad credential or an
unparseable meeting URL fails the `POST /sessions` call itself with a precise error, rather
than surfacing later as a degraded health check:

```
Python                                          .NET sidecar
  resolve_join_descriptor(meeting) ──────────▶
  CONTROL_JOIN {join, auth, audio, video} ────▶  client.Calls().AddAsync(...)   [Graph API]
                                          ◀────  READY {callId, audioSampleRateHz, ...}
                                                  (or fatal ERROR — e.g. missing consent)
```

Two join routes, chosen by what's in the request:
- **Meeting ID** — a numeric Teams "Meeting ID" (+ optional passcode).
- **Chat info** — a `teams.microsoft.com/l/meetup-join/<threadId>/...?context=...` link,
  parsed for the thread id, tenant id, and organizer id it carries.

### Run

```bash
.venv/bin/uvicorn src.main:app --port 8000
```

### Join a meeting

```bash
curl -X POST localhost:8000/sessions -H 'content-type: application/json' \
  -d '{"platform": "teams", "meeting_number": "123456789012", "passcode": "abc123"}'

# or, by join link:
curl -X POST localhost:8000/sessions -H 'content-type: application/json' \
  -d '{"platform": "teams", "meeting_url": "https://teams.microsoft.com/l/meetup-join/..."}'
```

### Status

Bridge-side logic (join-descriptor resolution, wire protocol, TLS, reconnect/rejoin state
machine) is **built and tested against an in-process fake sidecar**. The .NET side is
**written against Microsoft's documented API surface and not yet compiled or run** — no
Windows host, Azure tenant, or admin consent was available during development. The wire codec
itself is pinned by a direct Python↔C# conformance test, so that part is verified independent
of the rest.

---

## `teams_web` — join as a browser participant

### Architecture

Same page-bridge recipe as [Google Meet](google-meet.md) and [Zoom-web](zoom.md#zoom_web), and
the simplest of the three (no ingest-mode branch — the Graph route's entitlements are simply
unavailable to a guest, so there's only ever one way in):

```
Playwright ──▶ Chromium (profile optional — no persisted mic selection needed)
                    │
        resolves a join LINK or a meeting ID:
          teams.microsoft.com/l/meetup-join/...      (work/school link)
          teams.microsoft.com/meet/<id>?p=...         (work/school short link)
          teams.live.com/meet/<id>?p=<passcode>       (personal / "Teams for Life")
          <numeric id> + passcode, typed into the join form
                    │
        clicks past the "open the Teams app?" launcher
                    │
        loopback WebSocket (TWB1) ──▶ PageAudioSource / TeamsWebMediaSink
                    │
        (shared media pipeline — LLD.md §7)
```

Unlike Zoom-web, **no persisted microphone selection is required** — Teams accepts whatever
track `getUserMedia` returns, so an anonymous guest join works from a throwaway profile. A
profile is still worth setting for two reasons: a *signed-in* profile joins as a named tenant
user instead of a guest (which some organisers require, and skips the lobby), and it keeps
device/consent prompts from reappearing every session.

**A bare numeric meeting id doesn't say whether it's a personal or work/school meeting** —
both are 9–13 digits with no way to tell apart. The joiner tries the work/school form first
and, only if the page responds to nothing for a few polls, navigates to the personal
`teams.live.com/meet/<id>` form instead — a fact about the page's behaviour, not a guess from
the id's shape. Because of this ambiguity, `calendar-orchestrator` always sends the full
`meeting_url` for Teams (unlike Zoom/Meet, which send only the numeric id) — see
[calendar-orchestrator.md](../calendar-orchestrator.md#teams-links-and-why-this-one-also-sends-meeting_url).

**Teams' page CSP blocks the loopback WebSocket** unless disabled for this connector's
browser context — Chromium hands back a socket already `CLOSED` with no error and no `close`
event, which is silent and easy to mistake for "the connector isn't trying." This is on by
default (`bypass_csp: true`); the browser is ours, headless, visits exactly one site, and the
channel carries a per-session compared-in-constant-time token — nothing on that wire is a
credential exposed by turning this off elsewhere.

### Setup

```bash
.venv/bin/python scripts/teams_web_login.py --profile ~/.mc/teams-web-profile
```
Optional, unlike Zoom-web's equivalent — but if you run it, **leave the microphone unmuted**
on the pre-join screen; Teams persists that toggle into the next call.

```bash
MC_TEAMS_WEB__ENABLED=true
MC_TEAMS_WEB__PROFILE_DIR=~/.mc/teams-web-profile   # optional
```

### Run

```bash
.venv/bin/uvicorn src.main:app --port 8000
```

### Join a meeting

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
`passcode` fills the passcode field for the meeting-id route, or is appended as `?p=` when the
connector falls back to the personal-link form.

### Status

⚠️ Built and unit-tested; selectors have **not yet been verified against a live meeting**.

## Troubleshooting

- **`teams_web` channel looks connected but nothing ever arrives** — check for
  `bypass_csp: false` first; a blocked CSP fails completely silently (no error, no close
  event) and looks identical to "the connector never tried."
- **A `teams.live.com` link lands on the app home instead of joining** — confirms the
  work/school→personal fallback didn't trigger; check `live_url_template` isn't empty and that
  the id was recognised as ambiguous rather than confidently (and wrongly) routed.
- **`teams` connector: join succeeds, no audio/video ever flows** — check `Calls.AccessMedia.All`
  is actually admin-consented, not just requested; a missing grant here is the single most
  common cause and Graph's error in this case is not always obviously about permissions.
- **`teams` connector: `MediaPlatform.Initialize` fails on the sidecar** — almost always a
  certificate/FQDN mismatch; confirm the media certificate's subject matches `--service-fqdn`
  exactly.
