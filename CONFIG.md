# Testing Configuration — Platform Portals + Local Setup

**Zoom** is §1–§9. **Microsoft Teams** is §10, and its prerequisites are entirely
different — an Azure AD app registration, a Windows host, and two certificates rather
than a Marketplace app and a webhook URL.

**Goal of this guide:** get your own code (RTMS ingest, session lifecycle, stub sidecar,
mock avatar) exercised end-to-end with dummy/stub data — **not** a real, visible
participant in a Zoom meeting. That's a separate, much bigger task (real Meeting SDK
for Linux, Anonymous Join Exception approval, production build) and nothing below is
required for it.

Because of that scope, **most of the Zoom Marketplace UI does not need to be touched.**
Section 5 lists exactly what to skip, and why — read it first if anything below feels
like it should require more portal setup than it does.

Verified against Zoom's current developer docs as of writing this file. Zoom's UI
changes without notice; if a label below doesn't match what you see, the *shape* of
the flow (Access → Event Subscriptions → Validate → Save) is more durable than the
exact button text.

---

## 1. What you actually need

| Thing | Needed? | Why |
|---|---|---|
| **General App** (you already have this — "General app 898") | Yes | RTMS scopes, webhook, `CLIENT_ID`/`CLIENT_SECRET` |
| RTMS scopes + event subscription on that app | Yes | The only real external input your code depends on |
| Correct OAuth redirect URL on that app | Yes | Otherwise reinstalling/re-authorizing 405s |
| Server-to-Server OAuth app (you already have this) | Optional | Only if you try the on-demand REST trigger in §4.3 |
| Meeting SDK "Embed" toggle, Anonymous Join Exception, Beta Test/publish flow | **No** | Only matters for a *real* Meeting SDK join — see §5 |

---

## 2. General App setup (Zoom Marketplace)

Go to **[marketplace.zoom.us](https://marketplace.zoom.us) → Develop → Build App →** open your existing **General App** (do not create a new one).

### 2.1 Basic Info / App Credentials
Nothing to change here if the app already installs — this is where `Client ID` /
`Client Secret` live, which you've already copied into `MC_ZOOM__CLIENT_ID` /
`MC_ZOOM__CLIENT_SECRET`.

**Use the Development credentials, not Production.** Zoom shows both once an app
exists; Production only activates after the app is actually submitted and approved
through Marketplace review ("Submit for Review") — which you are deliberately not
doing. Local Test, the Beta Test screen, and the webhook `Validate` button in §2.3 all
run against the **Development** `Client ID`/`Client Secret` regardless of app state,
so that's what belongs in every Zoom credential slot in `.env` (§3), including
`SDK_KEY`/`SDK_SECRET`.

**Fix the OAuth redirect URL here** (Basic Info → OAuth Information, or wherever your
UI shows "Redirect URL for OAuth"): set it to
```
https://<your-ngrok-domain>/callback
```
**not** `.../webhooks/zoom/`. This repo now has a real `/callback` route
([router.py](src/connectors/zoom/oauth/router.py)) that just returns `200` — it exists
purely so this redirect has somewhere sane to land instead of 405ing against the
webhook endpoint.

### 2.2 Scopes tab
- Left nav → **Scopes** → **+ Add Scopes**
- Search `RTMS`, add the RTMS-related scopes shown (e.g. audio access under
  Realtime Media Streams) → **Done** → provide the one-line description Zoom asks for
  → **Save**.

### 2.3 Features → Access tab (this is where the webhook lives)
Zoom nests "Event Subscriptions" under **Features → Access**, not a standalone tab.

1. Left nav → **Features** → **Access**.
2. Under **General Features**, toggle **Event Subscriptions** on.
3. **Add New Event Subscription** (name it anything, e.g. `rtms`).
4. **Add Events** → search `RTMS` → select **Meeting RTMS Started** and
   **Meeting RTMS Stopped** → **Done**.
5. **Event notification endpoint URL** → enter:
   ```
   https://<your-ngrok-domain>/webhooks/zoom/
   ```
6. Click **Validate**. This is a manual button, not automatic-on-save — it fires the
   `endpoint.url_validation` challenge right now, live, against whatever is currently
   running at that URL. Your uvicorn + ngrok must already be up for this to succeed.
   - Success → a "Validated" message appears under the URL field.
   - `401`/signature mismatch → the **Secret Token** shown on this same panel doesn't
     match `MC_ZOOM__WEBHOOK_SECRET_TOKEN` in your `.env`. Copy it fresh from here,
     not from memory — this is a different value from Client Secret, easy to mix up.
7. Only once validated: click **Save**. Zoom won't let you save before that.

### 2.4 Local Test tab
- **Add App Now** → **Allow**. This (re-)authorizes the app on your own account —
  needed any time scopes change. This is the *only* install/authorize action you
  need; skip "Share outside your account" and the "Ready for beta test" flow
  entirely — those are for distributing to other Zoom accounts, not for you testing
  your own code.

---

## 3. `.env` checklist

```bash
# From General App → Basic Info → App Credentials
MC_ZOOM__CLIENT_ID=<Client ID>
MC_ZOOM__CLIENT_SECRET=<Client Secret>

# From General App → Features → Access → your event subscription's Secret Token
MC_ZOOM__WEBHOOK_SECRET_TOKEN=<Secret Token>

# Reuse the same Client ID/Secret — Zoom merged the once-separate Meeting SDK
# credential path into these (see .env's own comment for why). Any non-empty
# string works here for the stub sidecar; it never contacts Zoom to check them.
MC_ZOOM__SDK_KEY=<same Client ID>
MC_ZOOM__SDK_SECRET=<same Client Secret>

# Local, not from the portal
MC_SIDECAR__UDS_PATH=/tmp/mc-sidecar/sidecar.sock   # /run doesn't exist on macOS
MC_AVATAR__URL=ws://localhost:8100/stream            # matches mock_avatar.py
```

After any `.env` edit: **fully restart uvicorn** (`Ctrl+C`, rerun). `--reload` only
watches `.py` files, not `.env` — this bit us multiple times this session.

---

## 4. Actually triggering RTMS for a test meeting

Starting a normal meeting does **nothing** by itself — Zoom only sends
`meeting.rtms_started` if RTMS was explicitly triggered. Three ways, in order of
likely viability for a free/Basic account with no Account Admin panel:

### 4.1 Account auto-start (skip — needs Admin role)
Account Management → Advanced → Realtime Media Streams → pick your app as an
auto-start app. Not reachable without Admin, which a free account doesn't have.

### 4.2 Zoom Apps SDK `startRTMS()` (needs an in-meeting app panel)
If your General App has a Zoom Apps in-meeting UI, a button there calling
`zoomSdk.startRTMS()` triggers it for that meeting. Only useful if you build that panel.

### 4.3 On-demand REST call (your most likely path — uses the Server-to-Server app)
```
PATCH https://api.zoom.us/v2/live_meetings/{meetingId}/rtms_app/status
Authorization: Bearer <Server-to-Server access token>
Content-Type: application/json

{"action": "start", "settings": {"client_id": "<General App's Client ID>"}}
```
- Requires your **Server-to-Server OAuth app** to have the Realtime Media Streams
  feature enabled and the `meeting:update:participant_rtms_app_status` scope.
- Get the bearer token via the S2S app's client-credentials grant
  (`account_id` + S2S `client_id`/`client_secret` — a separate, already-existing
  credential set on that app, not any of the General App values above).
- Call this **after** the meeting is live, with the real numeric meeting ID.

**Honest caveat:** several developers on Zoom's forum report RTMS webhooks never
arriving even after doing all of the above correctly — Zoom sometimes needs to
manually flip a backend "RTMS enablement" flag for a specific App ID, especially on
Basic/free accounts. If §2.3's `Validate` succeeds (proving your endpoint is
reachable and correctly signed) but no real `meeting.rtms_started` ever shows up
after triggering §4.3, that's a Zoom Developer Forum support request, not a
config or code problem — see §6.

---

## 5. What you do **not** need for this level of testing

Skip all of this — it only matters once you have a real Zoom Meeting SDK for Linux
build (a separate, later task):

- **Features → Embed → Meeting SDK toggle** — the stub sidecar (`sdk_version=stub-no-sdk`)
  never calls a real SDK, so nothing checks this.
- **Anonymous Join Exception request** — only gates a *real* `JoinMeeting()` call,
  which the stub never makes.
- **Beta Test / "Share outside your account" / Submit for Review** — app distribution,
  unrelated to testing your own code on your own account.

If you find yourself on any of these screens while chasing "why doesn't my session
join," you've likely wandered from testing your code into the real-SDK rabbit hole.

---

## 6. Local process checklist

```bash
# 1. Generate a dummy fMP4 clip for mock_avatar.py to stream back (10s, blue screen, silence)
brew install ffmpeg   # if not already installed
ffmpeg -f lavfi -i color=c=blue:s=1280x720:d=10 \
       -f lavfi -i anullsrc=r=32000:cl=mono \
       -c:v libx264 -c:a aac -movflags +frag_keyframe+empty_moov \
       -t 10 dummy_avatar.mp4

# 2. Start the mock avatar server (needs dummy_avatar.mp4 in the cwd)
python3 mock_avatar.py

# 3. Start the stub sidecar
mkdir -p /tmp/mc-sidecar
src/connectors/zoom/publisher/sidecar/build/mc_zoom_sidecar --socket /tmp/mc-sidecar/sidecar.sock

# 4. Start the bridge itself
uvicorn src.main:app --reload

# 5. ngrok, pointed at the bridge
ngrok http 8000

# 6. Kick off a session
curl -X POST http://127.0.0.1:8000/sessions \
  -H "Content-Type: application/json" \
  -d '{"meeting_number": "<your test meeting number>", "passcode": "<passcode>"}'
```

---

## 7. Expected log lines, in order

```
service.startup                 # uvicorn up
sidecar.connected                # stub sidecar reachable
publisher.ready                  # sdk_version=stub-no-sdk — expected, not an error
rtms.attach_deferred             # no binding yet — expected, session is "joining"
session.created                  # POST /sessions → 202

# --- once the Access-tab Validate button succeeds (one-time, in the portal) ---
zoom.webhook.url_validated

# --- once a real meeting.rtms_started webhook actually arrives ---
session.rtms_binding_claimed  /  session.rtms_bound
rtms.attached_after_wait
# session state moves toward "active"
```

If you never see the last two blocks, ingest will sit at `joining` for 5 minutes,
then log `rtms.failed detail='no rtms_started webhook bound within 300s'` — that's
your own code correctly giving up, not a bug. Check §4 and §8.

---

## 8. Troubleshooting quick reference

| Symptom | Cause | Fix |
|---|---|---|
| `POST /sessions` → `409`, `no RTMS attachment bound yet` | Fixed in this repo — should not recur | If it does, confirm you're on the current code |
| `409`, `cannot connect to sidecar` | Sidecar not running, or `MC_SIDECAR__UDS_PATH` still the Linux default `/run/...` | Run the sidecar (§6.3), fix `.env`, **full restart** |
| `session.failed ... ingest=unknown ... for over 60.0s` | Old bug — supervisor's grace window didn't exempt "still waiting" | Fixed in this repo; if seen, you're on stale code |
| Webhook `401 signature mismatch` | `MC_ZOOM__WEBHOOK_SECRET_TOKEN` doesn't match the portal's current Secret Token | Recopy from Features → Access (§2.3), not Client Secret |
| ngrok shows zero `POST /webhooks/zoom/` ever | RTMS never triggered — see §4 | Try §4.3; if still nothing, it's a Zoom-side enablement gap |
| `Meeting SDK key/secret are not configured` | `MC_ZOOM__SDK_KEY`/`SDK_SECRET` blank | Fill with the same Client ID/Secret (§3) |
| Redirect lands on `/webhooks/zoom/?code=...` → `405` | OAuth redirect URL still points at the webhook | Fix in Basic Info (§2.1) to `/callback` |

---

## 9. If RTMS still never arrives after §4

File a request on the [Zoom Developer Forum](https://devforum.zoom.us/) (Realtime
Media Streams category) with your General App's App ID, describing: RTMS scopes
added, event subscription validated successfully, on-demand REST trigger attempted,
no `meeting.rtms_started` ever received. This exact pattern — everything configured
correctly, nothing arrives — is a known, recurring account/App-ID-level enablement
gap Zoom staff has to flip manually, not something fixable from the portal or this
codebase.

**Confirmed hit on this project, 2026-08-06**: the §4.3 REST trigger, called with a
correctly-scoped S2S token, the right `client_id`, and a genuinely live meeting,
returned:
```json
{"code":2310,"message":"Failed to perform RTMS app operation."}
```
This is [a well-documented, recurring error](https://devforum.zoom.us/t/rtms-start-api-returns-2310-although-rtms-scopes-and-events-are-configured/144213)
across the Zoom Developer Forum, hit by developers with otherwise-correct config —
every resolved thread ends in Zoom staff manually enabling RTMS for the reporter's
App ID. Nothing left to try locally at this point; the forum post above is the
next step, not another config or curl change.


---

# 10. Microsoft Teams

Nothing in §1–§8 applies here. Teams' prerequisites are Azure, not a Marketplace app,
and the honest headline is that **Teams cannot be exercised end-to-end without a Windows
host** — app-hosted media is Windows/.NET only ([doc 005 §1](docs/design/005-teams-connector-architecture.md)).

The good news mirrors how Zoom was de-risked: the bridge side is fully testable without
any of it (§10.5).

## 10.1 Azure AD app registration

In [portal.azure.com](https://portal.azure.com) → **Microsoft Entra ID** → **App registrations** → **New registration**:

1. Name it, single-tenant is fine, no redirect URI needed (this is a daemon, not a UI).
2. **Certificates & secrets** → **New client secret**. Copy the value immediately — it is
   shown once.
3. **API permissions** → **Add a permission** → **Microsoft Graph** → **Application
   permissions**:

| Permission | Why |
|---|---|
| `Calls.JoinGroupCall.All` | Join a scheduled meeting |
| `Calls.AccessMedia.All` | App-hosted media. **Without it the bot joins successfully and no media ever flows** — the most confusing failure in this whole setup, because nothing errors. |
| `Calls.JoinGroupCallAsGuest.All` | Only if joining meetings outside your tenant |

4. **Grant admin consent** — application permissions do nothing until an admin consents.
   This needs a Global Administrator; if you are not one, this is the step to hand off.

Note these three, they go in `.env` as `MC_TEAMS__TENANT_ID`, `MC_TEAMS__CLIENT_ID`,
`MC_TEAMS__CLIENT_SECRET`.

## 10.2 Bot registration

Register an **Azure Bot** with the **Microsoft Teams** channel enabled, and set its
**calling webhook** to:

```
https://<your-fqdn>:<media-public-port>/api/calls
```

That endpoint is served by the sidecar, not by this bridge — Graph's notifications are
input to the Calling SDK, which lives on the Windows side ([doc 005 §3.3](docs/design/005-teams-connector-architecture.md)).
Consequence: there is no Teams webhook to configure on the bridge, and no ngrok tunnel to
set up the way §2.3 needs one for Zoom.

## 10.3 Windows host

- Windows Server 2019+ (or Windows 10/11 to develop against), **x64**
- .NET Framework 4.7.2+
- A **publicly resolvable** DNS name — Microsoft's service connects *inbound*
- A **publicly trusted** certificate whose subject matches that name. A self-signed
  certificate fails at `MediaPlatform.Initialize` with a message that does not mention
  certificates, which is a long detour if you have not been warned.
- Inbound TCP open on the media public port and the notification port

Full build and run instructions: `src/connectors/teams/sidecar/dotnet/README.md`.

## 10.4 `.env` checklist

```bash
MC_TEAMS__TENANT_ID=<tenant guid>
MC_TEAMS__CLIENT_ID=<app registration guid>
MC_TEAMS__CLIENT_SECRET=<client secret>
MC_TEAMS__SIDECAR_HOST=teams-bot.example.com
MC_TEAMS__SIDECAR_CA_FILE=/etc/mc/teams-sidecar-ca.pem
```

The connector registers only when the first four are present. Leave them unset and the
service is Zoom-only — no Teams surface at all, and `{"platform": "teams"}` returns a
precise "no connector registered" rather than failing deep inside a join.

**Do not set `MC_TEAMS__VIDEO_FPS=25`.** Teams negotiates video against an enumerated
list of formats and 25 fps is not on it — even though 25 is this repo's shared default
for Zoom. The connector validates this at startup and refuses with the supported list, so
the failure is loud and local rather than happening on the Windows host mid-join. This is
also why Teams has its own geometry settings instead of reading `MC_MEDIA__*`.

## 10.5 Testing without any of the above

The whole Teams pipeline runs against an in-process fake sidecar that speaks the real wire
protocol:

```bash
poetry run pytest tests/unit/test_teams_link.py tests/unit/test_teams_session.py
poetry run pytest tests/unit/test_teams_sidecar_protocol.py   # the .NET contract
poetry run pytest -k teams                                    # everything Teams
```

That covers join, per-participant attribution, backpressure, full rejoin, budget
exhaustion, and the shared pipeline being reused — with no Windows host, no Azure tenant,
and no admin consent. The same strategy as proving RTMS against `FakeRtmsTransport` before
a Zoom account existed.

## 10.6 Starting a Teams session

```bash
# By the numeric Meeting ID from the invite — the same request shape as Zoom
curl -X POST localhost:8000/sessions -H 'content-type: application/json' \
  -d '{"platform": "teams", "meeting_number": "123 456 789 012", "passcode": "abc123"}'

# Or by join URL, when that is all you have
curl -X POST localhost:8000/sessions -H 'content-type: application/json' \
  -d '{"platform": "teams", "meeting_url": "https://teams.microsoft.com/l/meetup-join/19%3ameeting_...%40thread.v2/0?context=%7b%22Tid%22..."}'
```

Printed spacing in the meeting id is stripped for you. A join URL pasted into
`meeting_number` is also accepted — it is a natural mistake and everything needed is
present.

## 10.7 Expected log lines, in order

```
connectors.registered           platforms=['teams', 'zoom']
session.created                 platform=teams
teams_sidecar.connected         endpoint=teams-bot.example.com:8445 tls=True
teams_link.ready                call_id=... negotiated_audio=16000Hz/1ch negotiated_video=1280x720@30
teams_link.call_state           state=ESTABLISHED
teams_link.own_participant_known msi=...
echo_guard.own_participant_known user_id=...
```

The last two are the pair worth watching. Teams reports the bot's own identity from the
**roster, after** the call is established — where Zoom learns it during the join
handshake. Until they appear, echo suppression is running on the speaking gate alone,
which is by design but should not be the steady state.

## 10.8 Failures and what they mean

| Symptom | Cause |
|---|---|
| `no connector registered for platform 'teams'` | One of the four required `MC_TEAMS__*` values is missing, or Teams config failed validation — check startup logs for `connectors.teams_registration_failed`. |
| `Teams cannot send 1280x720@25` at startup | §10.4. Use 30 fps. |
| `teams.tenant_id` in a 409 on session create | Credentials absent; the connector refuses before dialling anything. |
| `GRAPH_403` / `missing Calls.AccessMedia.All` | §10.1 step 4 — admin consent not granted. Fatal by design, so it fails fast rather than retrying ten times. |
| Bot joins, but is silent and has no video | Almost always `Calls.AccessMedia.All` missing. The call succeeds; only media is refused. |
| `MEDIA_PLATFORM_INIT` | The certificate does not match `--service-fqdn`, its private key is unreadable by the service account, or the internal port is in use. |
| `AUDIO_FORMAT_MISMATCH` / `VIDEO_FORMAT_MISMATCH` | Sidecar negotiated something other than what was requested. Fatal deliberately — a silent mismatch produces pitch-shifted speech or garbled frames that read as avatar bugs. |
| `cannot verify sidecar certificate` | `MC_TEAMS__SIDECAR_CA_FILE` does not chain to the sidecar's IPC certificate. Fatal, not retried. |

---

# 11. Google Meet

Google Meet does **not** work like Zoom or Teams, and the setup reflects that.

There is no app registration, no client id, no secret, and no SDK entitlement — because
Google publishes no server-side way to send media into a Meet conference:

> "All conference media streams are \"receive-only\". Currently, the Meet Media API does
> not support sending of media from MediaApiClientInterface into a conference."
> — [Meet Media API C++ reference](https://developers.google.com/workspace/meet/media-api/reference/cpp/namespace/meet)

So the avatar joins as a **real signed-in Chromium**, driven by Playwright. The credential
is therefore a **browser profile on disk**, and provisioning it is the whole of the setup.
Full reasoning: `docs/design/007-google-meet-connector-architecture.md`.

## 11.1 Install the browser

Playwright is an optional extra, so a Zoom-only or Teams-only deployment pulls no browser.

```bash
poetry install --extras google-meet
poetry run playwright install chromium
```

Skipping the second command produces `PlaywrightUnavailableError` at session start, naming
the command — not an `ImportError` at boot.

## 11.2 Authenticate the profile — once, interactively

This is the only manual step, and it must not be automated. Google's sign-in can present a
second factor, a device-verification challenge, or an outright "this browser may not be
secure" refusal, and repeatedly scripting past those is what gets an automated account
restricted.

```bash
mkdir -p /var/lib/meeting-connectors/meet-profile

# Launch headed, sign in, then stop the process.
MC_GOOGLE_MEET__PROFILE_DIR=/var/lib/meeting-connectors/meet-profile \
MC_GOOGLE_MEET__HEADLESS=false \
poetry run uvicorn src.main:app

# Trigger one session so the browser opens, complete the Google sign-in in the window
# including any second factor, then Ctrl-C.
```

The profile is treated as a **template** from then on: each session runs on a throwaway copy
seeded from it, so sessions cannot corrupt each other's login and the template cannot be
corrupted at all. Re-authentication is only ever needed because Google expired the session.

Use an account that is a member of the organisation whose meetings the avatar joins.
A guest cannot enter a meeting restricted to the host's org, and the resulting denial is
terminal by design.

## 11.3 `.env` checklist

```env
# The only required value. Everything else has a working default.
MC_GOOGLE_MEET__PROFILE_DIR=/var/lib/meeting-connectors/meet-profile

# Optional
MC_GOOGLE_MEET__HEADLESS=true
MC_GOOGLE_MEET__DISPLAY_NAME=AI Avatar        # only used if Meet asks for a name,
                                              # which means the session was lost
MC_GOOGLE_MEET__VIDEO_WIDTH=1280
MC_GOOGLE_MEET__VIDEO_HEIGHT=720
MC_GOOGLE_MEET__VIDEO_FPS=25
MC_GOOGLE_MEET__PUBLISH_SAMPLE_RATE_HZ=48000   # 16000 | 24000 | 48000
MC_GOOGLE_MEET__LOBBY_TIMEOUT_S=300            # a human has to click Admit
MC_GOOGLE_MEET__REJOIN_MAX_ATTEMPTS=5
MC_GOOGLE_MEET__WATCHDOG_INTERVAL_S=5
MC_GOOGLE_MEET__ATTENDANCE_ENABLED=true        # remember who attended; see below
MC_GOOGLE_MEET__ATTENDANCE_PUSH_ENABLED=true   # tell the agent, so it can answer
MC_GOOGLE_MEET__ATTENDANCE_PUSH_INTERVAL_S=5
MC_GOOGLE_MEET__ATTENDANCE_PUSH_REQUIRE_NEGOTIATION=true   # false = skip the agent's
                                                           # handshake change; see below
MC_GOOGLE_MEET__CAPTIONS_ENABLED=true          # record who said what; see below
MC_GOOGLE_MEET__SPEAKER_TRACKING_ENABLED=true  # identify who is speaking; see below
MC_GOOGLE_MEET__SPEAKER_PUSH_ENABLED=true      # tell the agent, silently
MC_GOOGLE_MEET__SPEAKER_PUSH_INTERVAL_S=3
MC_GOOGLE_MEET__SPEAKER_PUSH_REQUIRE_NEGOTIATION=true
MC_GOOGLE_MEET__SPEAKER_HOLD_MS=1500           # how long somebody stays "the speaker"
                                               # after they stop — speech has gaps
MC_GOOGLE_MEET__SPEAKER_MERGE_GAP_MS=1200      # longer gap than this = a new turn

# Not recommended. Only bootstraps an empty profile, and only works on an account with
# no second factor — which is not a configuration for an account in customer meetings.
# MC_GOOGLE_MEET__GOOGLE_EMAIL=avatar@example.com
# MC_GOOGLE_MEET__GOOGLE_PASSWORD=...
```

Unlike Zoom and Teams there is **no passcode** — Meet admission is controlled by the host and
by Workspace policy, never by a secret the joiner supplies. Supplying one is logged as a
warning, because it usually means the request was written for another platform.

Notes on two defaults:

* **1080p is the ceiling.** Every frame crosses the page bridge as raw I420, so 1080p is
  already 3.1 MB per frame; anything larger is refused at startup, and Meet downscales it
  anyway.
* **48 kHz publish rate** is Web Audio's native rate on desktop Chromium, so the synthetic
  microphone needs no resampling stage. Only 16/24/48 kHz are accepted, because Web Audio
  would resample anything else *silently* rather than fail — a pitch artefact nobody can
  trace.

### Attendance — who is in the meeting, and who never came

`MC_GOOGLE_MEET__ATTENDANCE_ENABLED` (default `true`) keeps a per-session record built from the
roster the page already reports, so the agent can be asked who attended after the fact. On by
default because it costs nothing: no extra DOM scanning, no new page observer, and nothing other
participants can see — unlike `CHAT_ENABLED`, which has to open a panel.

Read it back per session:

```bash
curl localhost:8000/sessions/ses_abc123/participants
```

The response separates `present`, `departed` and `never_joined`, carries per-person join/leave
times and rejoin counts, and includes an `agent_context` string written for an LLM's context
window rather than for a parser.

**"Who was invited" needs the calendar, not the browser.** Meet only shows invitees who have not
joined inside the People panel, behind selectors that change without notice, so the invite list
is supplied as data instead:

```bash
curl -X POST localhost:8000/sessions/ses_abc123/invitees \
  -H 'content-type: application/json' \
  -d '{"invitees": ["Aarav Sharma", "priya@example.com"]}'
```

`calendar-orchestrator` does this automatically for meetings it schedules — it reads `attendees`
off the Google Calendar event and posts them after the join succeeds. The call is best-effort
there: a failure costs the invite list, never the meeting.

Without an invite list, `never_joined` is empty **and** `has_invite_list` is `false` — which the
`agent_context` renders as "unknown" rather than "nobody", because those are different answers.

#### Getting it into the agent — one required agent-side change

Serving the ledger over HTTP makes it *available*. It does not make the agent *hold* it, and the
difference is not academic: in a live run the bridge knew exactly who was present and the agent
still answered *"I don't have access to your meeting details or a list of participants."*

So the bridge now **pushes** the brief over the avatar socket
(`MC_GOOGLE_MEET__ATTENDANCE_PUSH_ENABLED`, default on) as a new frame kind:

```json
{"kind": "meeting_context", "topic": "attendance",
 "text": "Currently in the meeting (2): Aarav Sharma and Priya Menon. Invited but never joined (1): Rahul Verma.",
 "observed_at_us": 123456789}
```

The frame is deliberately *not* `kind: "chat"`. Everything on the chat channel is something the
avatar says out loud — that is how a raised hand becomes "of course, go ahead" — and an avatar
that announces "Aarav Sharma is in the meeting" every time somebody reconnects is worse than one
that says nothing.

**The agent has to handle the frame; there is no way around that.** The bridge cannot make an
LLM know something without sending it something the agent processes. If the log says

```
avatar.meeting_context_unsupported  negotiated=1.1  required=1.2
```

then the bridge is working and the agent has not been updated yet.

By default this is **two** agent edits, and forgetting the first silently disables the feature
while the second looks done:

1. reply `"1.2"` as `protocol_version` in the agent's server hello, and
2. handle `kind: "meeting_context"`.

**Set `MC_GOOGLE_MEET__ATTENDANCE_PUSH_REQUIRE_NEGOTIATION=false` to skip step 1.** The frame is
then sent regardless of the negotiated version, which is safe for any agent that ignores control
frames it does not recognise — leaving only the handler:

```python
# where the bridge's control frames are handled
if frame.get("kind") == "meeting_context":
    session.chat_ctx.add_message(role="system", content=frame["text"])   # replace the prior one
    return   # no session.generate_reply() — this is context, not a turn
```

The frame is a full replacement for the previous brief on the same `topic`, not a delta, so
keeping only the most recent one is correct. Leave the setting on if the agent instead *raises*
on an unknown kind, because then an undeliverable frame becomes an error on the avatar socket
rather than a warning in this log.

**Alternative: let the agent pull instead.** A tool-calling agent gets fresher data by hitting
the endpoint when the question is actually asked, at the cost of a round trip mid-answer. Set
`MC_GOOGLE_MEET__ATTENDANCE_PUSH_ENABLED=false` and register a tool:

```python
@function_tool
async def who_is_in_the_meeting() -> str:
    """Who is currently in the meeting, who has left, and who never joined."""
    r = await http.get(f"http://bridge:8000/sessions/{session_id}/participants")
    return r.json()["agent_context"]
```

Either path works; running both is redundant, not harmful.

#### A note on the names themselves

Meet's participant DOM is lossy, and two faults were fixed after the first live run. `innerText`
on a participant tile is the name *plus every control rendered over it*, which produced roster
entries like `frame_person Reframe visual_effects ... More options for jadumeetboot`; only the
first line is read now, and a label still containing icon-font tokens is rejected rather than
guessed at. Separately, the avatar was counting **itself** as an attendee, because `display_name`
is only what Meet is *asked* to call it — a signed-in profile renders the Google account's own
name instead. Self-detection now also uses Meet's "(you)" marker and the local part of
`MC_GOOGLE_MEET__GOOGLE_EMAIL`.

### Speakers — who is talking, and who said what

`MC_GOOGLE_MEET__SPEAKER_TRACKING_ENABLED` (default `true`) answers two questions attendance
cannot: **who is speaking right now**, and **who has spoken, in what order, and for how long**.

**Read this part before changing anything here.** The audio the avatar receives is a *mix* — the
page sums every remote track into one mono node before sampling it, which is what lets this
connector run without a resampler anywhere. Attribution does not change that and could not be
built from it. It is assembled from two observations taken *beside* the media path:

* **the level of each remote track**, measured on an `AnalyserNode` branched off the node that
  already feeds the mix. A branch, not a stage: the samples reaching the mix are identical and
  arrive at the same time. Sampled every 200 ms, touching no DOM — it reads 512 bytes and does
  arithmetic on them, which is why it can run faster than the DOM scans that force a layout.
* **who it can only be.** If exactly one other person is in the meeting — an interview, which is
  the case that matters most — whoever is speaking is that person. No markup is read, so nothing
  Google ships can break it. This is the path that actually names the speaker today.

Three weaker paths run alongside it and fill in the multi-person case: the participant tile a
stream is rendered on (matched by `MediaStream.id`, then by the receiver's SSRC against any
numeric attribute on a tile), the roster's `data-participant-id` → name mapping, and Meet's own
speaking indicator. **The first live run is why the ordering is that way round:** levels were
measured perfectly, three audio probes were active, and every turn came back "Someone" — because
Meet does not render remote *audio* on an element inside the participant tile. The tile holds the
video; the audio plays from elements Meet keeps elsewhere, so an audio stream's id never appears
on a tile and never can.

So the guarantees are: **no audio frame is retagged, re-timed, delayed, or re-mixed**, and every
existing signal — `MIXED_SOURCE`, the 20 ms frame cadence, the 16 kHz capture context, the echo
guard's strict mode — is exactly as it was. Turning this off changes ingest not at all.

Read it back per session:

```bash
curl localhost:8000/sessions/ses_abc123/speakers
```

The response carries `current_speaker`, everyone `speaking_now` (plural, because people talk over
each other), `talk_time_seconds` per person, the full `turns` list with start and end times, and
an `agent_context` string written for a context window rather than for a parser.

**Three behaviours worth knowing about, because each is a deliberate trade:**

* **A pause does not end a turn.** Speech has gaps at every clause boundary, so stretches
  separated by less than `SPEAKER_MERGE_GAP_MS` are one turn. Without it, one person talking for
  a minute is forty turns and every talk-time figure is a sum of fragments.
* **The current speaker survives a short silence** (`SPEAKER_HOLD_MS`). Asking who is speaking in
  the gap between two sentences would otherwise answer "nobody" — true of that instant, and the
  wrong answer to the question.
* **Somebody who started talking before Meet drew their tile is renamed, not double-counted.**
  The first sentence of a meeting is often heard before it can be attributed; the turn is
  back-filled when the name arrives, including retroactively from the roster.
* **A name arriving mid-remark claims the voice already being heard.** The energy path hears a
  voice the instant it starts and cannot name it; Meet's caption names that voice a second or two
  later. Both orders now collapse into one turn, so the agent is not told "Someone is speaking"
  for the first half of every remark and then a name for the second half. It fails closed on
  ambiguity — two anonymous voices, or somebody named already talking — because a confident wrong
  name is worse than "Someone". Turns named this way are flagged `inferred`.

**A muted participant is not the voice you are hearing, and that is what makes elimination work in
a room of two.** Elimination is the most reliable attribution route here — no markup, nothing a
Meet release can break — and it used to give up at two other participants, because naming one of
two is a guess. But Meet writes `", muted"` into the same `aria-label` the roster already reads, so
two others of whom one is muted is the same situation as one other: exactly one person it can be.
That case cost a live meeting four minutes of wrong answers — one identity typing in the chat with
its microphone off, another speaking, and captions naming the speaker only once they switched from
Urdu to English. Mute state is a **tri-state**: unread (`null`) keeps somebody in the field of
candidates, because an unreadable label must cost a name rather than invent one. And it narrows
*speech* only — somebody muted can type in the chat all day, so a typed line is still eliminated
against everyone present.

**Speech and chat are different channels, and the brief says so.** A participant who has only
typed has never taken the floor and never appears as a speaker. This mattered live: somebody spoke,
the page could not name the voice, and the avatar — asked *"what is my name?"* — answered with the
name of a **different** participant who had been typing. So an unattributed voice is now reported
as unattributed *with the inference ruled out* ("do not assume it is whoever spoke or typed most
recently"), a named one is marked as the person the avatar is being spoken to by, and the speaker
paragraph states outright that it counts speech only. The guard is in the frame **before the first
speaking edge**, not after it: somebody joining and talking immediately is the one moment the agent
is asked a question by a voice it has been told nothing about, and it is precisely when the only
name in the frame belongs to whoever was typing. An unattributed voice is also *narrowed* — "it is
one of these people: A, B" — because naming the field is real information and withholding it is
what left the model resolving the question from the chat history.

**If speech is detected but nobody is named, the log now says so even when captions are working.**
`speakerNothingSeen` reports `edges`, `attributed` and `attributedLive` — the last counting only
routes fast enough to name a voice *while it is still talking*. Captions do not qualify: they land
after Meet's transcription settles. Gating the report on `attributed` let four caption-derived
names switch it off for a whole meeting in which the `speaking` selectors matched nothing at all,
which is precisely the state it exists to explain. Those selectors remain **unverified against a
live meeting**; the report prints the media elements, tiles and participant nodes the page actually
has, so the next edit to them is a reading rather than another guess.

**It also names a barge-in.** `SPEECH_INTERRUPT_ENABLED` previously told the agent "Someone
raised their hand and wants to say something", because the mix carries no name. With speaker
tracking on, the same handover names the person — the lookup happens on the frame that already
triggered the interrupt, and a miss costs the name rather than the barge-in.

**The push is silent, and that is not incidental.** Who is speaking is delivered as
`kind="meeting_context"`, the same channel attendance uses — never as chat. A chat frame is a turn
the avatar *says out loud*, so pushing speaker changes down it would have the avatar narrate the
meeting: "Priya is speaking now", into the room, every time somebody took a breath. Sent on change
only, so a still meeting sends nothing.

**One brief, not two — and this one is a warning worth reading before adding a third.** An agent
keeps *one* slot for standing context. Attendance and the speaker therefore travel in the same
frame, under the unchanged `topic="attendance"`, from a single announcer. With two announcers the
speaker brief — pushed every few seconds against a roster that changes once a meeting — displaced
the attendance brief, and the avatar, asked who was in the meeting, answered *"Someone is present
in the meeting"*: it had been told who was speaking and its knowledge of the roster had been
evicted. That failure reads as attendance breaking, not as a push conflict, which is why the
one-pusher rule is enforced in the session wiring and asserted in the tests.

**A related fix, in the same area — and the third attempt at it, which is why it now rests on a
fact rather than on a name.** The avatar used to count *itself* as a participant whenever its
Google account's name was neither `DISPLAY_NAME` nor derived from `GOOGLE_EMAIL`: an account named
"Backend Services" matched nothing, so attendance reported two people in a call with one other
person, and attribution then credited that account with somebody else's speech.

Name comparison failed three ways in live runs — `DISPLAY_NAME` is only what Meet is *asked* to
call the avatar and a signed-in profile ignores it; Meet's "(you)" marker is not in the tile text
the roster scan reads; and the Google account button is absent from the layout headless Chromium
renders. So the page now identifies its own tile by **the track it published**: the self-view
renders a camera clone the bridge minted and handed to Meet, so the tile whose `<video>` carries
one of those track ids is the avatar's, whatever Meet calls it. `parse_roster` prefers that name
over the configured one.

That single fix corrects the attendance count, the chat and hand-raise self-filters, and both
elimination paths — "is exactly one other person here" is only answerable once the avatar is not
one of them.

As with attendance, an agent that prefers to pull can set
`MC_GOOGLE_MEET__SPEAKER_PUSH_ENABLED=false` and register a tool against the endpoint.

**If nobody is ever attributed, the log says why rather than staying silent.** After 30 seconds in
a call with nothing attributed, the page reports `meet_bridge.speaker_not_seen` with the counters
that separate the two failures: `probes: 0` means the analysers never attached and the energy path
is dead, while probes with `mapped: 0` means levels are being measured and nothing on the page
says whose they are. That distinction is the fix, and it is a reading rather than a guess — the
lesson the chat button and the hand indicator each cost a round of live testing to learn.

### Transcript — who said what

`MC_GOOGLE_MEET__CAPTIONS_ENABLED` (default `true`) is what makes the avatar able to answer
*"what did they ask you?"* and *"what did Dev say?"* — for speech. **Chat is recorded in the same
ledger** whenever `MC_GOOGLE_MEET__CHAT_ENABLED` is on, so a meeting held in the chat panel is a
conversation the avatar can recall rather than one it answered and forgot (see *Typed lines* at the
end of this section).

**Why that needs its own feature, when the avatar obviously hears the meeting.** Two things are
true at once and neither can become the other:

* the agent's transcription receives **one mixed stream**, so it knows the words and can never know
  whose they are;
* this connector measures **audio levels**, so it knows who is talking and never what was said.

Meet's caption panel is the one place in the meeting where a name and the words that person said
appear together, because Meet transcribes per participant. So the page turns captions on, reads
that panel, and Python keeps the ledger — see
[transcript.py](src/connectors/google_meet/meeting/transcript.py).

**Turning captions on is invisible to the meeting**, unlike opening the chat panel: Meet renders
captions locally for whoever enabled them, and nobody else's caption setting changes.

Read it back per session:

```bash
curl localhost:8000/sessions/ses_abc123/transcript
```

The recent lines also travel to the agent inside the meeting brief, so it can answer without a
round trip — and, because each line is attributed, it can address the person who just spoke by
name rather than answering into the room.

**Three behaviours, each a deliberate trade:**

* **A caption is not final when it appears.** Meet extends a line word by word while somebody
  talks, so a line is forwarded only once it has stopped changing for `CAPTION_SETTLE_MS` (1.2 s).
  Forwarding on sight would deliver one sentence as a dozen fragments.
* **The avatar's own captioned turns are kept and marked**, not dropped. A transcript missing half a
  conversation is not one; marking them stops the agent reading its own words back as a question.
* **A caption is also the best speaker signal there is** — Meet naming somebody in words beats any
  indicator this connector could match on — so it feeds the speaker tracker too.

**"You" in a caption is the avatar, not a participant.** Captions render in the avatar's own
browser, so Meet labels the *local* participant's lines "You" — and the local participant is the
bridge. Those lines are kept (a transcript missing the avatar's half is not a conversation) and
labelled `The avatar (you)`, which is what stops the agent reading its own greeting back as a
question it was asked. The same rule removes the avatar from "who is speaking": it does not take
the floor from itself.

**Two honest caveats.** Captions are Meet's own ASR: wording is approximate and names and technical
terms are often misheard, which is why the brief tells the agent so rather than presenting them as
verbatim quotes. And they follow the meeting's caption language — a Hindi turn is captioned in
Hindi only if Meet's caption language is set to it, otherwise it is transliterated or missed.

**Where the name in a caption comes from, in order.** The block selectors match the element holding
the *words*, and Meet renders the name in a sibling of it — a live run captured eleven lines and
attributed none of them. So the reader tries, in order: the participant photo's `alt` (a name by
definition, and an accessibility obligation rather than a build artefact), then the first rendered
line of the caption row one or two levels up, and finally **elimination** — in a two-person meeting
there is exactly one person the words can belong to, which needs no markup at all. Lines named that
way are flagged `inferred` in the API response.

**Typed lines are part of the conversation, and are marked as typed.** A live meeting was held
entirely in chat — five questions typed, every one answered aloud — and the avatar, asked what had
been discussed, described itself greeting somebody: each message had crossed the avatar socket once
and been recorded nowhere, leaving a ledger of its own captioned voice. Chat now lands in the same
ledger, rendered `Dev Choudhary (in chat): what is my name?`, and the brief tells the agent which
lines were spoken and which were typed — an avatar must not report hearing somebody who never
opened their microphone. Recorded **before** the `@mention` filter and including the avatar's own
messages, because whether to *answer* a message is policy and what was *said* is history: a
question two participants asked each other is not the avatar's to answer and is part of the
conversation it will be asked to summarise.

**If chat messages arrive with no sender, the log now says so.** Meet renders the sender's name in
the row *above* the element the message selectors match — and groups consecutive messages from one
person under a single heading — so a subtree search finds nothing, for every message, in every
meeting. The page climbs to the row, carries a group's name forward to the messages under it, and
falls back to **elimination** against the roster exactly as captions do. When every route misses,
`chatSenderMissing` prints the row's text and every attribute on it and its ancestors, so the
replacement selector is a reading rather than a guess; `chatMessagesSent` beside `chatAttributed`
in `__MC_BRIDGE_STATS__` is what makes "chat works but names nobody" visible as a number.

**If the transcript stays empty or unattributed, the log says which link failed.**
`meet_bridge.captions_not_seen` reports `captions_on` (`false` = the button was never found, with
the labels it did see), `captured` vs `attributed` (captions arriving but nameless), `block_shapes`
(one rendered line means the selector matched the words and the name is elsewhere), `alts`, and
`region_text` — the panel's actual contents. Every fix from here is a reading rather than a guess.

## 11.4 Container requirements

```dockerfile
# Chromium needs a real /dev/shm; the 64 MB Docker default crashes a video-carrying renderer.
# --shm-size=1g at run time, or the connector's own --disable-dev-shm-usage covers it.
```

If the runtime cannot grant `SYS_ADMIN` or a seccomp profile, add
`MC_GOOGLE_MEET__EXTRA_BROWSER_ARGS='["--no-sandbox","--disable-setuid-sandbox"]'`. Prefer the
capability where possible.

## 11.5 Testing without a Google account or a browser

Everything except Chromium itself is covered — the wire codec, the join flow with every
terminal outcome, admission refusal, the media round trip over a **real** loopback
WebSocket, rejoin, budget exhaustion, and the shared pipeline being reused:

```bash
poetry run pytest -k google_meet                 # 281 tests, no browser needed
poetry run pytest tests/integration/test_google_meet_end_to_end.py
```

The browser is faked (`tests/fakes/meet_page.py::FakeBrowserDriver`) but the *page* is not:
`FakePage` is a real WebSocket client speaking the real protocol, because a Python/JavaScript
codec mismatch is the one fault that would otherwise stay invisible until a live meeting.

## 11.6 Starting a Google Meet session

```bash
# By meeting code — the same request shape as Zoom and Teams
curl -X POST localhost:8000/sessions -H 'content-type: application/json' \
  -d '{"platform": "google_meet", "meeting_number": "abc-defg-hij"}'

# Or by link
curl -X POST localhost:8000/sessions -H 'content-type: application/json' \
  -d '{"platform": "google_meet", "meeting_url": "https://meet.google.com/abc-defg-hij"}'
```

An undashed code (`abcdefghij`) is regrouped for you, and a link pasted into
`meeting_number` is accepted. A Zoom meeting number is *rejected* with a named error rather
than turned into a URL that 404s.

## 11.7 Expected log lines, in order

```
connectors.registered            platforms=['google_meet', 'zoom']
session.created                  platform=google_meet
meet_bridge.listening            endpoint=ws://127.0.0.1:54321/bridge/<token>
meet_profile.cloned              files=4
meet_bridge.page_connected       generation=1
meet_auth.signed_in              account=a***@example.com
meet_join.navigating             target=abc-defg-hij
meet_join.in_lobby                                   # only if the host gates entry
meet_join.joined                 lobby_wait_s=12.4 join_button='//button[...Ask to join...]'
meet_controls.unmuted
meet_controls.camera_on
meet_bridge.joined               capture_hz=16000 publish_hz=48000 audio_published=True
```

**`meet_controls.unmuted` and `meet_controls.camera_on` are the pair worth watching.** Meet
does not publish tracks it was handed until told to, and nothing else in the system can
observe that it did not — a browser that joined muted looks healthy at every layer and is
silent in the meeting. `meet_bridge.page_connected` appearing twice is normal: the init
script runs again after navigating from the sign-in probe to the meeting.

## 11.8 Failures and what they mean

| Symptom | Cause |
|---|---|
| `no connector registered for platform 'google_meet'` | `MC_GOOGLE_MEET__PROFILE_DIR` is unset. Check startup for `connectors.google_meet_not_registered`. |
| `playwright is not installed` / `chromium is not installed` | §11.1. Fatal by design — no retry installs a browser. |
| `the Chromium profile is not signed in to Google` | §11.2, and the message repeats the fix. Fatal: retrying a sign-in Google has challenged makes things worse. |
| `Google is challenging this browser` | A second factor or device verification. A human must complete it once, headed. |
| `google meet refused admission: the host denied the request` | Terminal, deliberately. The connector will **not** retry — an account that repeatedly asks to enter a meeting it was refused from looks like abuse. |
| `the avatar was removed from the meeting` | Same, and same reason. |
| `nobody admitted the avatar within 300s in the lobby` | The host never clicked Admit. Recoverable; raise `LOBBY_TIMEOUT_S` if hosts are slow. |
| `no join button appeared … automation/selectors.py` | Meet's pre-join UI changed, or the code is invalid. Add the new selector to `MeetSelectors`; several candidates per concept coexist so old and new can both be listed. |
| `the Chromium page did not connect to the bridge` | The injected script did not run. Distinct from a join timeout: the browser started but never reached the bridge. |
| `the page built a 48000 Hz capture context but the avatar contract requires 16000` | A stale `js/bridge.js`. Fatal rather than resampled — the alternative is a chipmunk avatar that looks like an avatar-service bug. |
| `no conference audio for 45s with 2 other participant(s) present` | The watchdog. The browser is alive and the capture graph has lost its inputs or been suspended — the one failure every other check reports as healthy. |
| Avatar joins, is visible, but never speaks | Check `google_meet_publisher` in `GET /sessions/{id}`: `audio=0` with a healthy bridge means Meet is holding a track it was never told to publish. |
| Renderer keeps crashing (`rejoins` climbing) | `/dev/shm` too small, or memory pressure. §11.4. |
