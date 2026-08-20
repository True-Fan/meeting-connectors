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
 * TWO INGEST MODES, AND WHY THIS FILE HAS A BIG CONDITIONAL IN IT
 * --------------------------------------------------------------
 * Under `ingestMode: 'rtms'` almost everything the Google Meet connector scrapes out
 * of a page, Zoom hands over as data: RTMS reports who joined, who left, who is
 * speaking, what each person said and what they typed, each with a name attached. So
 * in that mode this script does *not* read the roster, the chat panel or the
 * captions, and adding that would be paying Meet's price for a problem Zoom does not
 * have. A raised hand is the one exception — RTMS's event list has no hand-raise
 * event in it, so the indicator exists only on screen.
 *
 * Under `ingestMode: 'browser'` there is no RTMS at all. That mode exists because
 * RTMS requires the meeting to be hosted on an account with it enabled, which most
 * deployments do not have and cannot obtain — and the avatar has to work in an
 * ordinary person's ordinary meeting. Every signal RTMS used to carry then has to
 * come from the only thing left that can see it, which is this page. So the roster,
 * the active speaker, the chat and the captions are all read here, and the meeting's
 * audio is tapped out of Zoom's own playout graph.
 *
 * Nothing here decides what any of it *means*. The page reports what it sees; Python
 * decides whether to interrupt, whose hand it was, which names are new, and what the
 * agent is told — the same split the Meet connector draws, for the same reason.
 *
 * WHY THE AUDIO TAP IS NOT AN `RTCPeerConnection` TAP
 * ---------------------------------------------------
 * Meet's bridge patches `RTCPeerConnection` and reads inbound transceivers, which is
 * correct there and is the technique every write-up on the subject describes. It is
 * not sufficient here. Zoom's web client does not reliably carry meeting audio over
 * WebRTC: its long-standing mode decodes audio in WebAssembly off a WebSocket and
 * plays it through Web Audio, with no inbound audio transceiver anywhere to find.
 * A peer-connection tap in that mode is not fragile, it is simply empty — which is
 * exactly what an earlier attempt here measured, and why this connector used RTMS
 * for ingest in the first place.
 *
 * So the tap is placed at the **playout** end instead of the transport end, where
 * both modes have to converge: audio that is going to be heard must reach either an
 * `AudioContext`'s destination or a media element. `installAudioTap` patches all
 * three paths — Web Audio, media elements, and peer connections — and fans whatever
 * it finds into one capture context. That makes it indifferent to which transport
 * Zoom chose, including to Zoom changing its mind between releases.
 */

(() => {
  const CONFIG = window.__mcZoomConfig || {};
  const ENDPOINT = CONFIG.endpoint;
  const SAMPLE_RATE = CONFIG.sampleRateHz || 16000;
  const WORKLET_SOURCE = CONFIG.workletSource;
  if (!ENDPOINT || !WORKLET_SOURCE) return;

  const BROWSER_INGEST = CONFIG.ingestMode === 'browser';

  const state = {
    context: null,
    node: null,
    micTrack: null,
    building: null,
    socket: null,
    frames: 0,
    // -- the meeting's audio, tapped (browser ingest only) -----------------
    capture: null,
    captureNode: null,
    captureBuilding: null,
    captureSources: 0,
    // Every MediaStream/AudioContext already wired into the capture graph. Tapping the
    // same stream twice sums it with itself, which is a 6 dB level jump and audible
    // clipping — and Zoom re-attaches the same stream on every re-render.
    captureSeen: new WeakSet(),
    captureFrames: 0,
    tapDestinations: new WeakMap(),
    // -- meeting observation (browser ingest only) -------------------------
    rosterTimer: null,
    rosterLast: '',
    speakerLast: null,
    speakerSince: 0,
    // message -> how many copies of it have been answered. A high-water mark, not a set —
    // see `chatSeen` for the two failures that shape produced.
    chatEmitted: new Map(),
    chatArmed: false,
    captionSeen: new Set(),
    captionPending: new Map(),
    captionsArmed: false,
    panelsOpened: {},
    // When each observer first looked for its panel, for the readiness timeout — see
    // `panelReady`. Not when the panel was *opened*: the observer may be reading a panel
    // somebody else opened, or one it was told not to open at all.
    watchSince: {},
    // Whether each observer has ever found anything. An observer that has is not diagnosed
    // further; one that has not is the whole reason `observerIdle` exists.
    rosterFound: false,
    speakerFound: false,
    chatFound: false,
    captionsFound: false,
    diagCount: 0,
    diagLastAt: 0,
    // Class tokens seen on the speaker/tile elements at the previous scan, and every token
    // observed to appear or disappear since. See `speakerChurnScan`.
    speakerTokens: null,
    speakerChurn: new Set(),
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
    // The microphone graph terminates at a `MediaStreamDestination`, never at
    // `context.destination`, so the tap would not catch it in any case — this is belt to
    // those braces. **If it ever did catch it the avatar would hear itself**, the echo gate
    // is deliberately open under browser ingest, and the agent would answer its own
    // sentences in the loop doc 008 §4 describes. Cheap insurance against a future edit.
    context.__mcOwn = true;

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

  /*
   * Send one tapped PCM frame back to the bridge, framed exactly as `page/protocol.py`
   * expects: 'ZWB1', version, kind 2, reserved, pts_us, byte length, then the samples.
   *
   * Binary rather than a JSON envelope for the reason the inbound direction is: this is
   * fifty messages a second, and base64 in an object would cost a third more bytes and a
   * parse per frame for readability nobody benefits from.
   *
   * `pts_us` is stamped from the audio clock and is **advisory only** — Python restamps
   * from its own media clock, because `AudioContext.currentTime` runs on the audio device's
   * timeline and drifts against the monotonic clock the pipeline is paced on. It is carried
   * anyway because it is the only thing that can attribute latency to the browser.
   */
  const CAPTURE_HEADER_BYTES = 20;

  function sendAudio(pcm) {
    const socket = state.socket;
    if (!socket || socket.readyState !== 1) return false;
    const bytes = pcm.byteLength;
    const buffer = new ArrayBuffer(CAPTURE_HEADER_BYTES + bytes);
    const view = new DataView(buffer);
    view.setUint8(0, 0x5a); // Z
    view.setUint8(1, 0x57); // W
    view.setUint8(2, 0x42); // B
    view.setUint8(3, 0x31); // 1
    view.setUint8(4, 1); // version
    view.setUint8(5, 2); // kind: page -> bridge audio capture
    view.setUint16(6, 0); // reserved
    const now = state.capture ? state.capture.currentTime : 0;
    view.setBigUint64(8, BigInt(Math.max(0, Math.round(now * 1e6))));
    view.setUint32(16, bytes);
    new Uint8Array(buffer, CAPTURE_HEADER_BYTES).set(new Uint8Array(pcm.buffer || pcm));
    try {
      socket.send(buffer);
      return true;
    } catch (err) {
      return false;
    }
  }

  // -- the meeting's audio, tapped out of Zoom's playout --------------------

  /*
   * The capture graph: one 16 kHz AudioContext, one worklet, everything fanned into it.
   *
   * **16 kHz is set on the constructor and that is the whole resampling story.** Web Audio
   * resamples every source connected into this context down to the context's rate in native
   * code before the worklet sees a sample, so `capture_worklet.js` has no resampler in it
   * and `ingest/mapping.py` can assert the format rather than convert it. The same property
   * the Google Meet connector relies on, and the reason RTMS's native 16 kHz was worth
   * calling out when this connector used it.
   *
   * One context rather than one per source, because fan-in *is* the mix: connecting five
   * participants' nodes to one destination is how Web Audio sums them, and it does it on the
   * audio thread. Mixing in the worklet would be the same arithmetic, worse.
   */
  async function buildCapture() {
    if (state.capture) return;
    const Ctx = window.AudioContext || window.webkitAudioContext;
    const context = new Ctx({ sampleRate: 16000 });
    // **Marked before anything is connected inside it.** `installWebAudioTap` patches
    // `AudioNode.prototype.connect` globally, so without this the termination below —
    // an edge into this context's own destination — would be tapped as though it were
    // Zoom's audio. The result is a feedback loop through a zero gain: harmless to the
    // sound, and ruinous to the diagnostics, because `audioTapped` is the line an
    // operator is told to check first when the avatar is deaf (doc 009 §6).
    context.__mcOwn = true;

    const blob = new Blob([CONFIG.captureWorkletSource], { type: 'application/javascript' });
    const url = URL.createObjectURL(blob);
    try {
      await context.audioWorklet.addModule(url);
    } finally {
      URL.revokeObjectURL(url);
    }

    const node = new AudioWorkletNode(context, 'mc-zoom-capture', {
      numberOfInputs: 1,
      numberOfOutputs: 1,
      processorOptions: {
        frameSamples: Math.round(16000 * ((CONFIG.captureFrameMs || 20) / 1000)),
      },
    });
    node.port.onmessage = (event) => {
      // The worklet posts nothing else, so this is a guard against a stale asset rather
      // than an expected branch — but a non-Int16Array reaching `sendAudio` would frame a
      // garbage length and desynchronise the socket that also carries the avatar's voice.
      if (!(event.data instanceof Int16Array)) return;
      state.captureFrames += 1;
      sendAudio(event.data);
    };

    /*
     * **A worklet whose output goes nowhere is not guaranteed to be pulled.** The graph has
     * to terminate at a destination or the render loop has no reason to run this node, and
     * the failure is total and silent: `process` is simply never called, no frame is ever
     * posted, and every other part of the connector reports healthy.
     *
     * The Google Meet bridge terminates its capture graph the same way, having found the
     * same thing. Zero gain because there is nothing to play this to — and on a host that
     * does have speakers, playing the conference aloud would create an acoustic loop.
     */
    const silence = context.createGain();
    silence.gain.value = 0;
    node.connect(silence);
    silence.connect(context.destination);

    // Chromium can start an AudioContext suspended. A suspended context renders nothing, so
    // the tap would be permanently silent with no error anywhere.
    if (context.state === 'suspended') {
      await context.resume();
    }

    state.capture = context;
    state.captureNode = node;
  }

  function ensureCapture() {
    if (!state.captureBuilding) {
      state.captureBuilding = buildCapture().catch((err) => {
        state.captureBuilding = null;
        report('captureBuildFailed', { error: String((err && err.message) || err) });
        throw err;
      });
    }
    return state.captureBuilding;
  }

  /*
   * Wire one MediaStream into the capture graph, once.
   *
   * Guarded by `captureSeen` because every path below can offer the same stream more than
   * once — a media element's `srcObject` is reassigned on re-render, a peer connection fires
   * `track` again after renegotiation — and connecting a source twice sums it with itself.
   * That is a 6 dB jump and audible clipping, not a duplicate frame, so it is worth a
   * WeakSet rather than being left to Python.
   *
   * A stream with no audio track is skipped rather than remembered: Zoom's video-only
   * streams would otherwise occupy a slot and the audio track added to that same stream a
   * moment later would never be picked up.
   */
  async function tapStream(stream, how) {
    if (!stream || typeof stream.getAudioTracks !== 'function') return;
    if (state.captureSeen.has(stream)) return;
    if (stream.getAudioTracks().length === 0) return;
    state.captureSeen.add(stream);

    await ensureCapture();
    if (!state.captureNode) return;
    try {
      const source = state.capture.createMediaStreamSource(stream);
      source.connect(state.captureNode);
      state.captureSources += 1;
      report('audioTapped', { how: how, sources: state.captureSources });
    } catch (err) {
      report('audioTapFailed', { how: how, error: String((err && err.message) || err) });
    }
  }

  /*
   * The Web Audio path, and the one that actually carries Zoom's audio in its WASM mode.
   *
   * `AudioNode.prototype.connect` is patched rather than `AudioContext.destination` being
   * replaced, because the destination is a read-only accessor on a live object and Zoom
   * holds a reference to it from before any patch could run. Intercepting the *edge* being
   * drawn works regardless of when the node was created.
   *
   * **The original connect still happens.** This adds a second edge to a
   * `MediaStreamAudioDestinationNode` in Zoom's own context, and that node is what bridges
   * into our 16 kHz context — two AudioContexts cannot be connected directly, but a
   * MediaStream crosses between them. Zoom's own graph is left exactly as it was, so nothing
   * about what the page does or hears changes.
   *
   * One bridge per context, cached in `tapDestinations`: Zoom uses one context, but a page
   * that made several would otherwise get one MediaStream per *edge* rather than per graph.
   */
  function installWebAudioTap() {
    const proto = window.AudioNode && window.AudioNode.prototype;
    if (!proto || !proto.connect || proto.__mcTapped) return;
    const original = proto.connect;

    proto.connect = function (destination) {
      const result = original.apply(this, arguments);
      try {
        const context = this.context;
        // Only an edge into the speakers is the finished mix. Tapping every intermediate
        // edge would capture the same audio several times over, at whatever stage of
        // Zoom's processing each one happened to be.
        //
        // `__mcOwn` excludes the graphs this script built. The capture context terminates
        // itself at its own destination, and tapping that would wire the tap into itself.
        if (context && !context.__mcOwn && destination === context.destination) {
          let bridge = state.tapDestinations.get(context);
          if (!bridge) {
            bridge = context.createMediaStreamDestination();
            state.tapDestinations.set(context, bridge);
            tapStream(bridge.stream, 'webaudio');
          }
          // Outside the `if (!bridge)` above, deliberately: the bridge is built once per
          // context, but *every* node that connects to the destination is a separate source
          // that has to reach it. Building it and wiring it in the same block would tap
          // whichever node happened to connect first and silently ignore the rest.
          original.call(this, bridge);
        }
      } catch (err) {
        /* a tap that throws must never break the page's own audio graph */
      }
      return result;
    };
    proto.connect.__mcTapped = true;
    proto.__mcTapped = true;
  }

  /*
   * The media-element path.
   *
   * Zoom attaches remote audio to `<audio>` elements in some builds and modes. Two things
   * can be there: a MediaStream on `srcObject`, which `tapStream` takes directly, or a
   * MediaSource on `src`, which it cannot — so `captureStream()` is used for the second.
   *
   * **`captureStream` rather than `createMediaElementSource`**, which is the obvious call
   * and is wrong here: routing an element through a Web Audio source node *disconnects it
   * from the speakers* unless it is reconnected to a destination, and getting that wrong
   * makes the meeting inaudible to anyone watching the avatar's browser. `captureStream`
   * observes without rerouting.
   */
  function installMediaElementTap() {
    const proto = window.HTMLMediaElement && window.HTMLMediaElement.prototype;
    if (!proto || proto.__mcTapped) return;

    const consider = (element) => {
      try {
        if (element.srcObject) {
          tapStream(element.srcObject, 'element.srcObject');
        } else if (typeof element.captureStream === 'function') {
          tapStream(element.captureStream(), 'element.captureStream');
        }
      } catch (err) {
        /* an element that refuses to be captured is one we do not tap */
      }
    };

    const descriptor = Object.getOwnPropertyDescriptor(proto, 'srcObject');
    if (descriptor && descriptor.set) {
      Object.defineProperty(proto, 'srcObject', {
        get: descriptor.get,
        set(value) {
          descriptor.set.call(this, value);
          if (value) tapStream(value, 'srcObject');
        },
        configurable: true,
        enumerable: descriptor.enumerable,
      });
    }

    const originalPlay = proto.play;
    if (originalPlay) {
      // `play` as well as `srcObject`, because an element whose source was assigned before
      // this script ran — or through `src` rather than `srcObject` — is only observable at
      // the moment it starts.
      proto.play = function () {
        consider(this);
        return originalPlay.apply(this, arguments);
      };
    }
    proto.__mcTapped = true;
  }

  /*
   * The peer-connection path.
   *
   * Kept even though it is the one least likely to fire, because it costs a patch and Zoom
   * does use WebRTC in some configurations — and when it does, this is the cleanest of the
   * three: an inbound track with nothing else attached to it.
   *
   * Inbound only. `track` fires for receivers, so the avatar's own synthetic microphone
   * cannot arrive here — but the guard is explicit anyway, because tapping our own
   * microphone would feed the avatar its own voice and the echo gate is not the thing that
   * should have to catch that.
   */
  function installPeerConnectionTap() {
    const Original = window.RTCPeerConnection;
    if (!Original || Original.__mcTapped) return;

    const Patched = function (...args) {
      const pc = new Original(...args);
      pc.addEventListener('track', (event) => {
        try {
          if (!event.track || event.track.kind !== 'audio') return;
          const stream = (event.streams && event.streams[0]) || new MediaStream([event.track]);
          tapStream(stream, 'rtc');
        } catch (err) {
          /* as above */
        }
      });
      return pc;
    };
    Patched.prototype = Original.prototype;
    Patched.__mcTapped = true;
    window.RTCPeerConnection = Patched;
    window.webkitRTCPeerConnection = Patched;
  }

  function installAudioTap() {
    if (!BROWSER_INGEST || !CONFIG.captureWorkletSource) return;
    installWebAudioTap();
    installMediaElementTap();
    installPeerConnectionTap();

    // A sweep for what already exists. The patches above only see things created *after*
    // them, and `add_init_script` runs before the page's own scripts — but a reload, a
    // late injection into an iframe, or Zoom creating its graph during the join all leave
    // elements this would otherwise never hear about.
    setInterval(() => {
      try {
        for (const element of document.querySelectorAll('audio,video')) {
          if (element.srcObject) tapStream(element.srcObject, 'sweep');
        }
      } catch (err) {
        /* a sweep that throws must not stop the next one */
      }
    }, CONFIG.captureSweepMs || 2000);
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
        /*
         * **Reported, so Python can hold the authoritative "still up" state.**
         *
         * The page's own set is not enough, and a live meeting is why. `handsUp` is keyed on
         * a name read out of a tile that Zoom re-renders constantly; a hand that stays up but
         * whose row disappears for longer than the grace window is retired here and then
         * detected as a *fresh* raise on the very next scan. To the person in the meeting
         * nothing moved, and the avatar interrupts itself to say "ok, go ahead" again.
         *
         * With a lower event, Python can treat a raise as an edge into a state it holds
         * across page re-renders, reloads and frames — which is the only place that state can
         * survive. See ``ZoomMeetingObserver._on_hand``.
         */
        send({ type: 'handLower', id: key });
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

  // -- meeting observation (browser ingest only) ----------------------------

  /*
   * Everything below replaces an RTMS stream, and every one of them is a worse signal than
   * the thing it replaces. That is the honest trade browser ingest makes, and it is worth
   * stating once rather than apologising for in five places:
   *
   *   - RTMS reports a join with a name and a user id. A DOM reports a list of names, so a
   *     rejoin under the same name is invisible and two people called "Dev" are one person.
   *   - RTMS reports the active speaker as an event. A DOM renders a highlight, which lags
   *     and flickers.
   *   - RTMS transcribes per participant regardless of what the meeting has switched on.
   *     Captions have to be enabled, by somebody, visibly.
   *
   * All three are what the Google Meet connector has always lived with, and the ledgers on
   * the Python side were built for that grade of signal — hold windows, merge gaps,
   * name-keyed history. Nothing downstream had to be weakened to accept these.
   */

  function textOf(node) {
    if (!node) return '';
    const raw = node.getAttribute
      ? node.getAttribute('title') || node.getAttribute('aria-label') || node.textContent
      : node.textContent;
    return String(raw || '').replace(/\s+/g, ' ').trim();
  }

  function queryAll(selectors) {
    const found = [];
    for (const selector of selectors || []) {
      try {
        for (const node of document.querySelectorAll(selector)) found.push(node);
      } catch (err) {
        /* an unparseable selector is a config problem, not a reason to stop */
      }
      if (found.length) break; // most-specific-first: the first list that matches wins
    }
    return found;
  }

  function firstText(root, selectors) {
    for (const selector of selectors || []) {
      try {
        const el = root.querySelector(selector);
        const text = textOf(el);
        if (text) return text;
      } catch (err) {
        /* as above */
      }
    }
    return '';
  }

  /*
   * Strip the decorations Zoom hangs off a name in the participants panel.
   *
   * "Dev Choudhary (Host, me)" and "Dev Choudhary" are the same person, and a roster that
   * reported both would show the meeting gaining and losing an attendee every time Zoom
   * re-rendered the row with a different suffix. The ledger keys on the name, so this is
   * the only place the two can be reconciled.
   */
  const NAME_SUFFIX = /\s*\((?:[^)]*\b(?:host|co-host|me|guest|external|participant id)\b[^)]*)\)\s*$/i;

  function cleanName(raw) {
    let name = String(raw || '').split('\n')[0].trim();
    // Repeatedly, because Zoom stacks them: "Dev (Host) (me)".
    for (let i = 0; i < 3; i += 1) {
      const next = name.replace(NAME_SUFFIX, '').trim();
      if (next === name) break;
      name = next;
    }
    return name.slice(0, 120);
  }

  /*
   * Whether a row names the avatar itself.
   *
   * Advisory only: Python re-decides this against every name the avatar might have joined
   * under (`_self_name_candidates`), and this knows only the one it was configured with.
   * The two disagree whenever `MC_ZOOM__DISPLAY_NAME` and the name in the `POST /sessions`
   * request differ, which is why nothing downstream is allowed to trust it.
   */
  function isSelfName(name) {
    const lowered = String(name || '').toLowerCase();
    if (!lowered) return false;
    // Zoom labels the avatar's own row "You" or "(me)" rather than repeating the name.
    if (SELF_WORDS.indexOf(lowered) !== -1) return true;
    return !!CONFIG.displayName && lowered === String(CONFIG.displayName).toLowerCase();
  }

  /*
   * Open a panel once, and only if configured to.
   *
   * **Each of these is a visible action in somebody else's meeting**, which is why there is
   * one switch per panel rather than one for all of them. An operator may well want the
   * roster (the panel is unobtrusive and already opened for hand raises) and not want the
   * avatar turning captions on for the room.
   */
  function openPanelOnce(key, selectors) {
    if (state.panelsOpened[key]) return;
    for (const selector of selectors || []) {
      let el = null;
      try {
        el = document.querySelector(selector);
      } catch (err) {
        continue;
      }
      if (!el) continue;
      state.panelsOpened[key] = true;
      try {
        el.click();
      } catch (err) {
        /* a click that throws is a control that was not one */
      }
      report('panelOpened', { panel: key, selector: selector });
      return;
    }
  }

  /*
   * Who the page can see, reported as a level.
   *
   * Sent only on change, and the comparison is over the *sorted* list: Zoom's participants
   * panel is virtualised and reorders rows as people speak, so an order-sensitive check
   * would report a roster change every few seconds in a meeting where nobody moved.
   */
  /*
   * A row Zoom labelled with a pronoun rather than a name.
   *
   * **The one self-check the page has to make, and Python cannot.** Zoom renders the local
   * participant's row as "You" or "(me)" instead of repeating the display name, and
   * `cleanName` strips the parenthetical — so a row reading exactly "You" arrives in Python
   * as a participant called "You". `_self_name_candidates` matches on the avatar's actual
   * names and would never recognise it, leaving a phantom attendee in the ledger for the
   * whole meeting and every headcount wrong by one.
   *
   * Where the row *does* carry the real name, it is passed through untouched and Python
   * filters it — which keeps the authoritative self-check where it knows the most names.
   */
  function isPronounSelf(name) {
    return SELF_WORDS.indexOf(String(name || '').toLowerCase()) !== -1;
  }

  function scanRoster() {
    const names = [];
    for (const row of queryAll(CONFIG.rosterRowSelectors)) {
      const name = cleanName(firstText(row, CONFIG.rosterNameSelectors) || textOf(row));
      if (!name || handMatch(name) || isPronounSelf(name)) continue;
      if (names.indexOf(name) === -1) names.push(name);
    }
    // Never an empty list. See `ZoomMeetingObserver._on_roster` — the avatar is always in
    // its own participants panel, so nothing found means this frame cannot see the panel,
    // and reporting it as an empty meeting would let a re-render wipe the roster.
    if (!names.length) return;
    state.rosterFound = true;

    const signature = names.slice().sort().join('\n');
    if (signature === state.rosterLast) return;
    state.rosterLast = signature;
    send({ type: 'roster', names: names });
  }

  /*
   * Who is talking, as Zoom draws it.
   *
   * **Held for a moment before it is believed.** Zoom's speaking indicator is an animation
   * driven by an audio level, so it flickers between syllables and between two people
   * talking at once. Reporting every flicker would hand the tracker a new turn several times
   * a second, and the agent a new "current speaker" with it. `speakerMinMs` is the floor
   * under that; `ZoomSpeakerTracker`'s hold and merge windows do the rest on the Python
   * side, where they already existed for Meet.
   */
  function scanSpeaker() {
    let speaking = null;
    for (const row of queryAll(CONFIG.speakerRowSelectors)) {
      let marked = false;
      for (const selector of CONFIG.speakerMarkerSelectors || []) {
        try {
          /*
           * Three directions, because the marker and the name are not on the same element
           * and which way to look depends on Zoom's layout. A live run showed the state as
           * `speaker-bar-container__video-frame--active` on the frame *containing* the
           * `video-avatar__avatar` that carries the name — so `closest` is the one that
           * finds it whenever the row selector resolved to the inner tile, and checking
           * only self-and-descendants (which this did) finds nothing at all.
           */
          if (
            row.matches(selector) ||
            row.querySelector(selector) ||
            (row.closest && row.closest(selector))
          ) {
            marked = true;
            break;
          }
        } catch (err) {
          /* as above */
        }
      }
      if (!marked) continue;
      const name = cleanName(firstText(row, CONFIG.rosterNameSelectors) || textOf(row));
      // A pronoun row is the avatar itself, and reporting it would be worse here than in the
      // roster: the tracker would name "You" as the current speaker for as long as the
      // avatar talked, and the interrupt source — which cannot recognise it as self either —
      // would take it as somebody talking over the avatar and stop it. Every sentence.
      if (name && !handMatch(name) && !isPronounSelf(name)) {
        speaking = name;
        break;
      }
    }

    const now = Date.now();
    if (speaking !== state.speakerLast) {
      state.speakerLast = speaking;
      state.speakerSince = now;
      return;
    }
    if (!speaking || !state.speakerSince) return;
    if (now - state.speakerSince < (CONFIG.speakerMinMs || 300)) return;
    // Zeroed so the same continuous turn is reported once. A genuine second turn by the
    // same person arrives as a transition through `null` and re-arms this.
    state.speakerSince = 0;
    state.speakerFound = true;
    send({ type: 'speaker', name: speaking, isSelf: isSelfName(speaking) });
  }

  /*
   * The meeting chat.
   *
   * Keyed on sender plus text plus the item's position, because Zoom re-renders the whole
   * list constantly and a scan-based reader sees every message on every pass. Position is in
   * the key so that somebody genuinely typing the same thing twice is two messages —
   * dropping the repeat would be the more surprising bug of the two.
   */
  /*
   * The first pass that finds anything records it and reports none of it.
   *
   * **Armed on first sight rather than on a timer**, which the hand observer can afford and
   * this cannot. A time-based baseline has to be chosen before the join, and the join spans
   * a waiting room — `join_timeout_s` defaults to 90 seconds. Any window short enough to be
   * useful has already expired by the time the panel opens, so the avatar would read a
   * meeting's accumulated backlog as having just arrived and answer a question from twenty
   * minutes ago. Keying on content instead makes the window exactly right by construction.
   *
   * Returns whether this pass is the baseline one.
   */
  /*
   * Whether the panel exists to be read, as opposed to merely being empty so far.
   *
   * **This distinction is the whole fix**, and `armOnFirstSight` did not make it. An empty
   * result means one of two things — the panel is open and nobody has typed, or the panel is
   * not rendered yet — and they demand opposite treatment. Treating "not rendered" as "open
   * and empty" arms the observer against a page it cannot see; treating "open and empty" as
   * "not rendered" is what swallowed the first message.
   *
   * The container is the signal that separates them: the list element exists whether or not
   * it has any children. The timer is a last resort for a build whose container selectors
   * have been renamed, so a missing selector costs a delay rather than the feature.
   */
  function panelReady(key, containerSelectors) {
    if (queryAll(containerSelectors).length) return true;
    if (!state.watchSince[key]) state.watchSince[key] = Date.now();
    return Date.now() - state.watchSince[key] >= (CONFIG.panelReadyTimeoutMs || 10000);
  }

  /*
   * Record what is already on screen without reporting it, once the panel can be seen.
   *
   * **Armed on the panel appearing, not on the first message appearing**, and a live meeting
   * is what forced the difference. `armOnFirstSight` waited for content, so a chat panel that
   * opened empty stayed unarmed — and then armed on the first message a participant sent,
   * recording their question as backlog and answering nothing. The observer looked healthy
   * (`observerArmed existing: 1`), the avatar was silent, and the log said a message had been
   * seen.
   *
   * The backlog this exists to suppress is whatever is in the panel *at the moment it opens*.
   * If that is nothing, then nothing is suppressed, and the very next message is new — which
   * is the case the old rule got exactly backwards.
   *
   * Returns whether this pass is the baseline one.
   */
  function armWhenReady(flag, ready, count) {
    if (state[flag] || !ready) return false;
    state[flag] = true;
    report('observerArmed', { observer: flag, existing: count });
    return true;
  }

  /*
   * **How many times this exact message is on screen, versus how many we have answered.**
   *
   * Three versions of this, and the two failures are worth keeping because they are opposite
   * mistakes with the same cause — trying to answer "is this message new?" with a set.
   *
   *   1. Keyed on `index + name + text`. Zoom virtualises its chat list, so the index of a
   *      message nobody touched changes constantly, and every shift made old messages look
   *      new. The avatar re-answers the backlog out loud.
   *   2. Keyed on `name + text`, with a node `WeakSet` alongside. No more re-answering — and
   *      **re-sending an identical message did nothing at all**, because the content was
   *      already in the set. Reported from a live meeting: pasting the same question was
   *      ignored, retyping a slightly different one worked. The trade was made knowingly and
   *      justified as "a repeated identical line is rare"; repeating a question the avatar
   *      did not answer is the opposite of rare, and it is exactly when a person repeats
   *      themselves.
   *
   * A set cannot express this because the question is not boolean. The panel shows *N* copies
   * of a line and the avatar has answered *M* of them; anything beyond M is new. So the state
   * is a high-water mark per message, and a re-render — which changes neither N nor M — is
   * invisible to it, while a second copy of an identical line raises N and is answered once.
   *
   * The high-water mark never decreases, which is what makes virtualisation safe: messages
   * scrolling out of the DOM lower N, and nothing is re-emitted when they scroll back.
   */
  function chatSeen(occurrence, key) {
    const emitted = state.chatEmitted.get(key) || 0;
    if (occurrence <= emitted) return true;
    state.chatEmitted.set(key, occurrence);
    // Per-session and bounded in practice, but a pathological page could grow this without
    // limit. Dropping the oldest half is cheaper than an LRU and costs at most one duplicate.
    if (state.chatEmitted.size > 4000) {
      state.chatEmitted = new Map(Array.from(state.chatEmitted).slice(2000));
    }
    return false;
  }

  function scanChat() {
    openPanelOnce('chat', CONFIG.chatPanelSelectors);
    const found = [];
    for (const item of queryAll(CONFIG.chatItemSelectors)) {
      const text = firstText(item, CONFIG.chatTextSelectors);
      if (!text) continue;
      const name = cleanName(firstText(item, CONFIG.chatNameSelectors));
      found.push({ node: item, key: name + '\n' + text, name: name, text: text });
    }

    if (found.length) state.chatFound = true;

    // **Nothing is emitted or recorded before the panel is readable.** Messages seen while
    // the panel is still rendering cannot be classified — they may be a backlog or they may
    // be brand new — so they are left alone until the next pass, by which time they can be.
    const ready = panelReady('chat', CONFIG.chatContainerSelectors);
    if (!state.chatArmed && !ready) return;
    const baseline = armWhenReady('chatArmed', ready, found.length);

    // Counted **in document order**, so the second copy of a line is occurrence 2 whether it
    // was pasted a second ago or an hour ago. `chatSeen` compares that against how many
    // copies have already been answered.
    const occurrences = new Map();
    for (const entry of found) {
      const key = entry.key;
      const occurrence = (occurrences.get(key) || 0) + 1;
      occurrences.set(key, occurrence);

      // On the baseline pass every message on screen is recorded as already answered, which
      // is what stops the avatar reading out a meeting's backlog when the panel opens.
      if (chatSeen(occurrence, key) || baseline) continue;
      send({
        // Unique per delivery rather than per message text: two identical lines are two
        // requests, and Python's own bookkeeping should be able to tell them apart.
        id: key + '\n#' + occurrence,
        type: 'chat',
        name: entry.name || null,
        text: entry.text.slice(0, 2000),
      });
    }
  }

  /*
   * Zoom's live transcript.
   *
   * **Settled, not streamed.** A caption element is rewritten in place while Zoom revises
   * its guess, so reading it on every scan yields a dozen partial versions of one sentence.
   * A line is emitted once its text has stopped changing for `captionSettleMs`, which is
   * what `final` on the wire means. Interim text is what makes a caption panel feel live and
   * is worthless as a record — and worse than worthless to an agent, which would answer a
   * half-parsed question.
   */
  function scanCaptions() {
    openPanelOnce('captions', CONFIG.captionsButtonSelectors);
    const now = Date.now();
    const items = queryAll(CONFIG.captionItemSelectors);
    // Armed on the same terms as the chat, and with the same correction. The settle rule
    // alone would hold back an in-flight sentence, but a transcript panel opened mid-meeting
    // is already full of settled ones — without a baseline the avatar is handed the entire
    // meeting so far as though it had just been said.
    //
    // The empty case matters more here than for chat: the avatar may have just switched
    // captions on itself, in which case the panel is *always* empty at that moment and the
    // first line transcribed is *always* genuinely new. Arming on first content would have
    // discarded it every single time.
    if (items.length) state.captionsFound = true;
    const ready = panelReady('captions', CONFIG.captionContainerSelectors);
    if (!state.captionsArmed && !ready) return;
    const baseline = armWhenReady('captionsArmed', ready, items.length);
    let index = 0;
    for (const item of items) {
      index += 1;
      const text = firstText(item, CONFIG.captionTextSelectors) || textOf(item);
      if (!text) continue;
      const name = cleanName(firstText(item, CONFIG.captionNameSelectors));
      const slot = index + '\n' + name;
      const key = name + '\n' + text;
      if (baseline) {
        state.captionSeen.add(key);
        continue;
      }
      const pending = state.captionPending.get(slot);
      if (!pending || pending.text !== text) {
        state.captionPending.set(slot, { text: text, at: now, name: name });
        continue;
      }
      if (now - pending.at < (CONFIG.captionSettleMs || 1200)) continue;
      state.captionPending.delete(slot);
      if (state.captionSeen.has(key)) continue;
      state.captionSeen.add(key);
      if (state.captionSeen.size > 4000) {
        state.captionSeen = new Set(Array.from(state.captionSeen).slice(2000));
      }
      send({
        type: 'caption',
        name: name || null,
        text: text.slice(0, 2000),
        final: true,
      });
    }
  }

  /*
   * How many elements each selector in a list currently matches.
   *
   * The first question about a silent observer is always "does the selector match anything",
   * and answering it per selector rather than for the list as a whole is what separates "the
   * whole concept is renamed" from "the precise one is gone and only the fallback is left".
   * -1 means the selector itself is unparseable, which is a config bug rather than a Zoom
   * change.
   */
  function selectorCounts(selectors) {
    const out = {};
    for (const selector of selectors || []) {
      try {
        out[selector] = document.querySelectorAll(selector).length;
      } catch (err) {
        out[selector] = -1;
      }
    }
    return out;
  }

  /*
   * Every class token on the page containing one of `needles`.
   *
   * **This is the diagnostic that actually fixes a selector**, and the live run is what
   * proved the others insufficient. `handsIdle` reported `rows: 0` for the participants
   * panel and it was correct — but knowing a count is zero says only that the guess was
   * wrong, not what the right answer is. Reporting the class tokens Zoom is really using
   * turns the next fix into an edit rather than another meeting: the roster selectors were
   * corrected to `video-avatar__avatar` from exactly this kind of evidence.
   *
   * Tokens rather than markup, for the reason `rowSample` gives: a row carries somebody's
   * name, and a diagnostic should not be a copy of it in a log.
   */
  function classTokenSample(needles, limit) {
    const found = new Set();
    try {
      for (const node of document.querySelectorAll('[class]')) {
        const raw = node.getAttribute('class');
        if (typeof raw !== 'string') continue; // SVG className is not a string
        for (const token of raw.split(/\s+/)) {
          const lowered = token.toLowerCase();
          for (const needle of needles) {
            if (lowered.indexOf(needle) !== -1) {
              found.add(token.slice(0, 60));
              break;
            }
          }
        }
        if (found.size >= limit) break;
      }
    } catch (err) {
      /* a diagnostic must never be the thing that breaks the scan */
    }
    return Array.from(found).slice(0, limit);
  }

  /*
   * The class tokens on the meeting's own tile and speaker elements, right now.
   *
   * Scoped and shallow where `classTokenSample` sweeps the whole page: this runs on every
   * scan rather than every fifteen seconds, on the thread that also encodes the avatar's
   * audio, so it reads the class attribute of a few dozen elements and does not descend.
   * The modifier being hunted (`…__video-frame--active`) sits on exactly this kind of
   * element.
   */
  const CHURN_ROOTS = ["[class*='speaker' i]", "[class*='video-avatar' i]"];

  function speakerTokensNow() {
    const found = new Set();
    for (const selector of CHURN_ROOTS) {
      let nodes = [];
      try {
        nodes = document.querySelectorAll(selector);
      } catch (err) {
        continue;
      }
      let seen = 0;
      for (const node of nodes) {
        if (seen++ > 120) break;
        const raw = node.getAttribute && node.getAttribute('class');
        if (typeof raw !== 'string') continue;
        for (const token of raw.split(/\s+/)) {
          if (token) found.add(token.slice(0, 60));
        }
      }
    }
    return found;
  }

  /*
   * **Which class tokens come and go.**
   *
   * Two live runs failed to identify the active-speaker marker for the same reason, and it
   * is a reason worth writing down: `observerIdle` samples the page every fifteen seconds,
   * and almost none of those moments are moments when somebody is talking. Run 2 caught
   * `speaker-bar-container__video-frame--active` once, by luck; run 3 sampled nine times and
   * never saw it. Absence from a snapshot is not absence from the DOM.
   *
   * A state marker is, by definition, a class that *toggles*. So instead of asking what is
   * on the page at an arbitrary instant, this records what changes between instants — which
   * needs no correlation with audio and cannot miss a marker that appeared at all. The
   * layout containers (`speaker-active-container__wrap`, `…__video-frame`) are constant and
   * drop out of the answer for free, which is exactly the distinction the last two rounds of
   * selectors got wrong.
   */
  function speakerChurnScan() {
    if (!CONFIG.speakerEnabled || state.speakerFound) return;
    const current = speakerTokensNow();
    const baseline = state.speakerTokens;
    state.speakerTokens = current;
    if (baseline === null) return;

    for (const token of current) {
      if (!baseline.has(token)) state.speakerChurn.add('+' + token);
    }
    for (const token of baseline) {
      if (!current.has(token)) state.speakerChurn.add('-' + token);
    }
    if (state.speakerChurn.size > 60) {
      state.speakerChurn = new Set(Array.from(state.speakerChurn).slice(0, 60));
    }
  }

  /*
   * Say what each silent observer can see, a bounded number of times, spaced out.
   *
   * **Every observer here fails by finding nothing, and finding nothing is exactly what a
   * quiet meeting looks like.** That is the failure mode doc 008 §8 named for the hand
   * observer, and the first live run of browser ingest showed it applies to all four: the
   * roster, the speaker, the chat and the captions were each silent, and the log could not
   * say whether that was Zoom's markup or the meeting.
   *
   * Spaced rather than burst, for the reason the hand diagnostics are: firing them all
   * during the join samples the one window in which nothing has happened yet.
   */
  const OBSERVER_PROBES = [
    {
      flag: 'rosterFound',
      enabled: () => CONFIG.rosterEnabled,
      selectors: () => CONFIG.rosterRowSelectors,
      needles: ['video-avatar', 'participant', 'display-name'],
    },
    {
      flag: 'speakerFound',
      enabled: () => CONFIG.speakerEnabled,
      selectors: () => CONFIG.speakerMarkerSelectors,
      needles: ['speak', 'active', 'audio-anim', 'volume', 'talking'],
    },
    {
      flag: 'chatFound',
      enabled: () => CONFIG.chatEnabled,
      selectors: () => CONFIG.chatItemSelectors,
      needles: ['chat', 'message'],
    },
    {
      flag: 'captionsFound',
      enabled: () => CONFIG.captionsEnabled,
      selectors: () => CONFIG.captionItemSelectors,
      needles: ['transcript', 'caption', 'subtitle'],
    },
  ];

  function observerDiagnostics() {
    const now = Date.now();
    if (state.diagCount >= (CONFIG.observerDiagMax || 24)) return;
    if (now - state.diagLastAt < (CONFIG.observerDiagIntervalMs || 15000)) return;

    for (const probe of OBSERVER_PROBES) {
      if (!probe.enabled() || state[probe.flag]) continue;

      const counts = selectorCounts(probe.selectors());
      // What Zoom actually calls things right now. This is the field to read.
      const tokens = classTokenSample(probe.needles, 24);

      /*
       * **Reported only from a frame that can actually see the meeting**, which is the guard
       * `handsIdle` has had since doc 008 §8 and this was missing.
       *
       * `add_init_script` runs in every frame Chromium creates, and most of Zoom's are
       * helper iframes with no meeting UI in them at all. Without this, every one of them
       * reports `counts: all zero, tokens: []` on every interval — and a live run drowned the
       * one frame that mattered under them. Worse than useless: the roster observer was
       * working correctly in the meeting frame the whole time, and the log said it was
       * finding nothing.
       *
       * A frame that can see the meeting always has *something* to say: the token sweep is
       * far wider than the selectors, so all-zero-and-nothing-found is not a diagnosis, it is
       * a frame that was never going to find anything.
       */
      let blind = tokens.length === 0;
      for (const key in counts) {
        if (counts[key] > 0) blind = false;
      }
      if (blind) continue;

      state.diagCount += 1;
      state.diagLastAt = now;
      const detail = {
        observer: probe.flag.replace('Found', ''),
        counts: counts,
        tokens: tokens,
      };
      // **The field to read for the speaker.** A `+token` appeared and a `-token` went away
      // since the last scan, so anything listed here toggles — and a marker that toggles is
      // the marker. Constants like `speaker-active-container__wrap` never appear.
      if (probe.flag === 'speakerFound') {
        detail.churn = Array.from(state.speakerChurn).slice(0, 40);
      }
      report('observerIdle', detail);
      // One observer per interval rather than all four at once: they share a socket with the
      // avatar's voice, and four class sweeps in the same tick is a visible audio glitch.
      return;
    }
  }

  function startMeetingObservers() {
    if (!BROWSER_INGEST) return;
    // One timer for all four rather than four timers, because they share a thread with the
    // microphone and the capture graph: four independent intervals means four chances to
    // land in the same tick as an audio callback, for no benefit — none of these needs to
    // run at a different rate from the others.
    setInterval(() => {
      const passes = [
        [CONFIG.rosterEnabled, scanRoster],
        [CONFIG.speakerEnabled, scanSpeaker],
        [CONFIG.chatEnabled, scanChat],
        [CONFIG.captionsEnabled, scanCaptions],
      ];
      for (const [enabled, pass] of passes) {
        if (!enabled) continue;
        try {
          pass();
        } catch (err) {
          /* one pass failing must not stop the others, and must never reach the page */
        }
      }
      try {
        // Every scan, not every diagnostic interval: the whole point is to catch a class
        // that is only present while somebody is speaking.
        speakerChurnScan();
      } catch (err) {
        /* as above */
      }
      try {
        observerDiagnostics();
      } catch (err) {
        /* as above, and doubly so: this exists to explain failures, not to cause them */
      }
    }, CONFIG.observeMs || 700);
  }

  // Connect immediately rather than on first use: the socket has to be open before
  // Zoom asks for a microphone, and the join begins as soon as the page loads.
  connect();
  ensureBuilt().catch(() => {});
  // Before the join, and that is load-bearing: the patches only see graphs and elements
  // created after them, and Zoom builds its playout graph while joining.
  installAudioTap();
  startHandObserver();
  startMeetingObservers();
})();
