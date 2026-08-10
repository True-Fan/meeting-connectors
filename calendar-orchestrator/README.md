# calendar-orchestrator

Watches the bot's Google Calendar and triggers `meeting-connectors`' `POST /sessions`
1 minute before any meeting that has a Google Meet link on it — no manual cURL needed.

```
Google Calendar  --poll-->  calendar-orchestrator  --schedule-->  APScheduler job
                                                                        |
                                                                 (T‑minus 60s)
                                                                        v
                                                     POST http://localhost:8000/sessions
                                                     {"platform":"google_meet","meeting_number":"..."}
                                                                        v
                                                          meeting-connectors bridge (bot joins)
```

It is a standalone service on purpose: it never touches a meeting itself, only decides
*when* to ask the existing bridge to join one. The bridge stays exactly as it is today.

## Project layout

```
calendar-orchestrator/
├── app/
│   ├── main.py              FastAPI app: lifespan wiring + /health, /jobs, /sync
│   ├── config.py            Settings (pydantic-settings, ORCH_ env prefix)
│   ├── models.py            CalendarEvent
│   ├── calendar_service.py  Polls Calendar API, extracts Meet links
│   ├── scheduler.py         Reconciles events -> APScheduler jobs (add/reschedule/remove)
│   ├── bot_client.py        POSTs to the bridge, with retries
│   ├── state.py             Durable "already joined" dedup store
│   └── auth.py              Builds Google credentials (service account or OAuth)
├── scripts/
│   └── oauth_bootstrap.py   One-time interactive sign-in for OAuth mode
├── requirements.txt
└── .env.example
```

## How it decides what to join

Every `ORCH_SCHEDULING__POLL_INTERVAL_S` seconds (default 120s), it lists events on the
bot's calendar (`ORCH_CALENDAR_ID`, default `primary`) starting within the next
`ORCH_SCHEDULING__LOOKAHEAD_HOURS` hours. Because it's reading the bot's own calendar, any
event returned is, by definition, one the bot was invited to. For each one:

1. Skip it if it has no Google Meet link (checks `conferenceData.entryPoints`, falls back
   to `hangoutLink`) or if it's cancelled.
2. Extract the meeting code from the Meet URL — `https://meet.google.com/veg-fkxv-rhg` ->
   `veg-fkxv-rhg`.
3. Schedule (or reschedule) an APScheduler job keyed on the event id, to fire
   `ORCH_SCHEDULING__JOIN_LEAD_TIME_S` seconds (default 60) before the event's start time.
4. Any previously-scheduled job whose event no longer appears in the fresh list (cancelled,
   deleted, or moved outside the lookahead window) is removed.

Because the reconciliation re-derives the full job set from Calendar's current state on
every poll, a time change just replaces the job (same event id), and a cancellation just
removes it — no separate update/delete handling needed.

**Dedup / restart safety.** Before firing, the join job checks a small on-disk JSON file
(`ORCH_STATE_FILE`) keyed on `event_id:start_time`. If that key is already recorded, it
skips — so a service restart between "trigger sent" and "meeting start" can't cause a
double join.

**Late starts.** If the join moment already passed by less than
`ORCH_SCHEDULING__LATE_JOIN_GRACE_S` (default 120s) — e.g. the service was briefly down —
it joins immediately instead of skipping. Older than that, it logs the meeting as missed
rather than joining minutes into an already-underway call.

## Setup

### 1. Enable the Calendar API

In [Google Cloud Console](https://console.cloud.google.com/), create or select a project,
then enable the **Google Calendar API** for it.

### 2. Choose a credential mode

| | Service account + domain-wide delegation | OAuth2 user credential |
|---|---|---|
| Use when | Bot account is a **Google Workspace** mailbox and you're an admin | Bot account is a plain Google account, or you can't get domain-wide delegation approved |
| Ongoing maintenance | None — server-to-server, tokens refresh themselves | Refreshes itself after one initial sign-in |
| Setup effort | One-time admin console step | One-time browser sign-in |

Scope needed either way: **`https://www.googleapis.com/auth/calendar.readonly`** — this
service only ever reads the calendar, never writes to it.

#### Option A — Service account + domain-wide delegation (recommended)

1. In Cloud Console → **IAM & Admin → Service Accounts**, create a service account. Create
   a JSON key for it and download it to `credentials/service_account.json`.
2. Note the service account's **Client ID** (numeric, on the service account's detail page).
3. As a Workspace super admin, go to **admin.google.com → Security → Access and data
   control → API controls → Domain-wide delegation**, and add a new API client:
   - Client ID: the numeric id from step 2
   - Scopes: `https://www.googleapis.com/auth/calendar.readonly`
4. Set in `.env`:
   ```
   ORCH_GOOGLE__AUTH_MODE=service_account
   ORCH_GOOGLE__SERVICE_ACCOUNT_FILE=credentials/service_account.json
   ORCH_GOOGLE__DELEGATED_SUBJECT=bot@mydomain.com
   ```
   `DELEGATED_SUBJECT` is what makes this work — it's the service account impersonating
   the bot's mailbox, which is why domain-wide delegation must explicitly permit it.

#### Option B — OAuth2 user credentials

1. In Cloud Console → **APIs & Services → Credentials**, create an **OAuth client ID** of
   type **Desktop app**. Download the client secret JSON to
   `credentials/oauth_client_secret.json`.
2. If your OAuth consent screen is in "Testing" mode, add `bot@mydomain.com` as a test user
   (Cloud Console → OAuth consent screen → Test users) — otherwise sign-in will be blocked.
3. Set in `.env`:
   ```
   ORCH_GOOGLE__AUTH_MODE=oauth
   ORCH_GOOGLE__OAUTH_CLIENT_SECRET_FILE=credentials/oauth_client_secret.json
   ORCH_GOOGLE__OAUTH_TOKEN_FILE=credentials/token.json
   ```
4. Run the one-time interactive consent, signing in **as the bot account**:
   ```
   python scripts/oauth_bootstrap.py
   ```
   This writes a refreshable `token.json`; the service refreshes it on its own from then
   on. Re-run only if `token.json` is deleted or the OAuth client is revoked.

### 3. Configure the bridge target and scheduling

Copy `.env.example` to `.env` and adjust `ORCH_BRIDGE__URL` /
`ORCH_SCHEDULING__*` if the defaults (bridge at `localhost:8000`, 60s lead time, 2-minute
poll interval) don't fit.

### 4. Run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8100
```

On startup it runs an immediate sync, then every `ORCH_SCHEDULING__POLL_INTERVAL_S`
afterward. Useful endpoints while it's running:

- `GET /health` — liveness + how many joins are currently scheduled
- `GET /jobs` — the scheduled joins and their fire times
- `POST /sync` — force an immediate re-sync instead of waiting for the next poll

## Notes on scaling this up

- **Multiple bot accounts / calendars**: run one instance per calendar (different
  `ORCH_CALENDAR_ID` / `ORCH_GOOGLE__DELEGATED_SUBJECT` and a separate `ORCH_STATE_FILE`),
  or extend `Settings` to a list and loop — the current single-calendar shape was chosen to
  match the one bot account in the request, not as a hard limit.
- **Push instead of poll**: Google Calendar supports webhook "watch" channels that notify
  on change instead of being polled. Not used here because they need a publicly reachable
  HTTPS callback and their own renewal lifecycle — worth adding once this is deployed
  somewhere with a stable public endpoint, but unnecessary complexity for a first version
  at a 1–2 minute poll interval.
- **Persistent job store**: jobs live in APScheduler's default in-memory job store, so a
  restart loses scheduled (but not-yet-fired) jobs until the next sync recreates them —
  which happens within one `poll_interval_s` of startup, well inside the `join_lead_time_s`
  margin for any reasonably-set lead time. If a much shorter lead time is ever needed,
  swap in APScheduler's `SQLAlchemyJobStore` so jobs survive a restart directly.
