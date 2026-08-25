# meeting-connectors — Documentation

This is the documentation set for the whole system: the **meeting-connectors** bridge (this
repo), its **calendar-orchestrator** sidecar service, and the external **Streaming Avatar
Agent** it talks to. It was written to let someone with no prior context understand what
exists, why it is shaped the way it is, and how to bring it up on a laptop.

## Start here

| Doc | Read this for |
|---|---|
| [HLD.md](HLD.md) | The system in one picture: what each service is, how they talk to each other, what problem the architecture solves. Start here. |
| [LLD.md](LLD.md) | The bridge's internals: every module, the session state machine, the shared media pipeline, the wire protocols, the API contract. Read this to change code. |
| [RUNBOOK.md](RUNBOOK.md) | Copy-pasteable commands to bring every service up locally, per platform, plus what to check when something doesn't work. |
| [calendar-orchestrator.md](calendar-orchestrator.md) | The scheduler that watches a calendar/inbox and calls `POST /sessions` automatically, so nobody has to run a `curl` by hand. |
| [connectors/google-meet.md](connectors/google-meet.md) | Google Meet connector: architecture, setup, run. |
| [connectors/zoom.md](connectors/zoom.md) | Both Zoom connectors (`zoom` — Meeting SDK, `zoom_web` — browser): architecture, setup, run. |
| [connectors/teams.md](connectors/teams.md) | Both Microsoft Teams connectors (`teams` — Graph/.NET sidecar, `teams_web` — browser): architecture, setup, run. |

## The three services, in one sentence each

- **meeting-connectors** (this repo) — joins a meeting on Zoom, Teams, or Google Meet as a
  bot, and bridges its audio/video to and from a Streaming Avatar Agent over a fixed,
  platform-agnostic contract. This is the only service documented in depth here; it contains
  no AI.
- **calendar-orchestrator** (`calendar-orchestrator/`, a separate deployable) — watches the
  bot's Google Calendar and inbox, and calls `meeting-connectors`' `POST /sessions` a minute
  before a meeting starts, or the moment someone invites the bot into one already running. It
  never touches a meeting itself.
- **Streaming Avatar Agent** (external, not in this repo — see
  [RUNBOOK.md § avatar agent](RUNBOOK.md#the-avatar-agent-external)) — the actual AI: speech
  recognition, an LLM, and text-to-speech, wrapped in an `avatar_gateway` that speaks the one
  WebSocket protocol `meeting-connectors` expects. Bring this up first; without it the bot
  joins a meeting and sits there silently.

## How the docs are organised

- **HLD** answers "what is this and why" — one system diagram, one paragraph per service,
  the contracts between them.
- **LLD** answers "how does the code work" — classes, state machines, queues, wire formats,
  file:line references into `src/`.
- **connectors/*.md** answer "how do I run *this specific platform*" — each platform's
  two-app-registration dance (Zoom), Azure AD + Windows host (Teams), or browser-profile
  sign-in (Google Meet, Zoom-web, Teams-web), plus the exact `curl` to join a meeting on it.
- **RUNBOOK.md** is the one page to keep open in a terminal — every process to start, in
  order, for every platform, gathered from the connector docs and the top-level README.

None of this replaces reading the code — the source in `src/` is unusually heavily commented
(most non-trivial settings and design decisions are documented as docstrings next to the
field they describe, in `src/config/settings.py`). These docs are the map; the code is the
territory.
