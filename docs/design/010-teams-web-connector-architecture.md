# 010 — Teams-web connector architecture

**Status:** built; **not yet verified against a live Teams meeting.** Every selector list in
`connectors/teams_web/automation/selectors.py` and the launcher/pre-join selectors in
`meeting/join.py` are candidates drawn from the hooks the Teams web client is known to use,
not measurements. §7 is the bring-up procedure that turns them into measurements.

Scope: the new `teams_web` connector, one enum member, one settings block, one container
registration. **Nothing in `zoom`, `zoom_web`, `google_meet` or `teams` changed** — the two
shared assertions that enumerate the platform set were updated, and that is the whole of the
diff outside the new package.

Read alongside doc 005 (the Graph-based Teams connector, which this does not replace) and doc
009 (Zoom-web browser ingest, whose findings this connector inherits wholesale).

---

## 1. The problem, which is not a technical one

Doc 005 built the Teams connector on Graph app-hosted media, and the reasoning in it is
sound: Microsoft publishes an API that carries per-participant audio with a source id, one
session in both directions, and no dependency on markup anybody can rename.

That argument has one premise, and the premise is not about Microsoft's API. **Graph
app-hosted media requires an Azure AD application with `Calls.JoinGroupCall.All` and
`Calls.AccessMedia.All` granted as *application* permissions and admin-consented in the
tenant that owns the meeting** — plus a Windows host running the .NET media SDK, because the
media platform exists nowhere else (doc 005 §1–§2).

In every deployment where the avatar joins a meeting somebody else booked, neither is
available. The tenant belongs to a customer, a candidate, a prospect; consent is a signature
from an administrator who has never heard of us. A Windows media host is merely expensive.

So a connector that is correct in every respect is unusable in the case it exists for. This is
the same shape as doc 009 §1, one step further along: Zoom's blocked path was a licensed SDK
download, Teams' is an administrator's signature.

**What this connector needs from the tenant: nothing.** It joins the ordinary web client the
way a person without a Teams account joins — the meeting link, a name typed into a form, and a
wait in the lobby.

## 2. Where each signal comes from

| Question | `teams` (doc 005) | `teams_web` (this doc) |
|---|---|---|
| Publish the avatar's audio | Graph media platform | synthetic `MediaStreamTrack`, patched `getUserMedia` |
| The meeting's audio | Graph, up to 4 unmixed speakers with a source id | one mixed stream tapped from the page's playout graph |
| Who is here / was here | Graph roster | the participants panel and the tile grid |
| Who is speaking | dominant-speaker events | the speaking ring Teams draws on a tile |
| What was said | — | live captions, **if switched on** |
| What was typed | — | the chat panel |
| Voice interruption | — | **audio energy**, plus the DOM speaker observer |
| Raised hand | — | the roster indicator and Teams' own announcement |
| Host requirement | Windows + .NET media SDK | Chromium |
| Tenant requirement | admin-consented app | none |

**There is one ingest mode, where `zoom_web` has two**, and that is not an omission. Zoom-web
can fall back to RTMS wherever a Zoom account will serve it. Teams' equivalent streams live
behind exactly the entitlement above, so there is nothing to select between — and no
`MC_TEAMS_WEB__INGEST_MODE`.

## 3. Why the audio tap patches three paths

Teams' web client carries meeting audio over WebRTC, so patching `RTCPeerConnection` and
reading inbound audio transceivers is expected to be the productive tap here — unlike on Zoom,
where doc 009 §3 measured that path to be *empty* because Zoom decodes audio in WebAssembly
off a WebSocket and exposes no inbound audio transceiver at all.

All three paths are patched anyway (`installWebAudioTap`, `installMediaElementTap`,
`installPeerConnectionTap`), and the reason is the one doc 009 paid for: the property that
holds across every transport a browser can use is that audio which is going to be *heard* must
reach either an `AudioContext`'s destination or a media element. Tapping at playout rather
than at the transport makes the connector indifferent to which transport Teams chose,
including to Teams changing its mind between releases.

Two consequences, both load-bearing elsewhere in the design:

* **One mixed stream, no attribution.** By playout the meeting has been mixed down to what a
  human would hear. Every frame arrives with `participant=None`, and no amount of work
  recovers the individual streams — they do not exist at that point in the graph. Who is
  talking comes from the DOM, on a separate path.
* **The avatar's own voice is structurally absent.** Teams does not play a participant their
  own microphone, and the microphone graph terminates at a `MediaStreamDestination` the tap
  never watches (`test_teams_web_js_assets.py` asserts that property directly). This is what
  §4 rests on.

## 4. Barge-in: the echo gate is switched off, deliberately

`EchoGuard` is built with `gate_enabled=False`.

The gate exists to stop the avatar hearing itself, which is a real problem on a leg carrying
the meeting's mix *with the avatar in it*. Doc 008 §4 records what happens without one on
Zoom's RTMS leg: the agent answering the tail of its own sentences, in a loop.

The page tap has no such loop (§3). And a shut gate would be actively harmful, because it
cannot tell the avatar's echo from somebody talking over it: it would withhold every inbound
frame during precisely the window a barge-in exists in, so an energy detector would be deaf
exactly when it matters. That is the failure doc 009 §4 describes on the other connector.

So the gate is off, `MediaRouter` gets a `SpeechDetector`, and **two triggers converge on one
handover**:

* **audio energy** — fast and anonymous. Fires on the first syllable.
* **the DOM speaker observer** — slower and *named*. Fires when Teams gets round to drawing
  the ring.

`TeamsInterruptSource`'s per-participant cooldown de-duplicates them, and
`_speaker_provider` names the interrupter from the tracker, falling back to elimination when
exactly one other person is in the meeting. What remains uncovered is the acoustic path — a
participant listening on speakers — which is a real echo no gate could have caught.

`MC_TEAMS_WEB__ECHO_GATE_HANGOVER_MS` is therefore read by nothing today. It is kept because
the value is part of the media pipeline's contract, and the field's docstring says so.

## 5. What this costs, stated plainly

Every observation is a rendering rather than an event. The consequences, none of which are
recoverable within this connector:

* **Two people sharing a display name are one person.** A DOM row carries no participant id,
  so `ParticipantEvent.user_id` is permanently `None` and the ledger keys on the folded name.
  The upside of the same choice: somebody whose wifi dropped and came back is *one* attendee.
* **Departures are approximate.** The roster is diffed at `observe_interval_ms`, and a
  disappearance is held for `roster_leave_grace_s` before it is believed — because Teams
  re-lays out its roster and tile grid constantly, and every believed flap re-pushes the
  meeting brief to the agent. Arrivals are believed at once; there is no layout in which Teams
  invents a participant.
* **The roster past one screenful is "whoever is rendered".** Teams virtualises both the panel
  and the grid. Correct for the small meetings this connector is aimed at, wrong for a town
  hall.
* **"What was said" needs captions on.** Reading the panel is invisible; *enabling* it is the
  avatar switching on a feature everybody in the meeting can see, which is why
  `captions_auto_enable` defaults to off. Without it the avatar can say who spoke and not what
  they said.
* **A Teams release can rename the hooks.** Concentrated in one file of data (§7), and every
  observer that finds nothing says what the page *does* contain.
* **Video is not published.** Accepted and counted (`video_dropped`), not sent. The audio path
  is what makes the avatar a participant rather than a picture.

## 6. What was reused, and what was copied

**Reused unchanged** — the shared pipeline did not learn that a fifth connector exists:
`MediaRouter`, `Pacer`, `DecodePipeline`, `FfmpegDecoder`, `IdleFrameSource`, `EchoGuard`,
`SpeechDetector`, `MediaClock`, `AvatarClient`, `WebSocketAvatarTransport`, `SessionRegistry`,
`SessionLifecycle`, `SessionSupervisor`, `MeetingService`, and every HTTP endpoint.

**Borrowed across a connector boundary**, with the same recorded debt `zoom_web` carries:
`google_meet.automation.driver.BrowserDriver` / `PlaywrightDriver` and
`google_meet.browser.launcher.build_launch_plan`. Chromium is not a Meet concept and moving
these into shared code is the better end state; the refactor touches ~10 files across two
connectors that are in production, and the brief for this change was not to disturb them.
`tests/architecture/test_layering.py` enumerates the connectors it holds to strict
independence (`zoom`, `teams`, `google_meet`) and neither browser connector is on that list —
which is exactly the debt, made visible.

**Copied rather than shared**, and each for a stated reason:

| What | Why not shared |
|---|---|
| The page codec (`page/protocol.py`, magic `TWB1`) | An asset a page loads is part of that connector's wire contract; two connectors that must change their page independently cannot share one. Different magic so a captured frame says which connector produced it. |
| The worklets | Same reason. `playout_worklet.js` is byte-for-byte the Zoom-web one; the capture processor is renamed `mc-teams-capture` so a stale asset is diagnosable from a log. |
| The observation types (`observations.py`) | Zoom's `user_id` is a real participant id from an event stream; here it is permanently `None`. Sharing the type would hide that, and would couple two connectors' release cycles. |
| The ledgers (`attendance`, `active_speaker`, `transcript`, `chat`, `hand_raise`, `announcer`) | Their health component names appear in operator-facing output; a `zoom_web_chat` component inside a Teams session is a bug report waiting to happen. The docstrings also differ substantively — most of what those files *explain* is which signal they are reading and how much it can be trusted. |
| The join-link test (`looks_like_join_url`) | `teams/graph/join_url.py` goes on to produce a Graph descriptor, which is precisely the thing this connector exists not to need. |

The ledgers are the largest duplication and the one worth revisiting: if a sixth
browser-driven connector arrives, the case for extracting a generic
`DomObservedMeetingLedgers` package becomes strong. With two it would be an abstraction
serving no third caller, which doc 003 §0 rules out.

## 7. Bring-up: turning the selector candidates into measurements

The selector lists are the only part of this connector that has not been exercised against
the real thing. They are **data**, injected into the page as configuration, so correcting one
is an edit to `automation/selectors.py` (or `meeting/join.py`) and a restart — not an asset
change.

Every observer fails by *finding nothing*, and finding nothing is exactly what a quiet meeting
looks like. So the page reports what it can see, and the procedure is to read those reports:

1. `MC_TEAMS_WEB__ENABLED=true`, `MC_TEAMS_WEB__HEADLESS=false`, and join a meeting you own.
2. Watch for `teams_web.page_event` lines:
   * `audioTapped` — **check this first.** Its absence means the avatar is deaf, and the
     `how` field says which of the three tap paths fired. `PageAudioSource.health` reports
     `tapped=0` for the same fact.
   * `handsArmed`, `participantsPanelOpened`, `panelOpened`, `observerArmed` — the observers
     starting up.
   * `handsIdle` — carries `rows`, `handLabels` and a `sample` of what a participant row
     actually contains (`data-tid` hooks and icon references, never the row's text — a
     diagnostic should not be a copy of somebody's name).
   * `observerIdle` — carries per-selector match `counts` and a `tokens` sweep of what Teams is
     really calling things. **`tokens` is the field to read**, and `data-tid` entries are the
     ones worth writing a selector against: Fluent's class names are build hashes.
3. For the active speaker, read the **`churn`** field of an `observerIdle` naming `speaker`. A
   state marker is by definition a hook that *toggles*; `churn` lists what appeared and
   disappeared between scans, so layout containers drop out for free. Doc 009 lost two live
   runs to sampling snapshots at moments when nobody was talking, which is why this exists.
4. Correct the list that is wrong, restart, repeat.

The one failure that is *not* silent is the join: `TeamsWebJoinTimeoutError` names what it
reached (`in_meeting=…`, and whether it sat in the lobby), and `TeamsWebJoinTargetError` fires
before the browser goes anywhere when the request says which meeting to join in neither of the
two accepted ways.

## 8. Joining

One poll loop over the whole sequence, because the steps are optional and Teams has more of
them than Zoom: a launcher page for a link and not for the join form, a name field for a guest
and not for a signed-in profile, a lobby when the organiser left it on.

Two routes in, both served by the same loop:

* **a join link** in `platform_data["meeting_url"]` — or in `meeting_number`, which is a
  natural operator mistake worth accepting, since `POST /sessions` has a `meeting_number` for
  every platform and a Teams invite gives you a link;
* **a meeting id and passcode**, filled into the web client's own join form at
  `MC_TEAMS_WEB__JOIN_URL_TEMPLATE`.

A link wins when both are present: it identifies the meeting exactly, passcode included for
the short forms.

**Three link shapes are recognised, and missing the third cost a live run.** The classic
`/l/meetup-join/…`, the work/school short form `teams.microsoft.com/meet/<id>?p=…`, and the
personal ("Teams for Life") `teams.live.com/meet/<id>?p=…`. The first implementation matched
`meetup-join` alone, so a `teams.live.com` link failed the test, fell through to the id route,
and navigated the work/school join form — which for a signed-in personal account redirects to
the Teams app home. No form, nothing to fill, and a timeout with no selector at fault.

**A bare meeting id does not say which Teams it belongs to**, and both are 9-13 digits.
Guessing from the shape of the id or the passcode is the kind of heuristic that works until it
silently does not, so the joiner tries `join_url_template` and — if the page responds to
*nothing* it does for `ROUTE_FALLBACK_POLLS` polls — navigates `live_url_template` for the same
id once, logging `teams_web.route_fallback`. That is a fact about the page rather than an
inference about the string. The gate is *no progress at all* rather than a timer, because a
lobby is also a page where nothing happens for minutes — and a lobby is reached by clicking
Join, so it can never be idle by that definition.

**The trap here is the mute state, not audio joining.** Zoom creates no audio path until
asked, which is the failure that looks like success on that connector. Teams negotiates audio
as part of the join — but it carries the *pre-join* microphone toggle into the call, and a
persistent profile remembers it across sessions. So `_ensure_unmuted` runs inside the loop as
well as after admission, and a join that could not clear it returns `unmuted=False` rather
than failing: the session is otherwise fine and an organiser can unmute it.

**Leaving is confirmed, not assumed.** Closing the browser drops the socket and Teams keeps
the participant until its own timeout, so the avatar's tile stays visible long after
`DELETE /sessions/{id}` returned success. The joiner clicks hang-up, clicks any confirmation
it opens, and waits for the in-meeting controls to disappear. "End meeting" is deliberately
absent from the confirm list — ending somebody else's meeting is not a thing the avatar may do
by accident.

## 8a. Two things the page must not do to its own session

Both were found in the first live run, and both are the kind of failure where the session
reports healthy throughout.

**It must not be invisible to Teams' device menu.** Teams enumerates audio inputs to populate
that menu; finding nothing selectable it reports *"Mic disconnected"* in the call and publishes
nothing, while the patched `getUserMedia` sits there working perfectly. So the page appends a
fake `audioinput` to the real device list.

Appends, rather than replacing — which is where this differs from the Google Meet bridge, and
the reason is the tap's position. That bridge returns three fixed fakes including an
`audiooutput`, and can afford to: it reads inbound WebRTC transceivers, so nothing it needs
depends on audio being *played*. This connector taps at **playout**. Told the only available
output is a device that does not exist, Teams could route the meeting to a sink that renders
nothing, and the tap would go silent for a reason indistinguishable from a broken selector. No
fake `videoinput` either: the avatar publishes no video, and advertising a camera means Teams
offers one that produces a grey rectangle.

**It must not click Teams' app navigation.** The left-hand app rail has "Chat" and "People"
buttons, and an unscoped `button[aria-label*='people' i]` matches one. The panel observer
clicked it: the single-page app navigated to the contacts page, the meeting kept running
behind it, and every observer was left reading a page with no meeting in it.

Three guards, in order of how much they can be trusted:

1. **Toolbar-scoped selectors.** `participants_panel_button` and `chat_panel_button` now match
   `data-tid` hooks or labels *inside* `[data-tid='calling-toolbar']` / `[role='toolbar']`.
2. **An app-rail exclusion in the page** (`TeamsObserverSelectors.app_rail`). A candidate whose
   `closest()` matches app navigation is skipped and reported as
   `panelSelectorHitAppRail`. An exclusion rather than a tighter positive match, because which
   container holds the real toolbar is a guess that changes between builds while app
   navigation being off-limits is permanent.
3. **A navigation guard.** Nothing is clicked before a meeting marker is present, and if the
   markers disappear after one has been seen, the page reports `meetingLost` and calls
   `history.back()` — bounded to two attempts, because a page that never joined must not spend
   its session pressing Back. Two presses of Back restored the meeting in the live run, which
   is why recovery is attempted rather than only reported.

The page and the joiner share one definition of *in a meeting*: `meetingMarkerSelectors` is
`TeamsWebSelectors.in_meeting_markers`, sent through the bootstrap, so the two cannot disagree
about whether there is a call.

## 8b. The channel reconnects, and one diagnostic that does not use it

**The page socket is retried for the life of the page**, with an exponential backoff capped at
five seconds. That is not defensive padding; the first live run failed on exactly this. Joining
walks through a launcher, a pre-join screen and several short-lived frames, and somewhere in
that churn the socket closed. The script stayed alive — it had already reported `handsArmed` —
with `state.socket === null` and nothing to call `connect()` again.

What makes that failure expensive is its *invisibility*. The avatar publishes into a socket
nobody holds, tapped frames are dropped before they are framed, and **every diagnostic the page
would send travels over the same socket** — including the ones that exist to explain this. The
whole of the evidence was one line: `first_audio_published attached_pages=0`.

**And the retry cannot be event-driven alone**, which the next run showed. The probe reported
`socket_state=3` (CLOSED) with `connects=0`, `closes=0` and no constructor error: the socket had
failed *before* `onopen`/`onclose` were attached, so neither ever ran and the retry they
schedule was never armed. Chromium is entitled to fail a refused WebSocket synchronously, and
nothing promises a close event on a connection that never started. So `ensureConnected()`
inspects `readyState` on a one-second timer of its own — the handlers stay as the fast path, and
the poll is the one that cannot be skipped. A socket found already dead is counted as
`staleSockets`, which is how "the handlers never ran" is told apart from "the channel keeps
dropping".

Two consequences in the design:

* That line is now a **warning** when nothing is attached, because it is a fault rather than a
  milestone.
* `TeamsWebSession._probe_page` reads `window.__mcTeamsMic` through the driver after the join —
  a path that does not depend on the channel working. It reports `page_script_not_running` (no
  script at all), `page_channel_down` (running, not attached, with the connect/close counts and
  any construction error), or `page_probe` with the counters. Bounded delay rather than bounded
  attempts on the retry, because a page that has lost its socket has nothing else to do and the
  session may outlive any attempt count worth naming.

The probe reads the **main frame only**, which is the honest limit: the script keeps state per
frame, so if Teams ever renders the meeting inside an iframe this reports the wrapper's view.
Even then, "no script" versus "socket in a bad state" is a real distinction, which is more than
silence offers.

## 8c. Teams' CSP, and why the failure was invisible

**The page's Content Security Policy was closing the channel**, and `bypass_csp=True` on the
browser context is the fix.

A CSP `connect-src` directive governs **WebSockets**, not only `fetch`. Chromium's behaviour
when it blocks one is the least helpful available: the constructor returns a socket object
already in `CLOSED`, throws nothing, and fires neither `error` nor `close`. The page therefore
cannot tell that it was refused, and neither can the bridge — and because every page diagnostic
travels over that same socket, the refusal silences its own explanation.

The measured signature, from `_probe_page`: `socket_state=3`, `connects=0`, `closes=0`,
`error=None`, `stale_sockets=58`. Fifty-eight retries in twenty-four seconds, every one failing
instantly and silently, on an origin whose *launcher* pages had connected to the same loopback
port perfectly moments earlier. That last detail is what points at CSP rather than at Local
Network Access: an LNA block would have refused every page on the origin, not just the one
serving the meeting.

It took three attempts to find, and the first two are worth recording because each was a real
bug that was not *this* bug: the socket did not reconnect at all (§8b), and then the reconnect
was event-driven where no events fire. Both are fixed and both were necessary; neither was
sufficient.

**Why disabling CSP here is a narrow cost.** The same argument `LOCAL_NETWORK_ACCESS_NOTE`
makes for the sibling flag: the browser is ours, headless, and visits one site; the channel is
loopback-bound and carries a per-session `secrets.token_urlsafe` token compared with
`compare_digest`; and nothing on that wire is a credential. It is nonetheless a page-hardening
feature switched off, which is why it is `LaunchPlan.bypass_csp` — a field defaulting to
`False`, omitted from `to_playwright_kwargs` entirely when unset, so Meet and Zoom-web launch
byte-for-byte as they did before it existed. `MC_TEAMS_WEB__BYPASS_CSP=false` turns it off, and
turning it off is the better end state the day Teams stops blocking the channel.

## 9. Ordering and lifecycle

**One ordering cannot be rearranged**: bind the page socket → inject the script → navigate.
The script patches `getUserMedia` and installs the audio tap, and Teams calls the first on its
pre-join screen and builds the graph the second watches while joining. A patch installed
afterwards sees neither, and the symptom is a session that joins, reports healthy, and carries
silence.

Note the contrast with `zoom_web`, whose ordering constraint is about the *device*: Zoom picks
its capture device while joining, so a microphone that appears afterwards is not the one
selected. Teams has no such requirement, which is also why a profile is optional here and
mandatory there.

**The legs do not recover independently, and health says so.** They cannot: ingest and egress
are one socket into one page. Reporting an independence this connector does not have would
mislead whoever is reading a health report at three in the morning.

**Teardown is guarded step by step and the browser closes last and always.** Doc 009's
connector shipped the unguarded version: a failed ingest `stop` aborted teardown before the
browser closed, `DELETE /sessions/{id}` returned success, and the avatar stayed in the
meeting. Closing the browser is what actually removes the participant, so nothing earlier is
allowed to prevent it.

## 10. What is verified, and how

`tests/unit/test_teams_web_*.py`, all running with no Chromium and no Teams tenant:

| File | What it pins |
|---|---|
| `test_teams_web_js_assets.py` | The audio header, implemented twice in two languages, byte for byte. Page/Python config-key parity in both directions — a key the page reads under a name Python does not send is a *silently disabled feature*. The tap's three paths, the capture graph's termination, and the microphone never reaching the tap. The observer shapes that were live bugs elsewhere: the chat high-water mark, the panel-readiness baseline, the lower event, and the ban on a bare `hand` token. |
| `test_teams_web_page_channel.py` | The codec and the real socket: token rejection before the handshake, ephemeral port, binary-vs-text routing, broadcast to every attached frame, a handler that raises not dropping the socket, and a Zoom-web frame being refused. |
| `test_teams_web_join.py` | Both routes, the launcher click, id normalisation, the lobby being waited out, refusal being fatal, the mute trap, and leave confirmation. |
| `test_teams_web_observer.py` | Levels becoming edges: roster diffing, an empty roster being ignored, the departure grace window, an unmoved hand being one interruption, elimination failing closed at two, and the transcript seeing chat before the mention filter. |
| `test_teams_web_media_legs.py` | Frames stamped from *our* media clock, the format asserted rather than converted, silence reported as degraded rather than unhealthy, and ingest `stop` not taking the shared page server down. |
| `test_teams_web_config.py` | The consumer folds, and that no Graph credential is required to build or register. |
| `test_teams_web_connector.py` | Wiring per configuration, the injection-before-navigation order, the browser closing despite a failing teardown step, and registration independence from `settings.teams`. |

**Not verified:** everything that depends on Teams' actual markup (§7), and the tap firing
against a real Teams playout graph.

## 11. Open questions

1. **Captions behind the "More" menu.** In several Teams builds the captions control is not on
   the toolbar. Reaching it means opening a menu, which is a second visible action, and the
   connector does not take one uninvited. If `captions_auto_enable` proves unusable in
   practice, the options are a two-step opener or accepting that the transcript only works
   where somebody else switched captions on.
2. **Roster beyond one screenful.** §5. If town-hall-sized meetings matter, the participants
   panel needs scrolling — which is a visible action and a pagination problem, not a selector
   fix.
3. **Publishing video.** A canvas track plus Teams' camera controls. The audio path is what
   makes the avatar a participant; this is a separate milestone.
4. **Extracting the DOM-observed ledgers.** §6. Worth doing at three browser connectors, not
   at two.
