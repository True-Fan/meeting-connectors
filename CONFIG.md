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
