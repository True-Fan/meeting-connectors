# calendar-orchestrator

Watches the bot's Google Calendar and triggers `meeting-connectors`' `POST /sessions`
1 minute before any meeting that has a **Google Meet, Zoom or Microsoft Teams** link on it —
no manual cURL needed.

```
Google Calendar  --poll-->  calendar-orchestrator  --schedule-->  APScheduler job
                                                                        |
                                                                 (T‑minus 60s)
                                                                        v
                                                     POST http://localhost:8000/sessions
                                                     {"platform":"google_meet","meeting_number":"abc-defg-hij"}
                                                     {"platform":"zoom_web","meeting_number":"838...","passcode":"..."}
                                                     {"platform":"teams_web","meeting_number":"9339756425487",
                                                      "passcode":"...","meeting_url":"https://teams.live.com/meet/..."}
                                                                        v
                                                          meeting-connectors bridge (bot joins)
```

It also joins **on demand**: when someone clicks "Add people" in a running Google Meet, or
**Invite → Email** in a running Zoom meeting, or pastes a Teams link into a mail, that message
lands in the bot's inbox and a second poller puts it in the call within seconds. See
[Direct invites](#direct-invites-four-shapes-four-ways-in). The whole inbox
path is opt-in — see [Instant invites](#instant-invites-gmail-polling).

**Which platform a meeting is on is decided per meeting, not per deployment.** A Meet code
resolves to the bridge's `google_meet` connector, a Zoom link to `zoom_web` and a Teams link
to `teams_web`; the same calendar and the same inbox can carry all three. See
[Platforms](#platforms).

It is a standalone service on purpose: it never touches a meeting itself, only decides
*when* to ask the existing bridge to join one. The bridge stays exactly as it is today.

## Project layout

```
calendar-orchestrator/
├── app/
│   ├── main.py              FastAPI app: lifespan wiring + /health, /jobs, /sync
│   ├── config.py            Settings (pydantic-settings, ORCH_ env prefix)
│   ├── meeting_link.py      Recognises a Meet code, Zoom link or Teams link in free text
│   ├── models.py            CalendarEvent
│   ├── calendar_service.py  Polls Calendar API, extracts joinable links
│   ├── scheduler.py         Reconciles events -> APScheduler jobs (add/reschedule/remove)
│   ├── bot_client.py        POSTs to the bridge, with retries
│   ├── state.py             Durable "already joined" dedup store
│   ├── auth.py              Builds Google credentials (service account or OAuth)
│   │
│   │                        -- instant invites (Gmail polling), see below --
│   ├── gmail_poller.py      One poll cycle: inbox -> filter -> dedupe -> trigger
│   ├── gmail_service.py     Async wrapper over the Gmail API (list, get, mark read)
│   ├── invite_parser.py     Is this email a live invite, and which meeting is it for?
│   ├── ics.py               Reads invite.ics: unfolds it, and says when the event is
│   └── gmail_state.py       Durable "already processed" message-id store
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

1. Skip it if it names no joinable meeting, or if it's cancelled. Four places are searched,
   **in descending order of authority**: `conferenceData.entryPoints`, `hangoutLink`,
   `location`, then `description`. Google fills the first two itself, so they are facts; the
   last two are free text that can contain last week's link in a copied agenda.
2. Extract the meeting from whichever link was found — `https://meet.google.com/veg-fkxv-rhg`
   -> `veg-fkxv-rhg` on `google_meet`, `https://us05web.zoom.us/j/83843212151` ->
   `83843212151` on `zoom_web`, `https://teams.live.com/meet/9339756425487?p=71cQWhQJ5X8fxHSmVy`
   -> `9339756425487` on `teams_web`, with the passcode if the event carries one.
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

## Platforms

All three platforms travel the same two routes — calendar and inbox — and the same three
filter steps. Nothing branches on platform except the link pattern itself
(`app/meeting_link.py`), which is why neither Zoom nor Teams needed a second poller, a second
state file or a second code path.

| | Google Meet | Zoom | Microsoft Teams |
|---|---|---|---|
| bridge connector | `google_meet` | `zoom_web` | `teams_web` |
| meeting id | `abc-defg-hij` | `83843212151` | `9339756425487` |
| passcode | n/a | sent when the invite spells it out | from the link's `?p=`, else the printed line |
| join URL sent | no | no | **yes** — see below |
| calendar source | `hangoutLink`, `conferenceData` | `conferenceData`, `location`, `description` | `conferenceData`, `location`, `description` |
| invite sender | `meetings-noreply@google.com` | `no-reply@zoom.us`, `no-reply@zoom.com` | **none** — always a human's mailbox |

**`zoom_web`, not `zoom`.** The bridge has two Zoom connectors: `zoom` uses the Meeting SDK
and needs the meeting to be hosted on an account with RTMS enabled *for this app*, and
`zoom_web` joins as an ordinary browser participant and needs nothing from the host. A
meeting the bot was invited to is by definition somebody else's, so that entitlement is
exactly what is not available — every invite therefore resolves to `zoom_web`.

**`teams_web`, not `teams`, for a stronger version of the same argument.** The bridge's
`teams` connector needs an Azure AD app with admin-consented `Calls.AccessMedia.All`, a tenant
willing to grant it, *and* a Windows host running the .NET media SDK. An invited meeting
belongs to somebody else's tenant — often a personal ("Teams for Life") account with no tenant
at all — so none of the three is available. `teams_web` drives Chromium and joins as an
anonymous guest.

### Teams links, and why this one also sends `meeting_url`

Two shapes, both recognised:

```
https://teams.live.com/meet/9339756425487?p=71cQWhQJ5X8fxHSmVy      personal / free
https://teams.microsoft.com/meet/281442953617?p=aB3dE9             work/school, short form
https://teams.microsoft.com/l/meetup-join/19%3ameeting_…%40thread.v2/0?context=…   work/school
```

For the short forms, `9339756425487` is the meeting id and `71cQWhQJ5X8fxHSmVy` is the
passcode. **Unlike Zoom's `pwd=`, Teams' `?p=` really is the typed passcode** — the same string
the invitation prints on its `Passcode:` line — so it is read straight out of the link. The
URL handed to the bridge is *rebuilt* from the scheme, host, id and passcode rather than copied
out of the mail, so entities and tracking parameters picked up from an HTML body can't reach
the browser that has to navigate it.

A `meetup-join` link carries a thread id rather than a number, so the meeting id comes from the
invitation's printed `Meeting ID:` line; with no printed id, the URL itself is sent as
`meeting_number`, which the bridge accepts for Teams.

**Teams is the one platform that gets `meeting_url` in the payload**, and the asymmetry is
deliberate: Meet and Zoom join by number and the bridge documents the field as ignored for
them. A bare Teams meeting id does not say whether it belongs to a personal or a work/school
account, and the bridge resolves that by trying one join form, waiting four polls for it to
make no progress, then re-navigating to the other. Sending the link turns that guess into a
fact. A Meet or Zoom join posts byte-for-byte the body it always did.

> **One collision worth knowing about.** Teams and Zoom print their meeting ids in the
> identical `Meeting ID: 281 442 953 617` form, and Zoom's last-resort pattern reads a bare id
> out of prose. So Teams is matched *before* Zoom, and Zoom's prose fallback is skipped
> entirely on text that says "Microsoft Teams". Without that, a Teams invitation whose link a
> mail client stripped would be dialled on `zoom_web` as a meeting Zoom has never heard of.
> A Teams meeting always requires a **link**; there is no prose-only route in.

### Two things to know about Zoom passcodes

**The `pwd=` in a Zoom link is not the passcode.** It is an encrypted token Zoom's own client
exchanges for entry; the bridge types into the passcode box instead, which rejects it. So the
passcode is read from the invite's `Passcode: 139601` line (or Google Calendar's `password`
field, or the one-tap dial-in string), and an invite carrying only the token yields **no**
passcode rather than a wrong one — the join then relies on the meeting having no passcode or
a waiting room the host admits from.

If that becomes a problem, the fix is on the bridge side: `ZoomWebJoiner` builds
`https://app.zoom.us/wc/{id}/join` and could carry the token as `?pwd=`. That is a change to
the Zoom connector, deliberately not made here.

### Direct invites: four shapes, four ways in

A meeting can reach the bot's inbox in four shapes, and they differ in what can be trusted
about them. All four still require the body to name a real meeting.

| shape | identified by | sender | acted on when |
|---|---|---|---|
| Zoom **in-meeting** invite | subject `Please join Zoom meeting in progress` | the host's own mailbox | always |
| **Calendar invitation** | the `invite.ics` part | the organiser's own address | only if the meeting is **live now** |
| Zoom or Teams invite **pasted** anywhere | the body's invite block | anyone | always |
| Zoom/Meet **system** invite | subject marker | `no-reply@zoom.us` etc. | sender must be allow-listed |

**For Teams the body route is not a third option — it is the only one.** Microsoft sends no
meeting invitation from a system address and has no in-meeting "invite by email" with a fixed
subject, so a Teams meeting arrives either as an Outlook calendar invitation (whose subject is
the event's own title) or as a link somebody pasted. Neither the sender nor the subject carries
any signal in either case.

#### Zoom in-meeting invite

```
From:    Any Host <whoever@example.com>         <-- the HOST's mailbox, not Zoom's
Subject: Please join Zoom meeting in progress   <-- this is the handle
         https://us05web.zoom.us/j/85666054587?pwd=...
         Meeting ID: 856 6605 4587
         Passcode: 2A4veB
```

Zoom composes this from **your own mailbox**, so the sender is unknowable in advance and the
subject is the only handle. `ORCH_GMAIL__ANY_SENDER_SUBJECT_MARKERS` lists subjects acted on
whoever sent them; it ships containing exactly that string, so it works with no setup.

#### Calendar invitation / a Zoom or Teams invite pasted anywhere

```
From:    Any Organiser <organiser@example.com>   <-- the ORGANISER, or anyone
Subject: test zoom                               <-- the EVENT's title. Arbitrary.

         Join Zoom Meeting
         https://us05web.zoom.us/j/85273228350?pwd=...
         Meeting ID: 852 7322 8350               <-- this is the handle
         Passcode: f4eVwN
```

Neither field carries a signal: the subject is whatever the organiser named the event, in any
language, and the sender is their own mailbox. So **the body is the handle** — a Zoom join
link *plus* a labelled `Meeting ID:` or `Passcode:` line. That is the block Zoom generates,
and it survives being pasted into a calendar event, forwarded, or reformatted.

Deliberately stricter than "there is a Zoom link somewhere". A colleague writing *"we used to
meet at zoom.us/j/123456789"* has a link and no invitation, and must not move the bot.

A Teams invitation reads the same way:

```
From:    Priya Sharma <priya@example.org>        <-- the ORGANISER, or anyone
Subject: Weekly sync                             <-- the EVENT's title. Arbitrary.

         Microsoft Teams
         Join the meeting now
         https://teams.live.com/meet/9339756425487?p=71cQWhQJ5X8fxHSmVy
         Meeting ID: 933 975 642 5487            <-- this is the handle
         Passcode: 71cQWhQJ5X8fxHSmVy
```

with one addition the Zoom form has no equivalent for: a short link carrying `?p=` counts as an
invitation **on its own**, without a labelled block underneath it. That string only comes from
the *Copy link* button and it is the whole of what a join needs. A passcodeless link
(`teams.live.com/meet/9339756425487` with nothing else) is a mention, not an invitation, and
does not move the bot.

When the message **does** carry an `invite.ics`, that decides instead — including deciding to
refuse:

> **Only invitations for a meeting already running are joined.** An invitation to next
> Tuesday's standup arrives *now*, and every other filter would pass it — fresh mail, real
> link, genuine organiser. Joining on receipt would put the bot in a meeting six days early.
> So an ics overrules the body signature, and anything scheduled is left to the calendar
> poller, which joins it at `join_lead_time_s` before it starts.

`METHOD:CANCEL` and `METHOD:REPLY` are ignored — a cancellation carries the same event, link
and times as the invitation, and acting on it would join a meeting that was just called off.

The ics is **unfolded** before it is read. iCalendar wraps lines at 75 octets, and a Zoom join
URL is longer than that — read raw, the URL is truncated mid-token while the meeting number
(nearer the front) still comes out right, so the bot joins the correct meeting holding a
broken link and nothing reports a problem.

Switches: `ORCH_GMAIL__ACCEPT_ZOOM_INVITE_BODIES=false`,
`ORCH_GMAIL__ACCEPT_TEAMS_INVITE_BODIES=false`,
`ORCH_GMAIL__ACCEPT_CALENDAR_INVITATIONS=false`, `ORCH_GMAIL__CALENDAR_INVITE_LEAD_S=300`.
The two body switches are independent: turning Teams off leaves Zoom working, and vice versa.

> **What these open routes accept.** Anyone who can email the bot a Zoom or Teams invitation
> block — or a valid `.ics` for a live meeting, or that exact Zoom subject — can make it join,
> with a link of their choosing. There is no cryptographic difference between the host's invite
> and a stranger's; both are ordinary mail. The mitigations are that the block signature is
> specific rather than any mention of the platform, that the bot's address is not usually
> published, and that stale invites age out via `MAX_INVITE_AGE_S`.
>
> For Teams this is the *only* route, since there is no system sender to allow-list — so
> switching it off does not fall back to a narrower Teams path, it turns Teams-by-mail off.

### Everything else is still sender-gated

`ORCH_GMAIL__ALLOWED_SENDERS` governs every other invite, and takes three entry forms:

| entry | grants |
|---|---|
| `no-reply@zoom.us` | that one mailbox |
| `@yourcompany.com` | anybody at that domain |
| `*` | anybody at all |

```bash
ORCH_GMAIL__ALLOWED_SENDERS='["no-reply@zoom.us","meetings-noreply@google.com","@yourcompany.com"]'
```

A domain entry is an exact match on the part after `@` — `@company.com` admits
`priya@company.com` and rejects `bad@notcompany.com` and `bad@company.com.evil.net`. Matching
is always on the **parsed** address, never the raw header: a display name reading
`no-reply@zoom.us` is something anyone can set.

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
  one would be handed to the bridge as a meeting number. The Zoom (`/j/`, `/wc/`, `/s/`) and
  Teams (`/meet/<digits>`, `/l/meetup-join/`) patterns require a path segment for the same
  reason — otherwise `zoom.us/download` and `teams.microsoft.com/downloads` become meetings.
- **The query has to admit what the parser would accept, or the feature is silently dead.**
  A pasted invitation matches no `from:` (arbitrary sender), no `subject:` (the event's title)
  and no `filename:ics`, so the query carries body terms — `"zoom.us"`, `"teams.live.com"`,
  `"teams.microsoft.com"`. Both Teams hosts, because which one an invite uses is decided by the
  organiser's account type. Losing precision here is safe; losing recall means the message is
  never *fetched*, and the parser that would have accepted it never sees it.
- **Flattened HTML bodies are repaired before anything is read out of them.** Gmail's HTML view
  drops the newlines between an invitation's blocks, and every pattern here ends at whitespace
  — so `Passcode: aB3dE9` followed by `Dial in by phone` reads as `aB3dE9Dial`. Plausible,
  passcode-shaped, and rejected by the meeting. `restore_line_breaks` puts the boundaries back
  at known block labels, URL starts, and the `---`/`____` section rules both platforms use.
- **Duplicate joins are guarded three ways**, because a meeting can be both on the calendar
  and instant-invited: the processed-id file (survives restarts), a short in-process
  meeting-code TTL (covers the gap before a session is listed), and a live check against the
  bridge's `GET /sessions`. The bridge's `POST /sessions` is not idempotent and will happily
  start a second avatar in the same meeting.
- **A failed join is retried, but only `ORCH_GMAIL__MAX_ATTEMPTS` times.** Without a cap, a
  bridge outage would retry the same invite every 5 seconds until it aged out.

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
