# Microsoft Teams connector (`teams_web`)

The bot joins a Teams meeting as an anonymous guest — a real browser tab via Playwright, the
same recipe used for [Google Meet](google-meet.md) and [Zoom](zoom.md). Nothing needs to be
granted by whoever owns the meeting — no admin consent, no special setup on their end at all.
This is what `calendar-orchestrator` always uses for an invited meeting (see
[calendar-orchestrator.md](../calendar-orchestrator.md#platforms)).

## Architecture

The simplest of the three browser connectors — no ingest-mode branch, only one way in:

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

Unlike Zoom's browser connector, **no persisted microphone selection is required** — Teams
accepts whatever track `getUserMedia` returns, so an anonymous guest join works from a
throwaway profile. A profile is still worth setting for two reasons: a *signed-in* profile
joins as a named tenant user instead of a guest (which some organisers require, and skips the
lobby), and it keeps device/consent prompts from reappearing every session.

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

### 1. The persistent profile (optional)

```bash
.venv/bin/python scripts/teams_web_login.py --profile ~/.mc/teams-web-profile
```
Optional — but if you run it, **leave the microphone unmuted** on the pre-join screen; Teams
persists that toggle into the next call.

```bash
MC_TEAMS_WEB__ENABLED=true
MC_TEAMS_WEB__PROFILE_DIR=~/.mc/teams-web-profile   # optional
```

## Run

```bash
.venv/bin/uvicorn src.main:app --port 8000
```

## Join a meeting

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

## Status

⚠️ Built and unit-tested; selectors have **not yet been verified against a live meeting**.

## Troubleshooting

- **`teams_web` channel looks connected but nothing ever arrives** — check for
  `bypass_csp: false` first; a blocked CSP fails completely silently (no error, no close
  event) and looks identical to "the connector never tried."
- **A `teams.live.com` link lands on the app home instead of joining** — confirms the
  work/school→personal fallback didn't trigger; check `live_url_template` isn't empty and that
  the id was recognised as ambiguous rather than confidently (and wrongly) routed.
