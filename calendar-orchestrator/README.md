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

It also joins **on demand**: when someone clicks "Add people" in a meeting that is already
running, Google Meet emails the bot, and a second poller watching that inbox puts it in the
call within seconds. That path is opt-in — see [Instant invites](#instant-invites-gmail-polling).

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
│   ├── auth.py              Builds Google credentials (service account or OAuth)
│   │
│   │                        -- instant invites (Gmail polling), see below --
│   ├── gmail_poller.py      One poll cycle: inbox -> filter -> dedupe -> trigger
│   ├── gmail_service.py     Async wrapper over the Gmail API (list, get, mark read)
│   ├── invite_parser.py     Is this email a live invite, and what is the Meet code?
│   └── gmail_state.py       Durable "already processed" message-id store
├── scripts/
│   └── oauth_bootstrap.py   One-time interactive sign-in for OAuth mode
├── tests/
├── requirements.txt
├── requirements-dev.txt
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
uvicorn app.main:app --reload --port 8200
```

**Not 8100.** That port belongs to the avatar agent — `MC_AVATAR__URL` defaults to
`ws://localhost:8100/stream`. Both were documented on 8100, and the collision does not
announce itself: uvicorn binds `127.0.0.1:8100` while the avatar binds `0.0.0.0:8100`, macOS
permits both, and the more specific bind wins every `localhost` connection. The bridge then
gets `HTTP 403` from this app's `/stream`, logs `router.avatar_unreachable`, and publishes
idle media — an avatar that joins the meeting and never speaks.

On startup it runs an immediate sync, then every `ORCH_SCHEDULING__POLL_INTERVAL_S`
afterward. Useful endpoints while it's running:

- `GET /health` — liveness + how many joins are currently scheduled
- `GET /jobs` — the scheduled joins and their fire times
- `POST /sync` — force an immediate re-sync instead of waiting for the next poll

## Instant invites (Gmail polling)

Off by default (`ORCH_GMAIL__ENABLED=false`). Everything below is opt-in and the calendar
path behaves exactly as before without it.

### What it covers

Someone clicks **Add people** during a meeting that is *already running*. No calendar event
is created, so there is nothing for the calendar poller to find — that path structurally
cannot see this. The only artifact is an email to the bot's inbox from
`meetings-noreply@google.com`, so this polls for that mail the same way the calendar path
polls for events.

```
"Add people" in a live meeting
        v
Gmail inbox (meetings-noreply@google.com)
        v  poll every ORCH_GMAIL__POLL_INTERVAL_S (default 5s)
calendar-orchestrator  --filter-->  sender + subject + Meet code
        v                           (skip if already processed)
POST http://localhost:8000/sessions
{"platform":"google_meet","meeting_number":"abc-defg-hij"}
```

### How the scheduler runs it alongside the calendar job

Both are jobs on the **same** `AsyncIOScheduler` created in `main.py`'s lifespan — no
second scheduler, no separate process, no thread. `_setup_gmail_poller` adds one more
`IntervalTrigger` job next to `calendar-sync`:

```python
ap_scheduler.add_job(
    _run_gmail_poll,
    trigger=IntervalTrigger(seconds=settings.gmail.poll_interval_s),  # 5s
    id="gmail-poll",
    kwargs={"poller": poller},
    max_instances=1,
    coalesce=True,
    misfire_grace_time=int(max(settings.gmail.poll_interval_s, 1)),
)
```

The calendar job's registration is untouched. Three settings are what make a 5-second job
safe to run next to a 2-minute one:

| Setting | Why it matters at 5s |
|---|---|
| `max_instances=1` | A cycle that outruns its interval (slow bridge, retrying join) must not have the next one start behind it and act on the same message twice. `GmailPoller` holds its own `asyncio.Lock` as well, but this stops the queue forming at all. |
| `coalesce=True` | If several runs were missed — laptop asleep, event loop briefly busy — run **once** on resume instead of firing a burst of catch-up polls that all see the same inbox. |
| `misfire_grace_time` | A poll more than one interval late is pointless; the next tick is already due and will see the same messages. |

Both jobs are cooperative coroutines on one event loop, and every Gmail call is dispatched
through `asyncio.to_thread` (`gmail_service.py`) because `googleapiclient` is blocking. A
poll therefore never delays the calendar sync, and neither can stall the other.

`/health` and `/jobs` filter both out of their counts — they are housekeeping, not
scheduled meeting joins.

### Setup

**1. Enable the Gmail API** in the same Google Cloud project.

**2. Add the Gmail scope.** `required_scopes()` follows the enabled features, so this is a
consequence of turning the feature on — but the credential has to be re-issued either way:

- *OAuth mode*: re-run `python scripts/oauth_bootstrap.py`. The service refuses to start on
  a token that predates the scope rather than failing later with an opaque 403 from a
  background job.
- *Service account mode*: add `https://www.googleapis.com/auth/gmail.readonly` to the
  domain-wide delegation entry in the Admin console, alongside the Calendar scope.

**3. Configure and restart:**

```bash
ORCH_GMAIL__ENABLED=true
ORCH_GMAIL__POLL_INTERVAL_S=5
```

There is nothing else to stand up — no Pub/Sub topic, no public HTTPS endpoint, no
subscription or watch to renew.

### Endpoints

- `GET /gmail/status` — last poll time, last error, joins triggered, dedup window size
- `POST /gmail/poll` — check the inbox now instead of waiting for the next tick

### Cost of a 5-second poll

`messages.list` costs 5 quota units, so a 5s cadence is ~1 unit/second against Gmail's
250 units/second per-user ceiling — about **0.4%** of the rate limit, and roughly 0.01% of
the daily quota. The cadence itself is not the expensive part; fetching a message body is
another 5 units, which is why the query is narrowed to the invite sender rather than pulling
a broad result set and filtering in Python.

### Things worth knowing before changing this code

- **`messages.list`, not `history.list`.** Both can answer "what is new". `history.list` is
  incremental and needs a durable `historyId` watermark that must be seeded, advanced only
  on success, and resynced when it ages out of the ~1 week Gmail retains — machinery that
  earns its place when reacting to a push notification. A stateless query for "unread invite
  mail from the last day" cannot desynchronise, and the dedup file does the rest.
- **The poll returns the same message every cycle.** Nothing marks it read by default, so
  one invite is twelve identical answers a minute. `ORCH_GMAIL__STATE_FILE` is the *only*
  thing preventing twelve joins; it is durable so a restart cannot replay one either.
- **`ORCH_GMAIL__MAX_INVITE_AGE_S` is load-bearing on first run.** Without it, switching the
  feature on against a mailbox holding old unread Meet invites fires every one of them at
  the bridge, joining meetings that ended days ago.
- **The sender check is the security boundary.** It is in the Gmail query *and* re-checked
  in `invite_parser`, because the query is a performance filter and the parser is the
  security one. `From:` is parsed to a bare address, so a display name reading
  `meetings-noreply@google.com` — which anyone can set — gains nothing.
- **The Meet code regex is deliberately stricter than `meet\.google\.com/([a-z-]+)`.** The
  loose version matches the support and landing links in Google's own email footer, and each
  one would be handed to the bridge as a meeting number.
- **Duplicate joins are guarded three ways**, because a meeting can be both on the calendar
  and instant-invited: the processed-id file (survives restarts), a short in-process
  meeting-code TTL (covers the gap before a session is listed), and a live check against the
  bridge's `GET /sessions`. The bridge's `POST /sessions` is not idempotent and will happily
  start a second avatar in the same meeting.
- **A failed join is retried, but only `ORCH_GMAIL__MAX_ATTEMPTS` times.** Without a cap, a
  bridge outage would retry the same invite every 5 seconds until it aged out.

### Tests

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest
```

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
