# calendar-orchestrator

A separate deployable, in `calendar-orchestrator/` in this same repo, with its own venv, its
own FastAPI app, and its own port (8200). Its whole job: watch the bot's Google Calendar (and,
optionally, its inbox) and call `meeting-connectors`' `POST /sessions` at the right moment, so
nobody has to run a `curl` by hand for a scheduled meeting.

**It never touches a meeting itself** — only decides *when* to ask the bridge to join one. The
bridge stays exactly as documented elsewhere in this doc set, unaware that anything schedules
it.

## Architecture

```
Google Calendar  --poll every 120s-->  calendar-orchestrator  --schedule-->  APScheduler job
  (the bot's own                                                                  |
   calendar)                                                              T-minus 60s (default)
                                                                                   v
                                                        POST http://localhost:8000/sessions
                                                        {"platform":"google_meet","meeting_number":"abc-defg-hij"}
                                                        {"platform":"zoom_web","meeting_number":"838...","passcode":"..."}
                                                        {"platform":"teams_web","meeting_number":"...","passcode":"...",
                                                         "meeting_url":"https://teams.live.com/meet/..."}
                                                                                   v
                                                                    meeting-connectors bridge
```

It also joins **on demand**: when someone clicks "Add people" in a Meet call already running,
or "Invite → Email" in a running Zoom meeting, or pastes a Teams link into a mail, that message
lands in the bot's inbox and a second, faster poller (5s by default) puts it in the call within
seconds. This path — [Instant invites](#instant-invites-gmail-polling) — is entirely opt-in.

### Project layout

```
calendar-orchestrator/
├── app/
│   ├── main.py              FastAPI app: lifespan wiring + /health, /jobs, /sync
│   ├── config.py            Settings (pydantic-settings, ORCH_ env prefix)
│   ├── meeting_link.py      Recognises a Meet code, Zoom link, or Teams link in free text
│   ├── models.py            CalendarEvent
│   ├── calendar_service.py  Polls the Calendar API, extracts joinable links
│   ├── scheduler.py         Reconciles events → APScheduler jobs (add/reschedule/remove)
│   ├── bot_client.py        POSTs to the bridge, with retries
│   ├── state.py             Durable "already joined" dedup store
│   ├── auth.py              Builds Google credentials (service account or OAuth)
│   ├── gmail_poller.py      One instant-invite poll cycle: inbox → filter → dedupe → trigger
│   ├── gmail_service.py     Async wrapper over the Gmail API
│   ├── invite_parser.py     Is this email a live invite, and which meeting is it for?
│   ├── ics.py               Reads an invite.ics: unfolds it, says when the event is
│   └── gmail_state.py       Durable "already processed" message-id store
├── scripts/oauth_bootstrap.py   One-time interactive Google sign-in (OAuth credential mode only)
└── requirements.txt
```

## How it decides what to join (the calendar path)

Every `ORCH_SCHEDULING__POLL_INTERVAL_S` (default 120s), it lists events on the bot's own
calendar starting within `ORCH_SCHEDULING__LOOKAHEAD_HOURS`. Because it's reading the bot's
own calendar, any event returned is, by definition, one the bot was invited to. For each:

1. Skip it if it names no joinable meeting or is cancelled. Four places are searched, in
   descending order of authority: `conferenceData.entryPoints`, `hangoutLink` (Google fills
   these itself — facts), then `location`, then `description` (free text — can hold last
   week's link, copied into an agenda).
2. Extract the meeting: a Meet URL → its code on `google_meet`; a Zoom URL → its number on
   `zoom_web`; a Teams URL → its id (+ passcode if present) on `teams_web`.
3. Schedule (or reschedule) an APScheduler job keyed on the event id, firing
   `ORCH_SCHEDULING__JOIN_LEAD_TIME_S` (default 60s) before the event starts.
4. Any previously-scheduled job whose event no longer appears (cancelled, deleted, moved
   outside the window) is removed.

Reconciling the full job set from Calendar's current state on every poll means a time change
just replaces the job, and a cancellation just removes it — no separate update/delete path.

**Dedup / restart safety**: before firing, a join job checks a small on-disk JSON file keyed
on `event_id:start_time`. Already recorded ⇒ skip, so a restart between "trigger sent" and
"meeting start" can't double-join.

**Late starts**: if the join moment passed by less than `ORCH_SCHEDULING__LATE_JOIN_GRACE_S`
(default 120s) — the service was briefly down — it joins immediately. Older than that, it logs
the meeting as missed rather than joining minutes into an already-underway call.

## Platforms

All three platforms travel the same two routes (calendar, inbox) and the same filter steps —
nothing branches on platform except the link pattern itself, which is the whole reason adding
Zoom and Teams needed no second poller or state file.

| | Google Meet | Zoom | Microsoft Teams |
|---|---|---|---|
| bridge connector | `google_meet` | `zoom_web` | `teams_web` |
| meeting id example | `abc-defg-hij` | `83843212151` | `9339756425487` |
| passcode | n/a | sent when the invite spells it out | from the link's `?p=`, else the printed line |
| join URL sent | no | no | **yes** |
| calendar source | `hangoutLink`, `conferenceData` | `conferenceData`, `location`, `description` | `conferenceData`, `location`, `description` |
| invite sender | `meetings-noreply@google.com` | `no-reply@zoom.us`/`.com` | none — always a human's mailbox |

**Always `zoom_web`, never `zoom`; always `teams_web`, never `teams`.** An invited meeting is
by definition somebody else's — it cannot carry the RTMS entitlement `zoom` needs or the
tenant-consented Azure app + Windows host `teams` needs. `zoom_web`/`teams_web` join as
ordinary/anonymous participants and need neither. See
[connectors/zoom.md](connectors/zoom.md) and [connectors/teams.md](connectors/teams.md) for
what each pair actually requires.

### Teams links, and why this is the one platform that also gets `meeting_url`

Two shapes, both recognised:
```
https://teams.live.com/meet/9339756425487?p=71cQWhQJ5X8fxHSmVy      personal / free
https://teams.microsoft.com/meet/281442953617?p=aB3dE9              work/school, short form
https://teams.microsoft.com/l/meetup-join/19%3ameeting_…            work/school, long form
```
A bare Teams meeting id doesn't say which kind of Teams it belongs to (personal vs.
work/school) — the bridge resolves that itself by trying one join form and, if it makes no
progress, re-navigating to the other. Sending the link (rebuilt from scheme/host/id/passcode,
not copied verbatim out of the mail) turns that guess into a fact, which is why Teams — and
only Teams — gets `meeting_url` in the trigger payload.

### Two things worth knowing about Zoom passcodes

The `pwd=` query parameter in a Zoom link is **not** the passcode — it's an encrypted token
Zoom's own client exchanges for entry, and typing it into the passcode box gets rejected. The
real passcode is read from the invite's `Passcode: ...` line (or Calendar's `password` field).
An invite carrying only the token yields *no* passcode rather than a wrong one.

## Instant invites (Gmail polling)

Off by default (`ORCH_GMAIL__ENABLED=false`) — everything below is opt-in, and the calendar
path behaves exactly as before without it.

**What it covers**: someone clicks "Add people" mid-meeting. No calendar event is created, so
the calendar poller structurally cannot see it — the only artifact is an email to the bot's
inbox. `calendar-orchestrator` polls for that mail (default every 5s) the same way it polls
Calendar for events, and both jobs run on the **same** APScheduler instance — no second
process, no second scheduler.

Four invite shapes reach the inbox, and they differ in what can be trusted:

| shape | identified by | sender | acted on when |
|---|---|---|---|
| Zoom in-meeting invite | subject `Please join Zoom meeting in progress` | the host's own mailbox | always |
| Calendar invitation (`.ics` attached) | the `invite.ics` part | the organiser's own address | only if the meeting is **live now** |
| A Zoom or Teams invite pasted anywhere | the body's invite block (link + labelled `Meeting ID:`/`Passcode:` line) | anyone | always |
| System invite (Zoom/Meet) | subject marker | `no-reply@zoom.us` etc. | sender must be allow-listed |

For Teams specifically, the pasted-body route is the **only** route — Microsoft sends no
system invitation mail and has no fixed-subject "invite by email" — so switching that route
off turns Teams-by-mail off entirely, with no narrower fallback.

An `.ics` attachment, when present, overrules everything else — including refusing to join: an
invitation to next Tuesday's standup arrives *now*, and every other filter would pass it, so
only an event that is **live right now** is joined this way; anything scheduled is left to the
calendar poller. `METHOD:CANCEL`/`METHOD:REPLY` are ignored outright.

**What this open route accepts, stated plainly**: anyone who can email the bot a Zoom/Teams
invite block, or a live `.ics`, or that exact Zoom subject, can make it join a meeting of their
choosing — there's no cryptographic difference between the host's invite and a stranger's. The
mitigations are that the block signature is specific (not "any mention of the platform"), the
bot's address usually isn't published, and stale invites age out.

Everything else is gated by `ORCH_GMAIL__ALLOWED_SENDERS` (exact address, `@domain`, or `*`).

### Duplicate-join protection (three layers)

A meeting can be both on the calendar and instant-invited, so a join is de-duplicated by: the
processed-message-id file (survives restarts), a short in-process meeting-code TTL (covers the
gap before a session appears in the bridge's own list), and a live check against the bridge's
`GET /sessions` — because `POST /sessions` is **not** idempotent and will happily start a
second avatar in the same meeting.

## Setup

### 1. Enable the Google Calendar API

In [Google Cloud Console](https://console.cloud.google.com/), create/select a project and
enable the **Google Calendar API**.

### 2. Choose a credential mode

| | Service account + domain-wide delegation | OAuth2 user credential |
|---|---|---|
| Use when | Bot account is a Google Workspace mailbox and you're an admin | Bot account is a plain Google account, or domain-wide delegation isn't approvable |
| Ongoing upkeep | None — server-to-server, self-refreshing | Refreshes itself after one sign-in |

Scope needed either way: `https://www.googleapis.com/auth/calendar.readonly` — this service
only ever reads the calendar.

**Option A — service account** (recommended for a Workspace admin): create a service account
+ JSON key in Cloud Console, note its numeric Client ID, then in
`admin.google.com → Security → API controls → Domain-wide delegation`, authorize that Client
ID for the calendar-readonly scope. Then:
```bash
ORCH_GOOGLE__AUTH_MODE=service_account
ORCH_GOOGLE__SERVICE_ACCOUNT_FILE=credentials/service_account.json
ORCH_GOOGLE__DELEGATED_SUBJECT=bot@mydomain.com
```

**Option B — OAuth2** (plain Google account): create a **Desktop app** OAuth client in Cloud
Console, download its secret to `credentials/oauth_client_secret.json`, add the bot as a test
user if the consent screen is in Testing mode, then:
```bash
ORCH_GOOGLE__AUTH_MODE=oauth
ORCH_GOOGLE__OAUTH_CLIENT_SECRET_FILE=credentials/oauth_client_secret.json
ORCH_GOOGLE__OAUTH_TOKEN_FILE=credentials/token.json
```
and run the one-time interactive sign-in, **as the bot account**:
```bash
python scripts/oauth_bootstrap.py
```
This writes a refreshable `token.json`; re-run only if it's deleted or the OAuth client is
revoked.

### 3. Configure and run

```bash
cp .env.example .env
# adjust ORCH_BRIDGE__URL / ORCH_SCHEDULING__* if the defaults (bridge at localhost:8000,
# 60s lead time, 2-minute poll interval) don't fit

python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8200
```

**Port 8200, never 8100.** Port 8100 belongs to the avatar agent's gateway
(`MC_AVATAR__URL` defaults to `ws://localhost:8100/stream`). Both were once documented on
8100; the collision does not announce itself — uvicorn binds `127.0.0.1:8100` while the
gateway binds `0.0.0.0:8100`, macOS permits both, and the more specific bind wins every
`localhost` connection. The bridge then gets `HTTP 403` from *this* app's `/stream`, logs
`router.avatar_unreachable`, and publishes idle media — an avatar that joins and never speaks,
with no obvious error pointing at the port. See [RUNBOOK.md](RUNBOOK.md) for the full startup
order across all services.

On startup it runs an immediate sync, then every `ORCH_SCHEDULING__POLL_INTERVAL_S`
afterward.

### Endpoints

- `GET /health` — liveness + how many joins are currently scheduled
- `GET /jobs` — the scheduled joins and their fire times
- `POST /sync` — force an immediate re-sync instead of waiting for the next poll

### Enabling instant invites (optional, on top of the above)

1. Enable the **Gmail API** in the same Cloud project.
2. Re-issue the credential with the Gmail scope added:
   - OAuth mode: re-run `python scripts/oauth_bootstrap.py` (the service refuses to start on a
     token that predates the scope, rather than failing later with an opaque 403).
   - Service account mode: add `https://www.googleapis.com/auth/gmail.readonly` alongside the
     Calendar scope in the domain-wide delegation entry.
3. ```bash
   ORCH_GMAIL__ENABLED=true
   ORCH_GMAIL__POLL_INTERVAL_S=5
   ORCH_GMAIL__ALLOWED_SENDERS='["no-reply@zoom.us","meetings-noreply@google.com","@yourcompany.com"]'
   ```
   Nothing else to stand up — no Pub/Sub topic, no public HTTPS endpoint, no watch to renew.

Additional endpoints once enabled: `GET /gmail/status` (last poll time/error, joins
triggered), `POST /gmail/poll` (check now).

## Scaling notes

- **Multiple bot accounts / calendars**: run one instance per calendar (separate
  `ORCH_CALENDAR_ID`/`ORCH_GOOGLE__DELEGATED_SUBJECT`/`ORCH_STATE_FILE`) — the current
  single-calendar shape matches "one bot account," not a hard limit.
- **Push instead of poll**: Calendar supports webhook "watch" channels; not used here because
  they need a public HTTPS callback and their own renewal lifecycle — worth it once deployed
  somewhere with a stable public endpoint.
- **Persistent job store**: APScheduler's default job store is in-memory, so a restart loses
  not-yet-fired jobs until the next sync recreates them (within one `poll_interval_s`) — fine
  for any reasonable lead time; swap in `SQLAlchemyJobStore` if a much shorter lead time is
  ever needed.

For the complete, line-by-line rationale behind every setting and filter above — including the
exact regexes, the HTML-flattening repair pass, and the Gmail quota math — read
`calendar-orchestrator/README.md` directly; this page is a map onto it, not a replacement.
