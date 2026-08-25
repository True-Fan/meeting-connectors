# How to use each connector (plain-English guide)

This page answers "how do I actually use this, day to day" — no code, no architecture.
For the engineering detail behind any claim here, follow the link at the end of each
section into `connectors/*.md`.

Two things are true for **every** platform below, so they're stated once instead of three
times:

- **The avatar's mailbox is one bot account** (e.g. `jadumeetboot@gmail.com`) with its own
  Google Calendar. Inviting that address to a meeting — on any platform — is what
  `calendar-orchestrator` watches for. See [calendar-orchestrator.md](calendar-orchestrator.md).
- **A raised hand and speaking over the avatar do the same thing**: the avatar stops
  talking immediately and waits for you to go ahead, on every platform that supports either
  signal.

---

## Google Meet

### Ways to get the avatar into a meeting

| Way | How | What actually happens |
|---|---|---|
| **Schedule it** | Invite the bot's email to the calendar event, same as inviting a person | `calendar-orchestrator` sees the event on the bot's own Google Calendar and joins **~1 minute before the meeting starts** — nobody has to be at a keyboard |
| **Add it mid-meeting** | Click **"Add people"** in a call already running and add the bot's email | Google emails the bot's inbox; a faster poller (every 5s) picks that up and joins **within seconds** — this path is opt-in and off by default |
| **Trigger it by hand** | Run one `curl` command with the meeting code | Joins immediately, no calendar or email involved at all — this is what you'd use to test, or if you don't want calendar-orchestrator running |

The bot account must be **signed into a real Chromium browser profile once, in advance** —
Google gives no other way for anything to publish audio/video into a Meet call. See
[connectors/google-meet.md](connectors/google-meet.md#2-sign-in-once-interactively).

### What it can do once it's in

- **Raise hand** — someone raises their hand in Meet's UI → the avatar stops talking mid-sentence and yields the floor.
- **Chat** — the avatar only answers chat messages that `@mention` it — matches `@AI Avatar`, `@ai_avatar`, `@AIAvatar`, `@ai-avatar` (case doesn't matter, dashes/underscores/spaces are all treated the same). "sounds good, thanks!" is ignored; "@AI Avatar what's the notice period?" is answered. Meet has no real @mention feature, so this is matched from the plain text of the message.
- **Voice** — talk over the avatar and it stops and listens, same as raising a hand.
- **Knowledge of the room** — the avatar is told, in the background, who's currently in the meeting and who's speaking, so it can answer "who's here?" without anyone asking it to look. This is a periodic status update, not something it announces out loud.

**Status**: this is currently the most exercised connector of the three — the one to reach for first if you're not sure which platform to test with.

---

## Zoom

The bot joins as an ordinary browser participant — the same recipe as Google Meet, just
pointed at Zoom's web client. Nothing extra to set up on your end.

### Ways to get the avatar into a meeting

| Way | How | What actually happens |
|---|---|---|
| **Schedule it** | Invite the bot's email to the calendar event (or add it via Zoom's own scheduling that syncs to the calendar) | Joins **~1 minute before start**, same as Google Meet |
| **Invite it mid-meeting** | In a running meeting, use **Invite → Email** and add the bot's address | Zoom emails the bot; picked up and joined **within seconds** |
| **Trigger it by hand** | Run one `curl` command with the meeting number (and passcode, if any) | Joins immediately |

### What it can do once it's in

- **Raise hand** — interrupts the avatar and asks you to go ahead.
- **Chat** — same `@mention` rule as Google Meet: `@AI Avatar`, `@ai_avatar`, `@AIAvatar`, `@ai-avatar` all trigger it; anything else is ignored.
- **Voice** — talking over the avatar interrupts it, same as a raised hand.
- **Knowledge of the room** — same background awareness of who's present and who's speaking.

**Honest status note**: the "browser join" mode above (what an invited meeting actually
uses) is built and tested but hasn't yet been run against a real, live Zoom meeting in
production — treat a first run as a smoke test, not a guarantee. See
[connectors/zoom.md § Status](connectors/zoom.md#status) for exactly what's verified vs.
pending.

---

## Microsoft Teams

Same shape as Zoom, for the same reason: an invited meeting is someone else's, so the bot
joins as an ordinary (guest) browser participant — no Azure app, no admin consent, nothing
to configure on your end for this path.

### Ways to get the avatar into a meeting

| Way | How | What actually happens |
|---|---|---|
| **Schedule it** | Invite the bot's email to the calendar event (or add it via the Teams app's own scheduling, which syncs to the calendar) | Joins **~1 minute before start** |
| **Invite it mid-meeting** | Paste the meeting's Teams link into an email to the bot's address | Picked up **within seconds** — Teams, unlike Zoom/Meet, sends no automatic "invite by email", so a pasted link is the only mid-meeting route |
| **Trigger it by hand** | Run one `curl` command with the meeting ID/passcode, or the join link | Joins immediately |

### What it can do once it's in

- **Raise hand** — interrupts the avatar and asks you to go ahead.
- **Chat** — same `@mention` rule: `@AI Avatar`, `@ai_avatar`, `@AIAvatar`, `@ai-avatar`.
- **Voice** — talking over the avatar interrupts it.
- **Knowledge of the room** — same background awareness of attendance/speaker.

**Honest status note**: built and unit-tested, but — like Zoom's browser mode — not yet
verified against a real, live Teams meeting. See
[connectors/teams.md § Status](connectors/teams.md#status).

---

## The other join method, for all three platforms

Every "trigger it by hand" row above is the same one call, just with a different
`platform`:

```bash
curl --location 'localhost:8000/sessions' \
  --header 'content-type: application/json' \
  --data '{"platform": "google_meet", "meeting_number": "abc-defg-hij"}'
```

`platform` is one of `google_meet`, `zoom_web`, `teams_web`. See each connector doc's "Join
a meeting" section for the exact fields that platform needs.

## What's not covered here

This page is about *using* a connector that's already set up. First-time setup (signing in
a browser profile, registering a Zoom/Azure app, standing up the calendar watcher) is one
level down, in [connectors/google-meet.md](connectors/google-meet.md),
[connectors/zoom.md](connectors/zoom.md), [connectors/teams.md](connectors/teams.md), and
[calendar-orchestrator.md](calendar-orchestrator.md). [RUNBOOK.md](RUNBOOK.md) is the
one page to keep open while doing that setup.
