# 009 — Zoom-web browser ingest

**Status:** built; **audio verified against a live Zoom meeting, observers partly.** Scope:
the `zoom_web` connector only, plus one shared-router bug it exposed (§9). Nothing in
`google_meet`, `zoom` (SDK) or `teams` changed by design; the router fix reaches Meet and is
argued in §9.

Supersedes doc 008 §2 and §4 for the `browser` ingest mode. Doc 008 remains authoritative
for `rtms` mode, which is unchanged and still the better leg where an account can serve it.

---

## 1. The problem, which is not a technical one

Doc 008 built meeting awareness on RTMS, and the reasoning in it is sound: Zoom publishes an
API that carries audio, transcript, chat and participant events with a name attached, and
building DOM observers for signals an API already serves would be paying Google Meet's price
for a problem Zoom does not have.

That argument has one premise, and the premise is not about Zoom's API. **RTMS requires the
meeting to be hosted on an account with RTMS enabled for the app.** Not the operator's
account — the *host's*. In every deployment where the avatar joins a meeting somebody else
booked, that entitlement is not available and cannot be obtained: it belongs to a customer,
a candidate, a prospect, a person with a free Zoom account.

So a connector that is correct in every respect is unusable in the case it exists for. This
document is about making it work in an ordinary person's ordinary meeting, at a cost that is
real and is stated in §5.

## 2. Where each signal comes from now

| Question | `rtms` (doc 008) | `browser` (this doc) |
|---|---|---|
| The meeting's audio | RTMS media socket | tapped from Zoom's own playout graph |
| Who is here / was here | `PARTICIPANT_JOIN` / `_LEAVE` | the participants panel |
| Who is speaking | `ACTIVE_SPEAKER_CHANGE` | Zoom's speaking indicator |
| What was said | `MEDIA_DATA_TRANSCRIPT` | the live-transcript panel, **if switched on** |
| What was typed | `MEDIA_DATA_CHAT` | the chat panel |
| Voice interruption | `ACTIVE_SPEAKER_CHANGE` | **audio energy** — see §4 |
| Raised hand | injected page script | injected page script (unchanged) |

Selected by `MC_ZOOM_WEB__INGEST_MODE`, which defaults to `browser`.

**The branch exists in exactly three places**, and that is the claim this design rests on:
which `AudioSource` is built, how `EchoGuard` is armed, and what the page is told to
observe. Everything downstream — `ZoomAttendanceLedger`, `ZoomSpeakerTracker`,
`ZoomTranscript`, `ZoomChatSource`, `ZoomInterruptSource`, `ZoomMeetingAnnouncer`,
`MediaRouter`, `Pacer`, `DecodePipeline`, and every HTTP endpoint — is untouched.

That was not luck. Doc 008 put the RTMS *observations* (`ParticipantEvent`, `SpeakerEvent`,
`TranscriptLine`, `ChatMessage`) in their own module precisely because they are "observations
one platform happens to make" rather than wire models. The page produces the same four types,
so the ledgers never learned there was a second producer.

## 3. Why the audio tap is not a peer-connection tap

Every write-up on browser meeting bots — including the one that prompted this work —
describes the same technique: patch `RTCPeerConnection`, read the inbound audio
transceivers, and you get one clean stream per participant. The Google Meet connector does
exactly that, in `bridge.js`.

**It does not work on Zoom, and the claim that it does is the load-bearing error in the
popular account of how these bots work.** Zoom's web client does not reliably carry meeting
audio over WebRTC. Its long-standing mode decodes audio in WebAssembly off a WebSocket and
renders it through Web Audio, and in that mode there is no inbound audio transceiver
anywhere on the page to find. A peer-connection tap is not fragile there; it is *empty*.
That is what an earlier attempt in this connector measured, and it is why `zoom_web` used
RTMS for ingest in the first place — the comment in `zoom_web_session.py` recording that
"Zoom's web client has no audio transceiver to tap" was accurate.

Two consequences follow, and the second is the important one:

* Per-participant audio is **not available** in this mode at any price. The individual
  streams do not exist by the time the audio is anywhere a guest script can reach it.
* The tap has to be placed at **playout** rather than at transport — the one point both
  modes must converge on, because audio that is going to be heard has to reach either an
  `AudioContext`'s destination or a media element.

So `installAudioTap` patches all three paths:

```
Zoom (WASM/WebSocket)  ──▶ AudioContext ──▶ destination
                                    │
                                    └─▶ (patched AudioNode.connect)
                                          MediaStreamDestination ─┐
Zoom (media element)   ──▶ <audio>.srcObject ───────────────────┐ │
Zoom (WebRTC, if used) ──▶ pc.ontrack ─────────────────────────┐│ │
                                                               ▼▼ ▼
                                         capture AudioContext @ 16 kHz
                                                       │
                                              mc-zoom-capture worklet
                                                       │
                                         binary frames ─▶ page channel ─▶ PageAudioSource
```

`AudioNode.prototype.connect` is patched rather than `AudioContext.destination` being
replaced, because the destination is a read-only accessor on an object Zoom holds a
reference to from before any patch could run. Intercepting the *edge* works regardless of
when the node was created. The original connect still happens, so Zoom's graph is unchanged
and the page sounds exactly as it did.

**Indifference to which transport Zoom chose is the point.** Zoom has changed its web media
stack before and will again; a tap at playout does not care, and one at transport has to be
rewritten each time.

## 4. Barge-in: the mode that can, and the mode that cannot

Doc 008 §4 explains at length why energy-based barge-in is impossible under RTMS: RTMS
delivers the mix *with the avatar in it*, so `EchoGuard` must run its speaking gate in strict
mode and withhold every inbound frame while the avatar talks — which is exactly the window a
barge-in exists in. An energy detector reads frames after the guard, so it is deaf precisely
when it is needed.

**None of that is true of the page tap, and the reason is structural rather than tuned.**
Zoom does not play a participant their own microphone, and the synthetic microphone is built
in an `AudioContext` that connects only to a `MediaStreamDestination` — never to a
destination the tap watches. The avatar's voice is therefore absent from the tapped audio by
construction, not by filtering.

So the gate is switched off (`gate_enabled=False`) exactly as it is on Google Meet, for
exactly the reason recorded there: a shut gate cannot tell the avatar's echo from somebody
talking over it, so it suppresses the interruption along with the echo. What remains is the
acoustic path — a participant listening on speakers — which the gate could never have caught
anyway.

Both triggers then run and converge on the same handover:

```
somebody speaks ──┬─▶ SpeechDetector (energy, after an open gate)  ─┐
                  │     fires on the first syllable                 │
                  └─▶ DOM speaking indicator ─▶ offer_voice ────────┤
                        fires when Zoom redraws the tile            │
                                                                    ▼
                                                    MediaRouter._yield_floor
                                          1. Pacer.interrupt(hold_ms)
                                          2. avatar.send_hand_raise()
```

`ZoomInterruptSource`'s per-participant cooldown already de-duplicates them; it was written
for a repeated DOM signal and a second producer costs it nothing. The energy path is the
better of the two — it fires on the first syllable rather than when Zoom gets round to
redrawing whose tile is highlighted — so **the known cost recorded in doc 008 §4 (the first
fraction of a second of an interrupting question never reaching the agent) does not apply to
this mode.**

**One setting must be revisited when switching.** `MC_ZOOM_WEB__ECHO_GATE_HANGOVER_MS` was
raised to 1500 ms for RTMS, for reasons entirely specific to RTMS's echo loop. It is unread
in browser mode because the gate is off, but leaving it set is a trap for anyone switching
back and forth.

## 5. What this costs, stated plainly

Browser ingest is worse than RTMS at everything except being available. The honest list:

* **No per-participant audio.** One mixed stream, every frame `participant=None`. Attribution
  comes from the DOM on a separate path, so it can be absent or late in a way it never was
  when a name arrived attached to the audio.
* **Two people called "Dev" are one person.** RTMS carries a user id; a DOM carries a name,
  and `ZoomAttendanceLedger` already keys on the name because ids are minted afresh on a
  rejoin. Nothing breaks — the count is simply wrong.
* **"Everybody left" is unobservable.** An empty roster is treated as a blind frame rather
  than an empty meeting, because `add_init_script` runs in every frame Chromium creates and
  most of Zoom's have no participants panel. The avatar is always in its own participants
  list, so a genuinely empty roster is not a state the page can see. The alternative — a
  re-render wiping the roster — is both more likely and worse.
* **The transcript needs captions switched on.** RTMS transcribed per participant with
  nothing asked of the meeting. Here `captions_auto_enable` clicks a control everybody can
  see, and it is off by default. With it off, the avatar can say who spoke and not what they
  said.
* **Panels have to be open.** The chat panel and the participants panel are opened on the
  avatar's own client. Nobody else is notified, but it is visible on a shared screen.
* **Selectors are a maintenance surface.** `ZoomObserverSelectors` is where a Zoom UI change
  lands. It degrades to silence, which is the failure mode that looks like nothing being
  wrong — see §6.

None of these touch whether the avatar can hear and be heard, which is why they are
acceptable in exchange for working at all.

## 6. Silence is the failure mode, so it is instrumented

Every observer here fails by finding nothing, and finding nothing is indistinguishable from
a quiet meeting. Three things tell them apart, and all three are already the pattern doc 008
§8 established for the hand observer:

* `zoom_web.page_event` with `audioTapped` — logged the moment a source is wired into the
  capture graph, with how it was found (`webaudio`, `srcObject`, `rtc`, `sweep`). **This is
  the first thing to check** if the avatar is deaf: no `audioTapped` line at all means the
  tap never attached, which is a different problem from a silent room.
* `PageAudioSource.health()` reports `DEGRADED` with `tapped=0` rather than claiming health,
  and refuses to guess which of the two situations it is in.
* `observerArmed` says a DOM observer found its first content, so an observer that never
  arms is one whose selectors miss.

## 7. Settings

New, all under `MC_ZOOM_WEB__`:

* **mode** — `INGEST_MODE` (`browser` | `rtms`, default `browser`)
* **audio tap** — `CAPTURE_FRAME_MS`
* **observers** — `OBSERVE_INTERVAL_MS`, `SPEAKER_MIN_MS`, `CAPTION_SETTLE_MS`
* **panels** — `CHAT_OPEN_PANEL`, `CAPTIONS_ENABLED`, `CAPTIONS_AUTO_ENABLE`
* **barge-in** — `SPEECH_INTERRUPT_THRESHOLD`

Every consumer switch from doc 008 §7 keeps its meaning in both modes; what changes is what
serves it. `ZoomWebConnectorConfig.from_settings` folds the mode in, so an
`RTMS_EVENTS_ENABLED=false` left over from an earlier configuration cannot silently disable
attendance and barge-in in a mode where RTMS is not involved.

## 8. What has not been verified

**This has not been run against a live Zoom meeting.** The unit tests cover the wire
format, the page-channel split, frame construction and clock discipline, the roster diff,
speaker/caption/chat routing, the config folds, and the echo-gate difference between the two
modes. None of that can establish the two things that actually decide whether this works:

1. **That the tap finds Zoom's audio.** The three patched paths are exhaustive over how a
   browser can render audio, so the mechanism is sound — but which one Zoom uses, in which
   frame, and whether it builds its graph before or after `add_init_script` runs, is an
   empirical question. `audioTapped` answers it in one meeting.
2. **That the selectors match.** Every value in `ZoomObserverSelectors` is a class name Zoom
   has used, not a guess — but "has used" is not "uses today, in this account's build". The
   `handsIdle`-style diagnostics are what correct them.

Run with `MC_ZOOM_WEB__HEADLESS=false` for the first meeting and read the
`zoom_web.page_event` lines. The tap is the thing to confirm first; the observers degrade to
a less capable avatar, whereas a tap that never attaches is a deaf one.

---

## 9. What the first live run changed

One meeting, and it settled §8's two open questions in opposite directions.

### 9.1 The tap works, first try

```
zoom_web.page_event  name=audioTapped  detail={'how': 'webaudio', 'sources': 1}
zoom_web.first_audio_tapped  samples=320
router.speech_detected  rms=1881  noise_floor=2  trigger_level=350
```

`how: webaudio` is the answer to §3: Zoom rendered its meeting audio through an
`AudioContext` destination, the patched `AudioNode.prototype.connect` caught the edge, and
the energy detector fired on real speech. **No peer-connection track was ever seen** — which
is the prediction §3 makes, confirmed.

### 9.2 The shared router was interrupting a silent avatar

The damaging finding, and it is not in this connector:

```
router.speech_detected   participant=Someone
router.floor_yielded     trigger=voice  was_speaking=False   ← the bug
avatar.chat_forwarded    sender=Someone
agent: "Ok, go ahead."
```

`MediaRouter._forward` yielded the floor on **every** detected utterance, whether or not the
avatar was speaking. So each time somebody asked a question, the agent was handed *"somebody
wants to say something, reply briefly like ok, go ahead"* immediately before the question
itself — and answered "Ok, go ahead." every single turn, in front of every real answer.

The rule was already written down in doc 008 §4 for `ZoomInterruptSource`: **a hand
interrupts a silent avatar too, a voice does not.** The router's leg simply never
implemented it. Every test in `test_speech_interrupt.py` wraps its body in `_avatar_talking`,
so the intent was never in doubt — the condition was missing, and no connector had an open
enough echo gate for it to show until this one.

A second defect surfaced with it: the "renew the hold while they keep talking" branch called
`Pacer.extend_hold`, which pushes `muted_until` forward from wherever it is and therefore
*mutes an unmuted pacer*. Without an interruption to renew, the avatar was held down for as
long as anybody spoke to it, and the agent's reply arrived into a muted pacer with its
opening discarded.

Both are fixed in `services/media/router.py`. **This changes Google Meet too**, which is
deliberate: it is the same bug there, with the same symptom available to anyone who talks to
a silent Meet avatar.

The detector is still shown every frame — the gate is passed *into* `_note_speech` rather
than applied around it — because the learned noise floor comes from the quiet frames, and
feeding it only the frames where the avatar happens to be talking would leave it
uncalibrated and firing on room tone.

### 9.3 The roster selectors were guessing at the wrong panel

With the participants panel confirmed open (`panelOpened` logged), every participants-panel
selector matched nothing, while the video tiles matched exactly the two people present:

```
handsIdle rows=2 sample=[
  {classes: [video-avatar__avatar, …-title, …-name, …-footer]},
  {classes: [video-avatar__avatar, …-title, …-img,  …-footer]}]
```

So `roster_row` now leads with `.video-avatar__avatar`, and `roster_name` leads with
`…-title` — the only name element present on *both* rows, because a camera-on tile carries
`…-img` where a camera-off tile carries `…-name`. Leading with `-name` would have read the
roster correctly right up until somebody turned their camera on.

**Cost of the tiles as a roster:** they are the people *on screen*. Zoom paginates and
virtualises the grid, so past a screenful the roster becomes whatever is rendered. Correct
for small meetings, wrong for a webinar.

### 9.4 "Someone wants to say something", in a two-person meeting

Attribution was empty on every interruption, because the mix carries none and the DOM speaker
marker had not been found. `_speaker_provider` now falls back to elimination — if exactly one
other person is present, it is them — which is the identical repair
`ZoomMeetingObserver._named` already applies to an unattributable raised hand, and fails
closed at two candidates for the same reason: a confidently wrong name is worse than none.

### 9.5 A held hand re-fired

The page retires a raised hand it has not *seen* for a grace window, which a tile re-render
can exceed — so an unmoved hand was re-detected as a fresh raise. The page now reports
`handLower` as well as `handRaise`, and `ZoomMeetingObserver` holds the authoritative
"whose hand is up" set: it outlives page re-renders, reloads and frames, which is the only
place that state can survive. Deliberately not the per-participant cooldown, which is a rate
limit and would let the repeat through anyway, only later.

### 9.6 Silence still needed better instrumentation

`handsIdle` reported `rows: 0` and was correct, but a count of zero says the guess was wrong
without saying what the right answer is. `observerIdle` now reports, per silent observer, the
match count of **each** selector plus **every class token on the page** containing a relevant
substring (`chat`, `transcript`, `speak`, …). That is what turns the next fix into an edit
rather than another meeting — §9.3 was solved from exactly this kind of evidence, obtained by
accident.

### 9.7 Still open

* **Chat** — no chat event was produced; `chat_item` selectors miss. `observerIdle` with
  `tokens` will name the real class in one meeting.
* **Active speaker** — `speaker_marker` unconfirmed. Degrades to elimination (§9.4), which
  covers the two-person case and nothing larger.
* **Transcript** — untested, because it needs `CAPTIONS_AUTO_ENABLE=true` and that was off.
  Without it the avatar cannot answer "who said what", by construction (§5).

---

## 10. Second live run

The run that looked worst and was mostly good. Worth recording because the *log* was the
defect, not the connector.

### 10.1 The diagnostics buried the thing they were built to explain

`observerIdle` fired every interval with `counts: all zero, tokens: []` — and the roster was
working the entire time:

```
zoom_attendance.joined  participant='dev Choudhary'  present=1
zoom_attendance.joined  participant='AI Avatar2'     present=2
avatar.meeting_context_sent  chars=518  topic=attendance
```

`add_init_script` runs in **every** frame Chromium creates, and most of Zoom's are helper
iframes with no meeting UI at all. `handsIdle` has guarded against reporting from those since
doc 008 §8; `observerIdle` was written without the equivalent, so dozens of blind frames
drowned the one frame that could see. It now stays silent when a frame's selectors match
nothing *and* the far-wider token sweep also finds nothing — which is not a diagnosis, it is
a frame that was never going to find anything.

**The lesson generalises:** a diagnostic that cannot distinguish "I looked and found nothing"
from "I was never able to look" is worse than none, because it reads as evidence of failure.

### 10.2 §9.2's fix is confirmed

```
router.speech_detected  attributed=True  participant='dev Choudhary'  rms=627
router.floor_yielded    trigger=voice    was_speaking=True
```

`was_speaking=True` — the barge-in now fires only into a talking avatar. `attributed=True`
with a name is §9.4's elimination working.

### 10.3 The active-speaker marker, found

The token sweep from the frame that can see:

```
speaker-active-container__wrap             layout, always present
speaker-active-container__video-frame      layout, always present
speaker-bar-container__video-frame         layout, always present
speaker-bar-container__video-frame--active **state**, comes and goes
```

So `[class*='speaker-active']` — the first entry in the old marker list — matched two
elements *permanently*, in every layout, talking or not. A marker that is always present is
not a marker, and the same objection retires the `[class*='speaking']` catch-all, which
matches the `speaker-…` prefix.

The state is the `--active` modifier, and it sits on the frame **containing** the tile that
carries the name — so `scanSpeaker` now checks `matches` / `querySelector` / **`closest`**.
Checking only self-and-descendants, as it did, could never have found it.

### 10.4 The roster flapped, and the fix is asymmetric

```
12:41:37  zoom_attendance.left      dev Choudhary  stayed_s=142.8
12:42:21  zoom_attendance.rejoined  dev Choudhary  rejoins=1
```

Nobody moved. The tile grid went from two to one and back as Zoom re-laid it out. Every flap
re-pushes the meeting brief, so the agent is told the room emptied and refilled, and
elimination briefly has nobody to name.

Departures are now held for `roster_leave_grace_s` (8 s); **arrivals are not held at all**,
because there is no layout in which Zoom invents a participant. The cost of a late leave is a
name lingering; the cost of an early one is the loop above.

### 10.5 Chat: found, and its key was wrong

`observerArmed {observer: 'chatArmed', existing: 1}` — the chat selectors match. But the
de-duplication key contained the item's **index in the list**, and Zoom virtualises that list
exactly as it does the tile grid. The roster flap is a re-push; the chat equivalent is the
avatar **re-answering old messages out loud**.

Keyed on the DOM node (a `WeakSet` — the same element is the same message, whatever moved
around it) plus the message content (covering virtualisation destroying and rebuilding the
element). The trade given up is that somebody typing the identical line twice is now one
message; a repeated identical line is rare and a re-render is continuous.

### 10.6 Still unverified

* The speaker marker, now that it is the right class and looked for in the right direction.
* A chat message actually reaching the agent — the run armed the observer but no tagged
  message followed.
* Captions, still off (`CAPTIONS_AUTO_ENABLE` defaults false).

---

## 11. Third live run

Chat and the roster are done. Two findings, one of which is about diagnostics again.

### 11.1 Working end to end

```
zoom_attendance.joined   participant='Dev Choudhary'
zoom_chat.received       attributed_by=elimination  sender='Dev Choudhary'  chars=15
avatar.chat_forwarded    chars=15   → agent: "Your name is Dev Choudhary."
router.floor_yielded     trigger=voice  was_speaking=True  attributed=True
```

§10.5's node-and-content chat key works, the roster no longer flaps, and the barge-in gate
holds. The agent answered "who is present in the meeting?" correctly from the pushed brief.

### 11.2 "What is my name?" — answered in chat, not by voice

The same question, two channels, two answers:

```
chat:  "@AI Avatar what is my name"  → "Your name is Dev Choudhary."
voice: "What is my name?"            → "I'm sorry, but I don't know your name."
```

The brief already said `Currently in the meeting (1): Dev Choudhary`, so the information was
present and the agent did not use it. The asymmetry explains why: **a chat message arrives
with its sender attached and a spoken turn does not.** The avatar hears one mixed stream
carrying no attribution at all, so nothing connects "the voice asking" to "the name in the
roster" — and the agent, correctly, refuses to guess.

With exactly one other participant that inference is not a guess. So the brief now makes it
outright rather than leaving it to be drawn:

> "Dev Choudhary" is the only other person here, so anyone speaking to the avatar right now
> is "Dev Choudhary" — answer with that name when asked who they are.

Silent at two or more, for the reason every elimination here fails closed. This is the same
repair as §9.4 applied one layer up: there, to the interruption prompt; here, to the standing
context, because a spoken *question* needs it as much as an interruption does.

**Caught while writing the test rather than by it:** `AttendanceSnapshot.present` holds
`AttendanceRecord`s, not names, so the first version interpolated a dataclass repr —
timestamps and user ids — into the agent's context window. A test asserting only that the
name appeared *somewhere* passed against it. The test now pins the whole sentence and asserts
no repr leaked.

### 11.3 The active-speaker marker: sampling was the wrong instrument

Three runs have failed to identify it, for a reason worth recording because it invalidates
the diagnostic rather than the guess:

* Run 2 caught `speaker-bar-container__video-frame--active` **once**, by luck.
* Run 3 sampled nine times, with the corrected selector in place, and never saw it — every
  count zero, every token list showing only the constants.

`observerIdle` samples the page every fifteen seconds, and almost none of those moments are
moments when somebody is talking. **Absence from a snapshot is not absence from the DOM**,
and a diagnostic that cannot tell those apart will keep reporting failure at a marker that is
present and working.

A state marker is by definition a class that *toggles*. So `speakerChurnScan` now runs on
every 700 ms scan and records which class tokens **appear or disappear** between scans,
reported as `churn` (`+token` appeared, `-token` went away). This needs no correlation with
audio, cannot miss a marker that appeared at all, and drops the layout constants
(`speaker-active-container__wrap`, `…__video-frame`) for free — which is precisely the
distinction the previous two rounds of selectors got wrong.

Scoped and shallow, because it runs on the thread that encodes the avatar's audio: the class
attribute of a bounded set of `[class*='speaker']` / `[class*='video-avatar']` elements, no
descent.

---

## 12. Chat de-duplication, third attempt

Reported from a meeting: **pasting a previously-sent message produced no reply, retyping a
different one worked.** That is §10.5's stated trade, and the justification for it was wrong.

Three versions, and the first two are opposite failures with the same root cause:

| key | re-render | identical message re-sent |
|---|---|---|
| `index + name + text` | ❌ re-answers the backlog aloud | ✅ answered |
| `name + text` in a set | ✅ silent | ❌ **ignored entirely** |
| occurrence count vs high-water mark | ✅ silent | ✅ answered |

The cause of both is using a **set** to answer "is this message new?" — which is not a boolean
question. Zoom's chat panel shows *N* copies of a line and the avatar has answered *M* of
them; anything past *M* is new. A re-render changes neither N nor M and is invisible; a second
copy raises N and is answered exactly once.

`chatSeen` therefore holds a high-water mark per message rather than membership, and
`scanChat` counts occurrences in document order. The mark never decreases, which is what makes
virtualisation safe: messages scrolling out of the DOM lower N, and nothing is re-emitted when
they scroll back in.

Verified against the six cases that matter — backlog on open, repeated scans, a duplicate
pasted twice and three times, a list shrinking and regrowing, and a genuinely new message.

**The reasoning error is worth keeping.** §10.5 justified the trade as "a repeated identical
chat line is rare". It is not rare at all — repeating yourself is what a person does *when the
avatar did not answer*, so the suppression was concentrated precisely on the messages most in
need of a reply. A rarity argument about user behaviour deserves more suspicion than it got,
and the failure it produced was silent on both sides: nothing in the log said a message had
been dropped, because from the page's point of view nothing had happened.

## 12.1 …and the first message, which was a different bug

Reported alongside §12, and the log had said so plainly:

```
13:49:13  panelOpened     {panel: 'chat'}              ← panel opens, empty
13:51:07  observerArmed   {observer: 'chatArmed', existing: 1}   ← the user's 1st message
13:51:46  zoom_chat.received  chars=15                 ← the user's 3rd message
```

Three messages were sent and one was answered. **The first became the baseline.**

`armOnFirstSight` took its baseline on the first pass that found *content*. The panel opened
empty, so nothing armed; the observer then armed on the first thing it ever saw — a
participant's question — and recorded it as backlog. The second was identical and hit §12's
bug. Only the third, with different text, got through.

The mistake is a conflation. An empty result means one of two things:

* the panel is open and nobody has typed — **the baseline is empty**, and the next message
  is new;
* the panel is not rendered yet — **there is no baseline to take**, and nothing can be
  classified.

`armOnFirstSight` treated the first as the second, indefinitely. The container element tells
them apart: a chat list exists whether or not it holds children. So `panelReady` looks for the
container, `armWhenReady` takes the baseline the moment the panel is readable — empty or not —
and nothing is emitted *or* recorded before that point, because a message seen mid-render
cannot be classified either way.

`panel_ready_timeout_ms` (10 s) is the last resort if every container selector is renamed. It
arms on a timer, which risks reading a backlog aloud — the lesser failure of the two, and a
loud one rather than a silent one.

**Captions had the identical flaw and it would have been worse there.** When
`captions_auto_enable` is on, the avatar switches captions on itself, so the panel is *always*
empty at that instant and the first line transcribed is *always* new. Arming on first content
would have discarded it every single time.

Both rules were verified by replaying the reported sequence — panel unrendered, panel open and
empty, message, identical paste, different message — through the old rule and the new one.
