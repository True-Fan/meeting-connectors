# 008 — Zoom-web meeting awareness

**Status:** built. Scope: the `zoom_web` connector only. Nothing in `google_meet`, `zoom`
(SDK) or `teams` changed behaviour — the RTMS additions are opt-in and default to off, so
the SDK connector sends the handshake it always sent.

Supersedes nothing. Doc 007 §4–6 describes how the Google Meet connector answers the same
questions; this documents why Zoom answers most of them a completely different way.

---

## 1. The problem

The `zoom_web` connector could join a meeting, speak, and hear. It could not answer any
question *about* the meeting, and it could not be interrupted:

* who is in this meeting, who was, who never joined
* who is speaking right now
* what was said, by whom
* a question typed in the chat
* somebody raising a hand
* **somebody talking over the avatar** — it spoke every reply to the end regardless

Doc 007 solves all six for Google Meet, and most of that solution is a browser reading a
DOM. The temptation was to port it. That would have been wrong: Google publishes no
participant API a guest can call, so a DOM is genuinely the only source there. Zoom
publishes RTMS, and RTMS already carries five of the six with a name attached.

## 2. Where each signal comes from

| Question | Source | Why |
|---|---|---|
| Who is here / was here | RTMS `PARTICIPANT_JOIN` / `PARTICIPANT_LEAVE` | Zoom's own events, with `user_id` and `user_name` |
| Who is speaking | RTMS `ACTIVE_SPEAKER_CHANGE` | Authoritative and named — no energy analysis, no tile reading |
| What was said | RTMS `MEDIA_DATA_TRANSCRIPT` | Zoom transcribes **per participant**, so a name and the words arrive together |
| What was typed | RTMS `MEDIA_DATA_CHAT` | Same, with the sender on it |
| Voice interruption | RTMS `ACTIVE_SPEAKER_CHANGE` | See §4 — this is the non-obvious one |
| **Raised hand** | **injected page script** | RTMS has no hand-raise event. The indicator exists only on screen. |

Consequences worth stating, because they are what makes this connector shorter than the
Meet one rather than merely different:

* **Nothing is scraped except a raised hand.** No roster scan, no caption panel, no chat
  panel, no `aria-label` cleaning, no icon-glyph heuristics.
* **No visible UI action** except optionally opening the participants panel, which exists
  only for the hand-raise observer (`hand_raise_open_panel`).
* **Attribution needs almost no inference.** The Meet tracker's machinery for renaming an
  open turn, merging two observers, and adopting an anonymous turn has no counterpart
  here: there is one signal and it names somebody. Elimination survives as a *repair* for
  an event that arrived with only an id.
* **Nothing new touches the media path.** Transcript and chat are text streams on the
  existing RTMS connection; events arrive on the signaling socket. No frame is retagged,
  re-timed or delayed.

## 3. One media type per connection

`media_type` in the data handshake is validated by Zoom as a **single enum member, not a
bitmask**. This was learned the expensive way. The first implementation asked one socket
for `AUDIO|TRANSCRIPT|CHAT` (25):

```
rtms.text_subscription_refused
  RTMS media handshake failed (status=14): Media type invalid value
```

**A refused data handshake ends that connection** — and it was the connection carrying the
meeting's audio, so the avatar went deaf in a meeting it had already joined. The fallback
meant to save it could not: Zoom stops serving a media socket whose handshake it rejected,
so the audio-only retry was sent into a connection that would never answer and hung until
the socket died ~90 s later. Zoom's `meeting.rtms_stopped` webhook then tore the session
down, and the operator saw only `session.start_failed: no close frame received or sent`.

Zoom's own signaling response had been saying this all along: `server_urls` is a **map**
with separate `audio`, `video` and `transcript` entries.

So:

* **The audio connection is exactly what it always was** — `media_type: AUDIO`, `MediaParams(audio=…)`, one handshake, never retried.
* **Each text stream opens its own socket** (`_attach_text_streams`), using its named URL and falling back to `all`.
* Each is **best-effort independently**: transcript can be refused while chat succeeds, each records its own reason in `text_degraded`, and `RtmsAudioSource.health()` surfaces it.
* The text pumps share the task group but **return instead of raising**, so a text socket dying cannot cancel the audio pump.

The lesson is structural rather than about ordering or retries: **an optional subscription
must not share a socket with a mandatory one.** No amount of retrying made the shared
version safe; a separate connection makes the failure impossible to propagate.

Event subscription (`msg_type 5`) is best-effort and never fails an attach: some accounts
deliver `EVENT_UPDATE` unsolicited, so a rejection is not evidence the events will not
arrive. `EVENT_UPDATE` is handled on **both** sockets, because Zoom has been observed
delivering it on either.

## 4. Barge-in: why not audio energy

The Google Meet connector detects speech by measuring the inbound mix (`SpeechDetector`).
That works there because its echo gate is **open** — its capture tap is inbound-only, so
the avatar's own audio structurally cannot enter it.

Here it can. RTMS delivers the meeting's mix *including the avatar*, so `EchoGuard` runs
its speaking gate in strict mode and **withholds every inbound frame while the avatar is
talking**. An energy detector reads frames after the guard, so it would be deaf during the
only window a barge-in exists for. That is exactly the reported behaviour: the avatar
talks until it finishes, whatever anybody says over it.

`ACTIVE_SPEAKER_CHANGE` is a control message on the signaling socket. It arrives whether
or not the gate is withholding audio, and it names the person. So:

```
Zoom: floor moved to Priya  ──▶ ZoomInterruptSource.offer_voice
                                  ├─ is it us?            → drop (the avatar is an
                                  │                          active speaker whenever
                                  │                          it talks)
                                  ├─ is the avatar        → drop (nothing to interrupt)
                                  │  actually speaking?
                                  └─ cooldown ok?         → queue a HandRaise
                                                              │
MediaRouter._yield_floor ◀────────────────────────────────────┘
  1. Pacer.interrupt(hold_ms)   — local, immediate: drop queued avatar media and
                                  hold the line while what is in flight drains
  2. avatar.send_hand_raise()   — the agent is told to stop and hand over
```

Both halves are necessary: step 1 disposes of speech that already exists, and the agent
goes on *generating* the rest of its sentence until step 2 lands.

**A hand and a voice are one source** (`ZoomInterruptSource`), because they are one request
and the router already knows the answer. They differ in exactly one place: a hand
interrupts a silent avatar too, a voice does not. Firing on every utterance would send the
agent "stop talking and let them speak" on every sentence anybody says.

**Known cost.** Zoom takes a moment to declare a new active speaker, and the echo gate
reopens a hangover after the avatar stops. So the first fraction of a second of the
interrupting question is not delivered to the agent. The avatar stopping is what the room
notices; the agent hears the question from just after its start.

## 5. What the agent is told

One `meeting_context` frame, on change only, carrying attendance + speaker + transcript
together (`ZoomMeetingAnnouncer`). **One frame because an agent has one slot** — doc 007
records the live failure where a second pusher evicted the first and the avatar, asked who
was in the meeting, answered "Someone is present in the meeting".

Never on the chat channel: a chat frame is a turn the avatar says out loud, so pushing the
roster down it would have the avatar announce arrivals into the room.

The signature keys on presence, current speaker, candidates, and the **chat** line count —
not the total transcript line count. Re-sending standing context makes the agent rebuild
its frame and discard the reply it had begun preparing, and transcript lines arrive *while
somebody is talking*, which is the worst possible moment. Spoken lines still reach the
agent; they no longer *cause* a push.

## 6. HTTP

No new endpoints. `ZoomWebSession` exposes `attendance`, `speakers` and `transcript`, and
the snapshot types are field-for-field compatible with the Google Meet connector's — so
`GET /sessions/{id}/participants`, `/speakers`, `/transcript` and
`POST /sessions/{id}/invitees` serve a Zoom-web session through the existing duck-typed
path in `MeetingService`, with no change to `api/` or `services/`.

## 7. Settings

All under `MC_ZOOM_WEB__`. Each is documented in `config/settings.py`; the shape is:

* **subscriptions** — `RTMS_TRANSCRIPT_ENABLED`, `RTMS_CHAT_ENABLED`, `RTMS_EVENTS_ENABLED`
* **ledgers** — `ATTENDANCE_ENABLED`, `SPEAKER_TRACKING_ENABLED`, `TRANSCRIPT_ENABLED`
* **chat policy** — `CHAT_ENABLED`, `CHAT_REQUIRE_MENTION`, `CHAT_MENTION_NAMES`
* **agent brief** — `CONTEXT_PUSH_ENABLED`, `..._INTERVAL_S`, `..._REQUIRE_NEGOTIATION`
* **interruption** — `VOICE_INTERRUPT_ENABLED`, `HAND_RAISE_ENABLED`,
  `HAND_RAISE_OPEN_PANEL`, `HAND_RAISE_PROMPT`, `HAND_RAISE_COOLDOWN_S`,
  `HAND_RAISE_MUTE_MS`
* **turn shaping** — `SPEAKER_HOLD_MS`, `SPEAKER_MERGE_GAP_MS`

`ZoomWebConnectorConfig.from_settings` folds the consumers into the subscriptions: a stream
nobody reads is not requested, so a deployment with every consumer off sends the handshake
it sent before any of this existed.

## 8. Known risk: the hand-raise selectors

The one feature here that depends on markup Zoom is free to change. It degrades to
**silence**, which is indistinguishable from a meeting where nobody raised a hand — so the
observer reports diagnostics over the page channel (`handsArmed`, `handsIdle`,
`participantsPanelOpened`), logged as `zoom_web.page_event`.

Two passes run, so a rename has to break both before the feature goes quiet: a selector
pass over `automation/selectors.py`, and a label sweep matching the sentence Zoom shows a
human ("… raised hand") wherever it appears. Selectors travel to the page as data, so
tuning them is a code change in one file rather than an asset edit.

The most likely reason for finding nothing is the **participants panel being closed** —
Zoom renders a raised hand as a transient toast with no persistent DOM otherwise. That is
what `hand_raise_open_panel` exists for.
