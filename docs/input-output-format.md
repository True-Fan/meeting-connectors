# Input / output formats — what each connector gives and takes, in plain terms

This covers the three connectors that are actually built and running today —
`google_meet`, `zoom_web`, `teams_web` — all three built on the same recipe (a real
Chromium browser, joining as a participant). Three questions this page answers for each:

1. What does the meeting send **to** the avatar agent — audio, video, or both?
2. What format does the avatar agent need that in?
3. What does the avatar agent send **back**, and does it actually show up as video in the
   meeting, or just as sound — and does any of this touch a **LiveKit room**?

## The one-sentence version

**All three connectors send the avatar audio only — never video — and get back audio *and*
video; only Google Meet actually shows that video in the meeting today. None of them talks
to LiveKit at all — LiveKit lives one hop further away, inside the avatar agent itself.**

## The picture

```
 the meeting                meeting-connectors (this repo)             the avatar agent
 (Zoom / Teams / Meet,       "the bridge" — no AI inside it            (a different repo/service)
  joined via Chromium)
┌──────────────────┐        ┌───────────────────────────────┐        ┌───────────────────────────────┐
│                   │        │                               │        │                               │
│  people talking   │──────▶ │  captures ONLY audio           │──────▶ │  speech-to-text → LLM → text  │
│                   │  audio │  from the meeting               │  PCM   │  -to-speech, i.e. the actual  │
│                   │        │                               │  audio │  "brain" of the avatar         │
│                   │        │                               │        │                               │
│  avatar's face +  │◀────── │  plays back whatever the       │◀────── │  renders its reply as a talking│
│  voice, IF the    │ audio/ │  avatar agent sent             │  fMP4  │  -head video + voice, wrapped  │
│  connector can     │ video  │                               │  audio+│  in a LiveKit room internally │
│  show video        │        │                               │  video │  (agent.py + avatar_gateway)  │
└──────────────────┘        └───────────────────────────────┘        └───────────────────────────────┘
```

The bridge (this repo) and the avatar agent talk over exactly **one WebSocket**, and that
socket carries the *only* two formats that ever exist in this whole system — see below.
LiveKit is entirely inside the right-hand box; nothing in this repo opens a LiveKit
connection, joins a LiveKit room, or knows LiveKit exists.

## 1. What goes INTO the avatar agent (all three connectors, no exceptions)

| | Value |
|---|---|
| What's captured | **Audio only.** No connector — not even Google Meet, which can see video — ever sends meeting video to the avatar agent. |
| Format | PCM, 16,000 samples/second, mono (one channel), 16-bit — think "phone-call quality," not music quality. This is fixed and never changes; it's asserted at startup, not converted on the fly. |
| Why audio-only | The avatar agent's whole job is listening and talking (speech-to-text → LLM → text-to-speech); it has no use for video of the meeting, so no connector bothers capturing it. |

So "how does the avatar know who's present / who's speaking" (the **knowledge** feature in
[USAGE.md](USAGE.md)) is **not** the avatar watching video — the connector reads the
meeting's own participant list / active-speaker signal itself, writes it up as a plain-English
sentence ("Aarav and Priya are in the meeting; Rahul was invited and never joined"), and
sends *that sentence* to the avatar as a small text message alongside the audio. The avatar
never sees a face.

## 2. What comes OUT of the avatar agent, and where it actually goes

The avatar agent always sends back **one stream containing both audio and video** — its
voice and its rendered talking-head — packaged as fragmented MP4 (the same technique
video-streaming sites use to send video a few seconds at a time). What the connector then
*does* with that stream is where the three genuinely differ:

| Connector | Avatar's **voice** heard in the meeting? | Avatar's **video** seen in the meeting? |
|---|---|---|
| **Google Meet** (`google_meet`) | ✅ Yes | ✅ Yes — the only one of the three that shows it, via a synthetic camera track |
| **Zoom** (`zoom_web`) | ✅ Yes | ❌ No — video arrives from the avatar agent and is thrown away; nothing appears on camera |
| **Teams** (`teams_web`) | ✅ Yes | ❌ No — same as Zoom |

**Practical takeaway**: on Zoom and Teams you will **hear** the avatar but not **see** it.
Only Google Meet shows the avatar's face today.

## 3. Does any of this publish into a LiveKit room?

**No — not from this repo, ever.** Every connector publishes straight into the real
meeting (Zoom, Teams, or Meet) through the browser tab it's joined with — a synthetic
microphone (all three), plus a synthetic camera (Google Meet only). None of them opens a
LiveKit connection or knows what LiveKit is.

LiveKit only exists **inside the avatar agent**, a separate service this repo doesn't own
and never modifies — it's how that service's internal STT → LLM → TTS pipeline is wired
together (`avatar_gateway` translates the one WebSocket protocol described above into
LiveKit tracks, and back). If you're trying to change anything LiveKit-related, you're
looking for the avatar-agent repo, not this one — see
[RUNBOOK.md § the avatar agent (external)](RUNBOOK.md#the-avatar-agent-external).

## See also

- [USAGE.md](USAGE.md) — how to actually get the avatar into a meeting and what it can do once it's there.
- [HLD.md](HLD.md) — the same picture as above, with the engineering detail.
- [LLD.md § 7 — the shared media pipeline](LLD.md#7-the-shared-media-pipeline-srcservicesmedia) — exact code paths for every arrow in the diagram.
