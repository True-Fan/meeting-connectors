/*
 * The avatar's microphone, inside Zoom's web client — and the one thing the page can
 * see that Zoom's API does not report.
 *
 * The microphone uses the same technique the Google Meet connector does: PCM arrives
 * over a loopback WebSocket, an AudioWorklet turns it into a real MediaStreamTrack,
 * and a patched `getUserMedia` hands that track to the page instead of a physical
 * device. No OS audio device, nothing to install.
 *
 * WHY THIS NEEDS A SIGNED-IN, PERSISTENT PROFILE
 * ----------------------------------------------
 * An earlier attempt injected the track into a throwaway profile and Zoom published
 * nothing: its device menu showed no microphone selected at all, so its capture
 * pipeline never started, whatever `getUserMedia` returned. Chromium persists the
 * per-origin microphone choice in the profile (`Default/Preferences`), so a profile
 * where a microphone has been chosen once makes Zoom request that specific
 * `deviceId` — and this patch answers it. That is why the connector uses a
 * persistent profile rather than a fresh directory per session.
 *
 * WHY THERE IS A DOM OBSERVER HERE AT ALL
 * ---------------------------------------
 * Almost everything the Google Meet connector scrapes out of a page, Zoom hands over
 * as data: RTMS reports who joined, who left, who is speaking, what each person said
 * and what they typed, each with a name attached. So this script does *not* read the
 * roster, the chat panel or the captions, and adding that would be paying Meet's
 * price for a problem Zoom does not have.
 *
 * A raised hand is the exception, and it is a real gap rather than an oversight:
 * RTMS's event list has no hand-raise event in it. The indicator exists only on
 * screen, so the only thing that can see it is something inside the page — which is
 * what the rest of this file is. It is still far smaller than Meet's `bridge.js`,
 * because it is looking for exactly one thing.
 *
 * Nothing here decides what a raised hand *means*. The page reports the edge; Python
 * decides whether to interrupt, whose hand it was, and what the agent is told — the
 * same split the Meet connector draws, for the same reason.
 */

(() => {
  const CONFIG = window.__mcZoomConfig || {};
  const ENDPOINT = CONFIG.endpoint;
  const SAMPLE_RATE = CONFIG.sampleRateHz || 16000;
  const WORKLET_SOURCE = CONFIG.workletSource;
  if (!ENDPOINT || !WORKLET_SOURCE) return;

  const state = {
    context: null,
    node: null,
    micTrack: null,
    building: null,
    socket: null,
    frames: 0,
    // -- hand-raise observation -------------------------------------------
    handsTimer: null,
    // Keys whose hand is currently up. Entering this set is what gets reported.
    handsUp: new Set(),
    // Key -> when it was last *seen* up. A hand comes down when it has not been seen
    // for a grace window, not when one scan missed it — see `scanHands`.
    handsSeenAt: new Map(),
    handsLastSentAt: new Map(),
    handsBaselineUntil: 0,
    handsArmed: false,
    handsSent: 0,
    handsDiagnostics: 0,
    handsLastDiagAt: 0,
    panelOpened: false,
  };
  window.__mcZoomMic = state;

  // -- the synthetic microphone --------------------------------------------

  async function build() {
    if (state.context) return;
    const Ctx = window.AudioContext || window.webkitAudioContext;
    const context = new Ctx({ sampleRate: SAMPLE_RATE });

    // The worklet source travels as a string and is wrapped in a blob URL: there
    // is no HTTP origin of ours to fetch it from — the page's origin is Zoom's.
    const blob = new Blob([WORKLET_SOURCE], { type: 'application/javascript' });
    const url = URL.createObjectURL(blob);
    try {
      await context.audioWorklet.addModule(url);
    } finally {
      URL.revokeObjectURL(url);
    }

    const node = new AudioWorkletNode(context, 'mc-playout', {
      numberOfInputs: 0,
      numberOfOutputs: 1,
      outputChannelCount: [1],
      processorOptions: {
        capacitySamples: Math.round(SAMPLE_RATE * (CONFIG.bufferSeconds || 1.0)),
        targetSamples: Math.round(SAMPLE_RATE * (CONFIG.targetSeconds || 0.2)),
        trimBlockSamples: Math.max(Math.round(SAMPLE_RATE * 0.005), 1),
        reportEverySamples: SAMPLE_RATE,
      },
    });

    const destination = context.createMediaStreamDestination();
    node.connect(destination);

    // Chromium can start an AudioContext suspended. The launch flags disable that
    // policy; this is the belt to those braces, because a suspended context renders
    // nothing and the track would be permanently silent with no error anywhere.
    if (context.state === 'suspended') {
      await context.resume();
    }

    state.context = context;
    state.node = node;
    state.micTrack = destination.stream.getAudioTracks()[0];
  }

  function ensureBuilt() {
    if (!state.building) {
      state.building = build().catch((err) => {
        state.building = null;
        throw err;
      });
    }
    return state.building;
  }

  // -- transport ------------------------------------------------------------

  const HEADER_BYTES = 20;

  function connect() {
    if (state.socket) return;
    let socket;
    try {
      socket = new WebSocket(ENDPOINT);
    } catch (err) {
      return;
    }
    socket.binaryType = 'arraybuffer';
    socket.onmessage = async (event) => {
      const buffer = event.data;
      if (!(buffer instanceof ArrayBuffer) || buffer.byteLength <= HEADER_BYTES) return;
      await ensureBuilt();
      if (!state.node) return;
      // int16 PCM follows the fixed header; the worklet owns the ring buffer.
      // Copy first, then transfer the copy's buffer.
      //
      // Posting the view over `buffer` and listing `buffer.slice(0)` as the
      // transfer — which this did — transfers an unrelated copy while cloning the
      // view, and a transfer entry unreachable from the message is the kind of
      // thing engines are entitled to reject outright. The frame then never
      // reaches the worklet and the avatar is silent with nothing logged.
      const pcm = new Int16Array(buffer, HEADER_BYTES);
      const copy = pcm.slice(0);
      state.frames += 1;
      state.node.port.postMessage(copy, [copy.buffer]);
    };
    socket.onclose = () => {
      state.socket = null;
    };
    socket.onerror = () => {};
    state.socket = socket;
  }

  /*
   * Send one JSON event back to the bridge.
   *
   * Text frames, where audio is binary: the transport tells the two apart, so nothing
   * needs a discriminator (`page/protocol.py`). Silent on failure — a socket that has
   * gone away is the session ending, and throwing out of a DOM observer would only
   * take the scan loop with it.
   */
  function send(payload) {
    const socket = state.socket;
    if (!socket || socket.readyState !== 1) return false;
    try {
      socket.send(JSON.stringify(payload));
      return true;
    } catch (err) {
      return false;
    }
  }

  /*
   * Diagnostics, bounded.
   *
   * This feature's failure mode is *silence*: a selector that no longer matches looks
   * exactly like a meeting where nobody raised a hand. These are what tell the two
   * apart, and they are capped because an observer that logs on every scan is a second
   * problem rather than a diagnosis for the first.
   */
  function report(name, detail) {
    send({ type: 'pageEvent', name: name, detail: detail || {} });
  }

  // -- device patches -------------------------------------------------------

  const media = navigator.mediaDevices;
  if (media && media.getUserMedia) {
    const original = media.getUserMedia.bind(media);
    media.getUserMedia = async (constraints) => {
      const wants = constraints || {};
      if (wants.audio) {
        await ensureBuilt();
        if (state.micTrack) {
          const stream = new MediaStream();
          // A fresh clone per call: Zoom may stop the track it is handed, and a
          // stopped original would silence every later request.
          stream.addTrack(state.micTrack.clone());
          return stream;
        }
      }
      return original(constraints);
    };
  }

  // Permission queries must answer "granted", or Zoom shows a prompt that cannot
  // be clicked in a headless browser.
  if (navigator.permissions && navigator.permissions.query) {
    const originalQuery = navigator.permissions.query.bind(navigator.permissions);
    navigator.permissions.query = (desc) => {
      const name = desc && desc.name;
      if (name === 'microphone' || name === 'camera') {
        return Promise.resolve({
          state: 'granted',
          onchange: null,
          addEventListener() {},
          removeEventListener() {},
        });
      }
      return originalQuery(desc);
    };
  }

  // -- raised hands ---------------------------------------------------------

  /*
   * Phrases that mean somebody put their hand up.
   *
   * Matched against labels and text rather than against class names, for the reason
   * Meet's observer settled on the same thing: class names and `data-*` attributes are
   * build artefacts that change without notice, and the sentence a product shows a
   * human is the most durable thing on the page. Zoom writes it into the participant
   * row's `aria-label` and its tooltip.
   */
  const HAND_TRIGGERS = [
    'raised their hand',
    'raised your hand',
    'raised a hand',
    'has their hand raised',
    'hand is raised',
    'raised hand',
    'hand raised',
    'raise_hand',
  ];

  /*
   * Text containing a trigger phrase that is not somebody raising a hand.
   *
   * Each is a control the avatar's own client renders continuously, and each would
   * otherwise fire on every single scan: "Raise Hand" is the reactions-menu item,
   * "Lower Hand" is what it becomes once pressed, and "Lower All Hands" is the host
   * control. Checked first, and by substring, because Zoom appends shortcut hints
   * inside the same label.
   */
  const HAND_EXCLUDE = [
    'raise hand',
    'lower hand',
    'lower all hands',
    'lower all',
    'raised hands',
    'raise your hand',
  ];

  const SELF_WORDS = ['you', 'me', '(me)', 'you (me)', 'your hand'];

  function handExcluded(lowered) {
    for (const phrase of HAND_EXCLUDE) {
      if (lowered.indexOf(phrase) !== -1) return true;
    }
    return false;
  }

  function handMatch(text) {
    if (!text) return null;
    const lowered = text.toLowerCase();
    if (handExcluded(lowered)) return null;
    for (const phrase of HAND_TRIGGERS) {
      const at = lowered.indexOf(phrase);
      if (at !== -1) return { phrase: phrase, at: at };
    }
    return null;
  }

  /* The name in "Dev Choudhary, raised hand" — the label with the phrase taken out. */
  function handName(text, match) {
    if (!text || !match) return null;
    const name = (text.slice(0, match.at) + ' ' + text.slice(match.at + match.phrase.length))
      .replace(/[(),.;:•\-–—]/g, ' ')
      .replace(/\s+/g, ' ')
      .trim();
    return name ? name.slice(0, 120) : null;
  }

  /*
   * The participant row an indicator sits inside, so an icon with no text of its own
   * still resolves to a person.
   *
   * Zoom's participants panel renders one row per person; the hand is an icon in that
   * row and the name is a sibling of it. Walking up to the row is what joins them.
   */
  function handRow(node) {
    const selectors = CONFIG.handRowSelectors || [];
    for (const selector of selectors) {
      try {
        const row = node.closest ? node.closest(selector) : null;
        if (row) return row;
      } catch (err) {
        /* an unparseable selector is a config problem, not a reason to stop */
      }
    }
    return null;
  }

  function handRowName(row) {
    if (!row) return null;
    const selectors = CONFIG.handNameSelectors || [];
    for (const selector of selectors) {
      try {
        const el = row.querySelector(selector);
        const text = el ? (el.getAttribute('title') || el.textContent || '') : '';
        const name = text.split('\n')[0].trim();
        if (name && !handMatch(name)) return name.slice(0, 120);
      } catch (err) {
        /* as above */
      }
    }
    const label = (row.getAttribute('aria-label') || row.textContent || '')
      .split('\n')[0]
      .trim();
    const match = handMatch(label);
    const name = match ? handName(label, match) : label;
    return name ? name.slice(0, 120) : null;
  }

  /*
   * One signal, resolved to the participant it is about.
   *
   * Returns null when nothing names anybody. **Skipped rather than keyed as
   * "somebody"**, which is the lesson Meet's observer paid for: an unattributable
   * trigger reappears on every scan, so keying it would interrupt the avatar forever.
   * A missed hand is a far cheaper mistake than that.
   */
  function handResolve(node, name, how, allowAnonymous) {
    const row = handRow(node);
    if (!name && row) name = handRowName(row);

    if (!name) {
      /*
       * **An unnamed hand is now reported rather than dropped, and only from the markup
       * pass.** The tile that carries the indicator is often the one showing video, and it
       * renders `video-avatar__avatar-img` where a camera-off tile renders
       * `video-avatar__avatar-name` — so the person whose hand is up is exactly the person
       * whose name the tile does not spell out. Dropping it would mean the detector works
       * and the feature still does nothing.
       *
       * Safe here where it would not have been before, for two reasons. `handsUp` keys on
       * the string below, so one anonymous hand is one edge and not one per scan — the
       * failure the label passes still guard against. And Python now holds the RTMS roster,
       * so it can put a name on this by elimination, which it could not when this file was
       * the only thing that knew who was in the meeting.
       */
      if (!allowAnonymous) return null;
      return { key: 'anonymous', name: null, isSelf: false, how: how };
    }

    const lowered = name.toLowerCase();
    const isSelf =
      SELF_WORDS.indexOf(lowered) !== -1 ||
      (!!CONFIG.displayName && lowered === String(CONFIG.displayName).toLowerCase());
    return { key: 'name:' + lowered, name: name, isSelf: isSelf, how: how };
  }

  /*
   * Everything on the page that currently looks like a raised hand.
   *
   * Two passes, because Zoom expresses the same state two ways and neither is reliable
   * alone. The selector pass reads the indicator elements the participants panel
   * renders — precise, and dependent on class names Zoom may rename. The text pass
   * walks labels and tooltips for a trigger phrase — slower, and durable against a
   * rename. Running both means a Zoom release has to break both before the feature
   * goes quiet, and the diagnostics say which one is finding things.
   */
  /*
   * Markup tokens that mean "this row has a raised hand", matched against the row's own
   * HTML rather than against a label.
   *
   * **This is the pass that had to exist, and the live run is what proved it.** A meeting
   * with a hand up reported `rows: 2` and `handLabels: []` — the participant rows were
   * found perfectly and there was not one string anywhere on the page containing the word
   * "hand". Zoom's web client puts "Raise Hand" inside the Reactions menu rather than on
   * the toolbar, and marks the raised hand on the row with an icon whose only trace is a
   * class or an SVG reference. So there is nothing for a label sweep or an `aria-label`
   * selector to find, and both of the earlier passes were looking for something that is
   * not there.
   *
   * **Compound tokens only, never a bare "hand", and that is a correctness requirement
   * rather than tidiness.** The row's HTML contains the participant's *name*, and plenty
   * of real names contain those four letters — "Chandra", "Handa", "Chand". Matching
   * `hand` would raise a permanent false hand for anybody so named, interrupting the
   * avatar every cooldown for the whole meeting. None of the tokens below can occur
   * inside a name.
   */
  const HAND_MARKUP = [
    // **The one Zoom actually renders**, captured from a live meeting: a raised hand adds
    // `lazy-icon-nvf/270b` to the participant's tile and removes it when the hand goes
    // down. `nvf` is Zoom's abbreviation for nonverbal feedback and `270b` is the Unicode
    // codepoint of ✋ (U+270B RAISED HAND) — so the class names the gesture exactly.
    //
    // Matched with the `nvf` prefix rather than as a bare `270b`, which could plausibly
    // turn up inside a build hash or an element id and would then raise a permanent
    // false hand.
    'nvf/270b',
    'nvf-270b',
    'nvf_270b',
    // The other spellings Zoom has used elsewhere for the same state. They cost nothing
    // to check and are what a different Zoom build or the participants-panel rendering
    // may use instead of the tile's reaction bubble.
    'raisehand',
    'raise-hand',
    'raise_hand',
    'handraise',
    'hand-raise',
    'hand_raise',
    'nonverbal',
  ];

  /*
   * Whether a participant row's markup says its owner has a hand up.
   *
   * Reads `innerHTML`, which catches the class name, the SVG `href`, the icon ligature and
   * any data attribute at once — so it keeps working when Zoom renames one of them, which
   * a selector per shape does not.
   */
  function handMarkupMatch(row) {
    if (!row) return false;
    let html = '';
    try {
      html = (row.innerHTML || '').toLowerCase();
    } catch (err) {
      return false;
    }
    if (!html) return false;
    for (const token of HAND_MARKUP) {
      if (html.indexOf(token) !== -1) return true;
    }
    return false;
  }

  function handCandidates() {
    const found = new Map();

    // The row-markup pass first, because it is the one that works against the DOM Zoom
    // actually renders — see HAND_MARKUP. The selector and label passes below stay as
    // they are: they cost nothing when they match nothing, and a Zoom build that does
    // expose a label should not need a code change to be understood.
    for (const selector of CONFIG.handRowSelectors || []) {
      let rows = [];
      try {
        rows = document.querySelectorAll(selector);
      } catch (err) {
        continue;
      }
      for (const row of rows) {
        if (!handMarkupMatch(row)) continue;
        const candidate = handResolve(row, handRowName(row), 'markup', true);
        if (candidate && !found.has(candidate.key)) found.set(candidate.key, candidate);
      }
    }

    for (const selector of CONFIG.handSelectors || []) {
      let nodes = [];
      try {
        nodes = document.querySelectorAll(selector);
      } catch (err) {
        continue;
      }
      for (const node of nodes) {
        const candidate = handResolve(node, null, 'selector');
        if (candidate && !found.has(candidate.key)) found.set(candidate.key, candidate);
      }
    }

    // Bounded, because an unbounded sweep of a page Zoom is still building shows up as
    // dropped audio frames rather than as an error. `[aria-label]` and `[title]` are
    // where Zoom puts the sentence; reading attributes forces no layout.
    let labelled = [];
    try {
      labelled = document.querySelectorAll('[aria-label],[title]');
    } catch (err) {
      labelled = [];
    }
    const limit = CONFIG.handScanLimit || 4000;
    let seen = 0;
    for (const node of labelled) {
      if (seen++ > limit) break;
      const text = node.getAttribute('aria-label') || node.getAttribute('title') || '';
      const match = handMatch(text);
      if (!match) continue;
      const candidate = handResolve(node, handName(text, match), 'label');
      if (candidate && !found.has(candidate.key)) found.set(candidate.key, candidate);
    }

    return Array.from(found.values());
  }

  /*
   * Open the participants panel once, so there are rows to read.
   *
   * **The indicator does not exist in a DOM nobody opened.** With the panel closed Zoom
   * renders a raised hand as a transient toast and, on some layouts, nothing at all —
   * so the observer would be correct, running, and permanently blind. This is the one
   * visible action the connector takes inside the meeting, which is why it is a switch
   * (`MC_ZOOM_WEB__HAND_RAISE_OPEN_PANEL`) rather than unconditional.
   *
   * Once, not on every scan: clicking a toggle repeatedly would close it again on the
   * next pass, which is a slow way to make the panel flicker for everybody watching the
   * avatar's screen share.
   */
  function openParticipantsPanel() {
    if (state.panelOpened || !CONFIG.handOpenPanel) return;
    for (const selector of CONFIG.participantsPanelSelectors || []) {
      let el = null;
      try {
        el = document.querySelector(selector);
      } catch (err) {
        continue;
      }
      if (!el) continue;
      state.panelOpened = true;
      try {
        el.click();
      } catch (err) {
        /* a click that throws is a control that was not one; nothing to recover */
      }
      report('participantsPanelOpened', { selector: selector });
      return;
    }
  }

  function scanHands() {
    if (!CONFIG.handRaiseEnabled) return;

    if (!state.handsArmed) {
      state.handsArmed = true;
      // A baseline window, because a hand that was already up when the avatar joined is
      // not an interruption of anything the avatar was saying. Recorded as up, reported
      // to nobody.
      state.handsBaselineUntil = Date.now() + (CONFIG.handBaselineMs || 4000);
      report('handsArmed', {
        selectors: (CONFIG.handSelectors || []).length,
        openPanel: !!CONFIG.handOpenPanel,
      });
    }

    openParticipantsPanel();

    const now = Date.now();
    const baselining = now < state.handsBaselineUntil;
    const cooldownMs = CONFIG.handCooldownMs || 0;
    const candidates = handCandidates();

    for (const candidate of candidates) {
      // Written for a hand that is merely still up as much as for one that has just gone
      // up: this is the timestamp the retirement pass below reads.
      state.handsSeenAt.set(candidate.key, now);
      if (state.handsUp.has(candidate.key)) continue;

      // Recorded as up *before* the baseline and cooldown gates rather than after, so a
      // hand that is up but deliberately not reported still counts as up. Otherwise the
      // next scan reads it as a fresh raise and the gate it just failed becomes a rate
      // limit rather than an edge — which is how Meet's version once produced an
      // interruption every cooldown at somebody whose hand had not moved.
      state.handsUp.add(candidate.key);
      if (baselining) continue;

      const last = state.handsLastSentAt.get(candidate.key) || 0;
      if (cooldownMs && now - last < cooldownMs) continue;
      state.handsLastSentAt.set(candidate.key, now);

      send({
        type: 'handRaise',
        id: candidate.key,
        name: candidate.name,
        // Reported, not acted on: only Python knows the name the avatar joined under.
        isSelf: candidate.isSelf,
      });
      state.handsSent += 1;
    }

    /*
     * A hand comes down when it has not been *seen* for a while, not when one scan
     * missed it. A raised hand genuinely disappears from the DOM for a moment when Zoom
     * re-renders a row, virtualises a long participant list, or the panel is scrolled —
     * none of which is anybody lowering their hand, and treating them as such would
     * re-detect the same unmoved hand as a brand new one on the next scan.
     */
    const graceMs = CONFIG.handDownGraceMs || 2500;
    for (const key of Array.from(state.handsUp)) {
      const seenAt = state.handsSeenAt.get(key) || 0;
      if (now - seenAt >= graceMs) {
        state.handsUp.delete(key);
        state.handsSeenAt.delete(key);
      }
    }

    /*
     * Nothing found yet: say what the page does have, a bounded number of times.
     *
     * **Reported only from a frame that can actually see the meeting.** `add_init_script`
     * runs in every frame Chromium creates, and most of them are Zoom's helper iframes
     * with no participant list in them at all — so an unconditional report buried the one
     * frame that matters under a stream of `rows: 0` from frames that were never going to
     * find anything. That is what a live run looked like, and it made the diagnostic
     * useless for the exact question it exists to answer.
     */
    /*
     * **Spaced out over the meeting, not spent in the first three seconds.** The earlier
     * version capped at four reports with no interval, so all four fired during the join —
     * before anybody could raise anything — and the one moment worth observing was never
     * sampled. A diagnostic that cannot be present at the failure is not one.
     */
    const diagEveryMs = CONFIG.handDiagIntervalMs || 15000;
    if (
      state.handsSent === 0 &&
      state.handsDiagnostics < 20 &&
      !baselining &&
      now - state.handsLastDiagAt >= diagEveryMs
    ) {
      let rows = [];
      try {
        rows = document.querySelectorAll(
          (CONFIG.handRowSelectors || []).join(',') || 'nothing'
        );
      } catch (err) {
        /* the join produced something unparseable; the list stays empty */
      }
      if (rows.length > 0 || state.panelOpened) {
        state.handsDiagnostics += 1;
        state.handsLastDiagAt = now;
        report('handsIdle', {
          rows: rows.length,
          panelOpened: state.panelOpened,
          handLabels: handLabelSample(),
          // **A sample of the markup, which is what the last round was missing.** Knowing
          // the count is zero says the selectors miss; knowing what a row actually
          // contains is what lets the matcher be corrected without another live meeting.
          // Truncated hard — this crosses a socket that also carries the avatar's voice.
          sample: rowSample(rows),
        });
      }
    }
  }

  /*
   * The first couple of participant rows, reduced to what identifies a hand indicator:
   * the classes present, and any markup token that looks like an icon reference.
   *
   * Deliberately not the raw `innerHTML` — a row carries the participant's name and a
   * meeting's worth of Zoom's own markup, and putting that in a log is both noisy and a
   * needless copy of somebody's name. Classes and icon references are what a selector is
   * written against.
   */
  function rowSample(rows) {
    const out = [];
    try {
      for (const row of rows) {
        if (out.length >= 2) break;
        const classes = new Set();
        const icons = new Set();
        const nodes = [row].concat(Array.from(row.querySelectorAll('*')).slice(0, 60));
        for (const node of nodes) {
          const cls = String(node.getAttribute && node.getAttribute('class') || '');
          for (const token of cls.split(/\s+/)) {
            if (token) classes.add(token.slice(0, 60));
          }
          const href =
            (node.getAttribute && (node.getAttribute('href') || node.getAttribute('xlink:href'))) ||
            '';
          if (href) icons.add(String(href).slice(0, 60));
          const tag = (node.tagName || '').toLowerCase();
          if (tag === 'use' || tag === 'svg') icons.add(tag + ':' + String(href).slice(0, 40));
        }
        out.push({
          classes: Array.from(classes).slice(0, 25),
          icons: Array.from(icons).slice(0, 8),
        });
      }
    } catch (err) {
      /* a diagnostic must never be the thing that breaks the scan */
    }
    return out;
  }

  /*
   * Up to eight strings on the page that mention a hand, whatever renders them.
   *
   * Deliberately wider than the matcher: it ignores HAND_EXCLUDE and the trigger list, so
   * it captures the toolbar's own "Raise Hand" alongside anything Zoom writes when somebody
   * actually raises one. Both are useful — seeing only the toolbar entry proves the sweep
   * is running and the indicator is simply not a label, which is a different fix from a
   * sweep that is not running at all.
   */
  function handLabelSample() {
    const found = [];
    const push = (value) => {
      const text = String(value || '').replace(/\s+/g, ' ').trim().slice(0, 120);
      if (text && text.toLowerCase().indexOf('hand') !== -1 && found.indexOf(text) === -1) {
        found.push(text);
      }
    };
    try {
      const nodes = document.querySelectorAll('[aria-label],[title]');
      let seen = 0;
      for (const node of nodes) {
        if (seen++ > 4000 || found.length >= 8) break;
        push(node.getAttribute('aria-label'));
        push(node.getAttribute('title'));
      }
      // The participant rows' own text, because Zoom may render the indicator as an icon
      // glyph or an emoji inside the row rather than as an attribute on anything.
      if (found.length < 8) {
        for (const selector of CONFIG.handRowSelectors || []) {
          let rows = [];
          try {
            rows = document.querySelectorAll(selector);
          } catch (err) {
            continue;
          }
          for (const row of rows) {
            if (found.length >= 8) break;
            push(row.getAttribute('aria-label') || row.textContent);
          }
        }
      }
    } catch (err) {
      /* a diagnostic must never be the thing that breaks the scan */
    }
    return found;
  }

  function startHandObserver() {
    if (!CONFIG.handRaiseEnabled || state.handsTimer) return;
    // Polled rather than a MutationObserver, and that is the cheaper of the two here:
    // Zoom mutates its participant list constantly, so an observer would fire hundreds
    // of times a second on the renderer thread that also encodes the avatar's audio,
    // and every one of those callbacks would run the same scan this timer runs twice a
    // second.
    state.handsTimer = setInterval(() => {
      try {
        scanHands();
      } catch (err) {
        /* A scan that throws must not stop the next one, and must never reach the
           page: this shares a thread with the microphone. */
      }
    }, CONFIG.handScanMs || 500);
  }

  // Connect immediately rather than on first use: the socket has to be open before
  // Zoom asks for a microphone, and the join begins as soon as the page loads.
  connect();
  ensureBuilt().catch(() => {});
  startHandObserver();
})();
