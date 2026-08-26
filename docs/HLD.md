# High-Level Design

## 1. What problem this solves

An AI "Streaming Avatar Agent" already exists: it listens, thinks, and speaks, over a
WebSocket that carries PCM audio in and a fragmented-MP4 (fMP4) audio+video stream out. It
knows nothing about Zoom, Teams, or Google Meet, and it must never be modified to learn — it
is used by other products too.

**meeting-connectors is the bridge.** It joins a meeting on whichever platform as an ordinary
participant, and translates in both directions:

```
Human speaks → platform → bridge → Avatar Agent → fMP4 → decode → platform → meeting
```

The avatar's contract is fixed and identical for every platform: **PCM 16 kHz mono in,
fragmented MP4 out.** Every connector's whole job is making its platform's native media shape
meet that contract — nothing downstream of ingest, and nothing upstream of publish, needs to
know which platform it's talking to.

The bridge **contains no AI** and never modifies the avatar. Everything that makes the avatar
sound and behave a particular way — language, persona, when to speak — lives in the agent,
which is a separate deployable this repo talks to over one WebSocket and never imports.

## 2. The three services

```
┌─────────────────────┐     watches calendar/inbox      ┌──────────────────────┐
│  calendar-orchestrator│ ──────────────────────────────▶│  Google Calendar /   │
│  (own FastAPI app,   │                                 │  Gmail (the bot's    │
│   port 8200)         │                                 │  own account)        │
└──────────┬───────────┘
           │ POST /sessions  {platform, meeting_number, passcode, ...}
           │ ~60s before each meeting starts, or within seconds of an instant invite
           ▼
┌───────────────────────────────────────────────────────────────────────────┐
│  meeting-connectors  (this repo — FastAPI app, port 8000)                 │
│                                                                           │
│   POST /sessions ──▶ MeetingService ──▶ ConnectorRegistry ──▶ connector   │
│                                                                           │
│   one of: zoom_web · teams_web · google_meet                             │
│   (a session joins exactly one platform, chosen per request)             │
│                                                                           │
│   every connector feeds the SAME shared pipeline:                        │
│   AudioSource → EchoGuard → AvatarClient ⇄ (WebSocket) ⇄ avatar_gateway   │
│                              ↑                              │             │
│                     DecodePipeline ← fMP4 ←──────────────────┘            │
│                              │                                            │
│                            Pacer → MediaSink (publishes into the meeting) │
└───────────────────────────────────────────────┬─────────────────────────┘
                                                  │ ws://.../stream  (PCM in / fMP4 out)
                                                  ▼
┌───────────────────────────────────────────────────────────────────────────┐
│  avatar_gateway  (external repo, port 8100) ⇄ LiveKit room ⇄ agent.py     │
│  Translates the bridge's fMP4/PCM protocol into LiveKit tracks and back;  │
│  this is where the actual STT → LLM → TTS agent lives.                    │
└───────────────────────────────────────────────────────────────────────────┘
```

Only **meeting-connectors** is documented in depth by this doc set (it's the repo these docs
live in). `calendar-orchestrator` is a sibling directory in the same repo but a genuinely
separate deployable — its own venv, its own FastAPI app, its own port. The avatar agent
(`avatar_gateway.py` / `agent.py`) lives in a different repository entirely; it is treated
here as an external dependency with a fixed contract, documented only as far as that contract
goes (see [RUNBOOK.md](RUNBOOK.md#the-avatar-agent-external)).

**Why three separate services instead of one.** Each has a reason to fail, deploy, and scale
independently:
- The bridge needs no AI credentials and no calendar access — it can be stood up and tested
  with zero knowledge of the agent behind it (in-repo fakes for every
  external dependency make this literal — see [LLD § testing seams](LLD.md#12-testing-seams)).
- The orchestrator needs no meeting-platform credentials at all — it only reads a calendar
  and an inbox, and could be pointed at a different bridge entirely.
- The avatar agent needs no meeting-platform or calendar knowledge — swapping Zoom for Teams
  never touches it.

## 3. meeting-connectors: platforms it can join

| | Zoom (`zoom_web`) | Teams (`teams_web`) | Google Meet (`google_meet`) |
|---|---|---|---|
| How the avatar gets in | Ordinary browser participant (Playwright) | Ordinary browser participant (Playwright) | Ordinary browser participant (Playwright) |
| Ingest | Tapped from the page | Tapped from the page | Tapped from the WebRTC peer connection |
| Needs from the meeting owner | Nothing — joins like a guest | Nothing — joins like a guest | A Google account signed in once, in advance |
| Where the "credential" lives | A Chromium profile with a mic selected | A Chromium profile (optional) | A Chromium profile (**required**) |
| Runs on | Same container as the bridge | Same container | Same container |
| Status | ✅ built and unit-tested; selectors unverified against a live meeting | ⚠️ built and unit-tested; selectors not yet verified against a live meeting | Roadmap only in the original design, **now the connector most exercised in practice** (see `docs/README-gateway.md` in the avatar-agent repo) |

**All three join the same way: as an ordinary participant in a real, headless browser tab.**
None needs anything granted by whoever owns the meeting — no special account entitlement, no
app registration, no consent flow — which is exactly why this is what
`calendar-orchestrator` always uses (see
[calendar-orchestrator.md § Platforms](calendar-orchestrator.md#platforms)).

**Why Google Meet's browser profile must be signed in, and the other two don't strictly need
one.** Google's real-time media API is receive-only — it states so explicitly — so there is
no way for anything to publish into a Meet call except a real, signed-in browser; the account
identity has to come from somewhere, and a persistent, pre-authenticated profile is it. Zoom
and Teams accept an anonymous guest, so their profiles exist for smaller reasons (a
pre-selected microphone for Zoom, avoiding repeated consent prompts for Teams) rather than out
of necessity.

A session picks its platform per request (`POST /sessions {"platform": "..."}`), not per
deployment — a single running bridge can hold a Zoom session and a Teams-web session at the
same time, and a connector that isn't configured (no credentials, no browser profile) simply
isn't registered, so requesting it fails fast with a clear error rather than partway through a
join.

## 4. The invariant that makes this tractable

**Every connector implements the same handful of ports** (`src/protocols/`) — `AudioSource`,
`MediaSink`, optionally `ChatSource`/`HandRaiseSource`, and `ConnectorSession` /
`ConnectorSessionFactory`. Everything on the other side of those ports — the media router, the
jitter-buffering pacer, the echo suppressor, the fMP4 decoder, the shared clock, the avatar
WebSocket client, the session state machine, the HTTP API — is **one implementation shared by
all three connectors**, written once and never touched as Zoom-web, Teams-web, and Google Meet
were added one at a time. Adding a connector was, and remains, "a new folder, one enum
member, and one line in the DI container" — see
[LLD § adding a connector](LLD.md#13-adding-a-fourth-connector).

This is also why the three connectors (Google Meet, Zoom-web, Teams-web) look almost
identical to each other despite being separate packages: they share the same recipe (inject
JS, patch `getUserMedia`, tap `RTCPeerConnection`, talk to Python over a loopback WebSocket)
without sharing a base class, arrived at independently and only converged on later — see
[LLD § the page-bridge pattern](LLD.md#8-the-page-bridge-pattern-google-meet-zoom-web-teams-web).

## 5. Request lifecycle, end to end

1. Something decides a meeting should be joined — a human running `curl`, or
   `calendar-orchestrator`'s scheduler firing 60 seconds before a calendar event.
2. `POST /sessions` (see [LLD § API](LLD.md#2-api-layer)) — the call returns **202 Accepted**
   immediately; the avatar is visible in the meeting, publishing idle media, before ingest is
   even confirmed working. Zoom in particular may still be waiting on a webhook at this point.
3. `MeetingService` resolves the platform to a registered connector, allocates a
   `SessionContext`, and asks the connector to `start()`.
4. The connector's `AudioSource` and `MediaSink` come alive (mechanics are entirely
   platform-specific — see the per-connector docs) and are wired into the **shared media
   pipeline** (`MediaRouter`).
5. Meeting audio flows: `AudioSource` → `EchoGuard` (drops the avatar's own voice) →
   `AvatarClient` → the avatar agent, as PCM.
6. The avatar agent's reply flows back: fMP4 fragments → `DecodePipeline` (ffmpeg) → raw
   audio/video frames → `Pacer` (releases them on a shared clock, absorbing the agent's
   jitter) → the connector's `MediaSink` → the meeting.
7. A `SessionSupervisor` polls the connector's health every second in the background; a
   platform-specific failure that doesn't recover within a grace window fails the session.
   Chat, raised hands, and speaking-over-the-avatar are optional signals that, when a
   connector supplies them, can interrupt the avatar mid-sentence and hand the floor back —
   see [LLD § barge-in](LLD.md#67-barge-in-unification).
8. `DELETE /sessions/{id}`, a supervisor-detected failure, or process shutdown ends the
   session — teardown is symmetric and idempotent everywhere.

## 6. Cross-cutting properties, briefly

- **Every log line and every metric sample carries a `session_id` and `correlation_id`**,
  bound to ambient context so no call site threads them through by hand, and stamped on every
  media frame so the two can never drift apart mid-session.
- **A connector is registered only when its configuration says it's wanted.** A deployment
  with `MC_TEAMS_WEB__ENABLED` unset carries no Teams code path in its running process; a
  broken optional connector's config is logged and skipped, never a boot failure.
- **Enforced import boundaries.** `domain/` depends on nothing; `protocols/` depends only on
  `domain/`; `api/`, `services/`, `avatar/` never import a connector; each connector's page
  wire format never leaves its own package. These are asserted by an architecture test, not
  just convention. (One deliberate exception, recorded as debt: `zoom_web` and `teams_web`
  import Chromium's driver and launch-plan builder from `google_meet`.)
- **Nothing here is untestable without the real platforms.** Every external dependency — the
  avatar agent itself, the page channel, even a browser — has an in-repo fake or stub
  that speaks the real wire protocol, so the bridge's own logic is verified before (or
  entirely without) any of the real platforms being reachable.

Continue to [LLD.md](LLD.md) for how each of these pieces actually works.
