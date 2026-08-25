/*
 * The avatar's microphone inside the Teams web client, the meeting's audio on the way back
 * out, and everything about the meeting that only a browser can see.
 *
 * THE MICROPHONE
 * --------------
 * The same technique the Google Meet and Zoom-web connectors use: PCM arrives over a
 * loopback WebSocket, an AudioWorklet turns it into a real `MediaStreamTrack`, and a patched
 * `getUserMedia` hands that track to the page instead of a physical device. No OS audio
 * device, nothing to install.
 *
 * **This connector does not need a profile with a device already chosen, and Zoom-web does.**
 * Zoom will not start its capture pipeline until its own device menu has a selection, so a
 * throwaway profile there publishes nothing however good the injected track is. Teams uses
 * what `getUserMedia` returns. A persistent profile is still worth having — a *signed-in* one
 * joins as a tenant user rather than a guest — but it is not what makes the audio work.
 *
 * WHY THE TAP IS AT PLAYOUT AND NOT ONLY AT THE PEER CONNECTION
 * ------------------------------------------------------------
 * Teams' web client does carry meeting audio over WebRTC, so an `RTCPeerConnection` tap is
 * expected to be the productive one here — unlike on Zoom, whose long-standing mode decodes
 * audio in WebAssembly off a WebSocket and has no inbound audio transceiver to find at all.
 *
 * All three paths are patched anyway, and that is a deliberate choice rather than
 * belt-and-braces padding. The property that holds across every transport a browser can use
 * is that audio which is going to be *heard* must reach either an `AudioContext`'s
 * destination or a media element. Tapping there makes this indifferent to which transport
 * Teams chose, including to Teams changing its mind between releases — which is exactly the
 * failure the Zoom-web connector spent a round of live meetings discovering.
 *
 * WHAT THE PAGE DECIDES
 * ---------------------
 * Nothing. The page reports what it sees — a name in the roster, a tile with a speaking
 * ring, a chat line, a caption, a raised hand — and Python decides whether to interrupt,
 * whose hand it was, which names are new, and what the agent is told. That split is what
 * keeps every policy question beside the settings that govern it.
 *
 * ONE MORE THING WORTH KNOWING BEFORE EDITING
 * -------------------------------------------
 * `add_init_script` runs this in *every* frame Chromium creates, and Teams creates several
 * that have no meeting UI in them. So: every observer must be safe to run in a frame that
 * can see nothing, every diagnostic must refuse to report from such a frame (or it buries
 * the one frame that matters), and duplicate reports from several frames are normal and are
 * absorbed in Python.
 */

(() => {
  const CONFIG = window.__mcTeamsConfig || {};
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
    // The socket's own bookkeeping, read by ``TeamsWebSession._probe_page``. Counts rather
    // than a boolean, because "connected once and then flapped nine times" and "connected
    // once and held" are different diagnoses and the second is the healthy one.
    connects: 0,
    closes: 0,
    reconnectAttempts: 0,
    reconnectTimer: null,
    connectError: null,
    // Sockets the liveness poll found already dead — the count that proves the poll is the
    // thing holding the channel up rather than the event handlers.
    staleSockets: 0,
    // -- the meeting's audio, tapped -------------------------------------
    capture: null,
    captureNode: null,
    captureBuilding: null,
    captureSources: 0,
    // Every MediaStream/AudioContext already wired into the capture graph. Tapping the same
    // stream twice sums it with itself, which is a 6 dB level jump and audible clipping —
    // and Teams re-attaches the same stream on every re-render.
    captureSeen: new WeakSet(),
    captureFrames: 0,
    tapDestinations: new WeakMap(),
    // -- meeting observation --------------------------------------------
    rosterLast: '',
    speakerLast: null,
    speakerSince: 0,
    // message -> how many copies of it have been answered. A high-water mark, not a set —
    // see `chatSeen` for the two failures that shape is here to prevent.
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
    // Selectors that only ever resolved to the app rail, reported once each — a selector
    // worth fixing rather than a click worth taking.
    railSkipped: {},
    lastPanelSelector: null,
    // Whether a live meeting has ever been seen in this frame, and how many times we have
    // tried to get back to one. See `guardMeetingNavigation`.
    sawMeeting: false,
    recoverAttempts: 0,
    // Whether each observer has ever found anything. An observer that has is not diagnosed
    // further; one that has not is the whole reason `observerIdle` exists.
    rosterFound: false,
    speakerFound: false,
    chatFound: false,
    captionsFound: false,
    diagCount: 0,
    diagLastAt: 0,
    // Hooks seen on the speaker/tile elements at the previous scan, and every hook observed
    // to appear or disappear since. See `speakerChurnScan`.
    speakerTokens: null,
    speakerChurn: new Set(),
    // -- hand-raise observation -----------------------------------------
    handsTimer: null,
    // Keys whose hand is currently up. Entering this set is what gets reported.
    handsUp: new Set(),
    // Key -> when it was last *seen* up. A hand comes down when it has not been seen for a
    // grace window, not when one scan missed it — see `scanHands`.
    handsSeenAt: new Map(),
    handsLastSentAt: new Map(),
    handsBaselineUntil: 0,
    handsArmed: false,
    handsSent: 0,
    handsDiagnostics: 0,
    handsLastDiagAt: 0,
    panelOpened: false,
  };
  window.__mcTeamsMic = state;

  // -- the synthetic microphone --------------------------------------------

  async function build() {
    if (state.context) return;
    const Ctx = window.AudioContext || window.webkitAudioContext;
    const context = new Ctx({ sampleRate: SAMPLE_RATE });
    // The microphone graph terminates at a `MediaStreamDestination`, never at
    // `context.destination`, so the tap would not catch it in any case — this is belt to
    // those braces. **If it ever did catch it the avatar would hear itself**: the echo gate
    // is deliberately open on this connector, so the agent would answer its own sentences in
    // a loop. Cheap insurance against a future edit.
    context.__mcOwn = true;

    // The worklet source travels as a string and is wrapped in a blob URL: there is no HTTP
    // origin of ours to fetch it from — the page's origin is Microsoft's.
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

    // Chromium can start an AudioContext suspended. The launch flags disable that policy;
    // this is the belt to those braces, because a suspended context renders nothing and the
    // track would be permanently silent with no error anywhere.
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

  /*
   * **The socket reconnects, and a live run is why this is not optional.**
   *
   * The first version connected once, at script start, and `onclose` merely nulled the
   * reference. That is enough on a page that loads its meeting and stays there. Teams does
   * not: joining walks through a launcher, a pre-join screen and several short-lived frames,
   * and somewhere in that churn the socket closes. The script stays alive — it had already
   * reported `handsArmed` — but with `state.socket === null` and nothing to call `connect()`
   * again.
   *
   * What that looks like from the outside is the worst kind of failure. The session joins,
   * reports healthy, and `first_audio_published attached_pages=0` is the only hint: the avatar
   * publishes into a socket nobody is holding, the tap's frames are dropped on the floor
   * before they are framed, and every observer's report — including the diagnostics that exist
   * to explain this — goes nowhere, because `report()` needs the socket too.
   *
   * So: retry, with a bounded backoff, for the life of the page. Bounded delay rather than
   * bounded attempts, because a page that has lost its socket has nothing else to do and the
   * session may outlive any attempt count worth naming.
   */
  function scheduleReconnect() {
    if (state.reconnectTimer !== null) return;
    state.reconnectAttempts += 1;
    const base = CONFIG.reconnectDelayMs || 500;
    const cap = CONFIG.reconnectMaxDelayMs || 5000;
    const delay = Math.min(base * Math.pow(2, Math.min(state.reconnectAttempts - 1, 4)), cap);
    state.reconnectTimer = setTimeout(() => {
      state.reconnectTimer = null;
      connect();
    }, delay);
  }

  /*
   * **Liveness is polled, not merely listened for, and a live run is why.**
   *
   * The event-driven reconnect above was not enough. A probe of a joined page reported
   * `socket_state=3` (CLOSED) with `connects=0`, `closes=0` and no constructor error — so the
   * socket had failed *before* `onopen`/`onclose` were attached, neither handler ever ran, and
   * the retry they schedule was never armed. Chromium is entitled to fail a refused WebSocket
   * synchronously; nothing in the spec promises a close event on an object whose connection
   * never started.
   *
   * A handler that might not fire cannot be the only thing holding the channel open. So the
   * state is *inspected* on a timer: anything past OPEN (CLOSING, CLOSED, or a reference to a
   * socket that quietly died) is discarded and reconnected. The event handlers stay, because
   * they react in milliseconds where this reacts in one second — they are the fast path, and
   * this is the one that cannot be skipped.
   */
  function ensureConnected() {
    const socket = state.socket;
    // 0 CONNECTING, 1 OPEN, 2 CLOSING, 3 CLOSED.
    if (socket && socket.readyState <= 1) return;
    if (socket) {
      state.socket = null;
      state.staleSockets += 1;
    }
    if (state.reconnectTimer !== null) return;
    connect();
  }

  function connect() {
    if (state.socket) return;
    let socket;
    try {
      socket = new WebSocket(ENDPOINT);
    } catch (err) {
      // Recorded rather than swallowed. `report()` cannot help here — it needs the very
      // socket that just failed — so the reason is kept on `state` where the Python-side
      // probe can read it (`TeamsWebSession._probe_page`). A page that cannot open the
      // channel at all is otherwise indistinguishable from one that never ran the script.
      state.connectError = String((err && err.message) || err);
      scheduleReconnect();
      return;
    }
    socket.binaryType = 'arraybuffer';
    socket.onmessage = async (event) => {
      const buffer = event.data;
      if (!(buffer instanceof ArrayBuffer) || buffer.byteLength <= HEADER_BYTES) return;
      await ensureBuilt();
      if (!state.node) return;
      // int16 PCM follows the fixed header; the worklet owns the ring buffer. Copy first,
      // then transfer the copy's buffer — posting a view over `buffer` while listing
      // `buffer.slice(0)` as the transfer hands the engine a transfer entry unreachable from
      // the message, which it is entitled to reject outright. The frame then never reaches
      // the worklet and the avatar is silent with nothing logged.
      const pcm = new Int16Array(buffer, HEADER_BYTES);
      const copy = pcm.slice(0);
      state.frames += 1;
      state.node.port.postMessage(copy, [copy.buffer]);
    };
    socket.onopen = () => {
      // Reset on success, not on attempt: a socket that flaps repeatedly should back off,
      // and one that reconnects cleanly should be quick again the next time.
      state.reconnectAttempts = 0;
      state.connectError = null;
      state.connects += 1;
    };
    socket.onclose = () => {
      state.socket = null;
      state.closes += 1;
      scheduleReconnect();
    };
    socket.onerror = () => {
      // `onclose` follows an error, so the retry is scheduled there. This exists so the
      // failure is not reported to the console as an unhandled one.
    };
    state.socket = socket;
  }

  /*
   * Send one JSON event back to the bridge.
   *
   * Text frames, where audio is binary: the transport tells the two apart, so nothing needs
   * a discriminator (`page/protocol.py`). Silent on failure — a socket that has gone away is
   * the session ending, and throwing out of a DOM observer would only take the scan loop
   * with it.
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
   * Every observer here fails by *finding nothing*, and finding nothing is exactly what a
   * quiet meeting looks like. These are what tell the two apart, and they are capped because
   * an observer that logs on every scan is a second problem rather than a diagnosis for the
   * first.
   */
  function report(name, detail) {
    send({ type: 'pageEvent', name: name, detail: detail || {} });
  }

  /*
   * Send one tapped PCM frame back to the bridge, framed exactly as `page/protocol.py`
   * expects: 'TWB1', version, kind 2, reserved, pts_us, byte length, then the samples.
   *
   * Binary rather than a JSON envelope for the reason the inbound direction is: this is fifty
   * messages a second, and base64 in an object would cost a third more bytes and a parse per
   * frame for readability nobody benefits from.
   *
   * `pts_us` is stamped from the audio clock and is **advisory only** — Python restamps from
   * its own media clock, because `AudioContext.currentTime` runs on the audio device's
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
    view.setUint8(0, 0x54); // T
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

  // -- the meeting's audio, tapped out of Teams' playout --------------------

  /*
   * The capture graph: one 16 kHz AudioContext, one worklet, everything fanned into it.
   *
   * **16 kHz is set on the constructor and that is the whole resampling story.** Web Audio
   * resamples every source connected into this context down to the context's rate in native
   * code before the worklet sees a sample, so `capture_worklet.js` has no resampler and
   * `ingest/page_audio_source.py` can assert the format rather than convert it.
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
    // `AudioNode.prototype.connect` globally, so without this the termination below — an
    // edge into this context's own destination — would be tapped as though it were Teams'
    // audio. The result is a feedback loop through a zero gain: harmless to the sound, and
    // ruinous to the diagnostics, because `audioTapped` is the line an operator is told to
    // check first when the avatar is deaf.
    context.__mcOwn = true;

    const blob = new Blob([CONFIG.captureWorkletSource], { type: 'application/javascript' });
    const url = URL.createObjectURL(blob);
    try {
      await context.audioWorklet.addModule(url);
    } finally {
      URL.revokeObjectURL(url);
    }

    const node = new AudioWorkletNode(context, 'mc-teams-capture', {
      numberOfInputs: 1,
      numberOfOutputs: 1,
      processorOptions: {
        frameSamples: Math.round(16000 * ((CONFIG.captureFrameMs || 20) / 1000)),
      },
    });
    node.port.onmessage = (event) => {
      // The worklet posts nothing else, so this is a guard against a stale asset rather than
      // an expected branch — but a non-Int16Array reaching `sendAudio` would frame a garbage
      // length and desynchronise the socket that also carries the avatar's voice.
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
     * Zero gain because there is nothing to play this to — and on a host that does have
     * speakers, playing the conference aloud would create an acoustic loop.
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
   * That is a 6 dB jump and audible clipping, not a duplicate frame.
   *
   * A stream with no audio track is skipped rather than remembered: Teams' video-only streams
   * would otherwise occupy a slot, and an audio track added to that same stream a moment
   * later would never be picked up.
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
   * The Web Audio path.
   *
   * `AudioNode.prototype.connect` is patched rather than `AudioContext.destination` being
   * replaced, because the destination is a read-only accessor on a live object and the page
   * holds a reference to it from before any patch could run. Intercepting the *edge* being
   * drawn works regardless of when the node was created.
   *
   * **The original connect still happens.** This adds a second edge to a
   * `MediaStreamAudioDestinationNode` in the page's own context, and that node is what
   * bridges into our 16 kHz context — two AudioContexts cannot be connected directly, but a
   * MediaStream crosses between them. The page's graph is left exactly as it was, so nothing
   * about what it does or plays changes.
   *
   * One bridge per context, cached in `tapDestinations`: a page that made several contexts
   * would otherwise get one MediaStream per *edge* rather than per graph.
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
        // edge would capture the same audio several times over, at whatever stage of the
        // page's processing each one happened to be.
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
   * Teams attaches remote audio to `<audio>` elements. Two things can be there: a MediaStream
   * on `srcObject`, which `tapStream` takes directly, or a MediaSource on `src`, which it
   * cannot — so `captureStream()` is used for the second.
   *
   * **`captureStream` rather than `createMediaElementSource`**, which is the obvious call and
   * is wrong here: routing an element through a Web Audio source node *disconnects it from
   * the speakers* unless it is reconnected to a destination, and getting that wrong makes the
   * meeting inaudible to anyone watching the avatar's browser. `captureStream` observes
   * without rerouting.
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
   * The peer-connection path, and on Teams this is the one expected to carry the meeting.
   *
   * An inbound track with nothing else attached to it is the cleanest of the three taps: no
   * ambiguity about what stage of processing it is at, and no dependence on how the page
   * chose to play it.
   *
   * Inbound only. `track` fires for receivers, so the avatar's own synthetic microphone
   * cannot arrive here — but the guard is explicit anyway, because tapping our own microphone
   * would feed the avatar its own voice and the echo gate is not the thing that should have
   * to catch that.
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
    if (!CONFIG.captureWorkletSource) return;
    installWebAudioTap();
    installMediaElementTap();
    installPeerConnectionTap();

    // A sweep for what already exists. The patches above only see things created *after*
    // them, and `add_init_script` runs before the page's own scripts — but a reload, a late
    // injection into an iframe, or Teams creating its graph during the join all leave
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
          // A fresh clone per call: Teams may stop the track it is handed, and a stopped
          // original would silence every later request.
          stream.addTrack(state.micTrack.clone());
          return stream;
        }
      }
      return original(constraints);
    };
  }

  /*
   * **Teams needs to see a microphone in the device list, not just get one from
   * `getUserMedia`.** Without this it reports "Mic disconnected — try troubleshooting" in the
   * call and publishes nothing, which a live run showed while the patched `getUserMedia` above
   * was working perfectly: Teams enumerates audio inputs to populate its device menu, finds
   * nothing it can select, and concludes the microphone went away.
   *
   * **Appended to the real list rather than replacing it, which is where this deliberately
   * differs from the Google Meet bridge.** That one returns a fixed set of three fake devices
   * including an `audiooutput`, and it can afford to: its tap reads inbound WebRTC
   * transceivers, so nothing it needs depends on audio actually being *played*. This
   * connector taps at **playout** (§ the audio tap, above). If Teams were told the only
   * available output is a device that does not exist, it could route the meeting's audio to a
   * sink that renders nothing — and the tap would go silent for a reason that looks exactly
   * like a broken selector.
   *
   * So: one fake `audioinput`, the real devices left in place, and no fake output at all.
   *
   * No fake `videoinput` either. The avatar publishes no video, and advertising a camera would
   * have Teams offer one that produces a grey rectangle — the same objection
   * `--use-fake-device-for-media-stream` gets in the launcher.
   */
  const FAKE_MIC = {
    deviceId: 'mc-avatar-mic',
    kind: 'audioinput',
    label: 'Avatar Microphone',
    groupId: 'mc-avatar',
  };

  if (media && media.enumerateDevices) {
    const originalEnumerate = media.enumerateDevices.bind(media);
    media.enumerateDevices = async () => {
      let devices = [];
      try {
        devices = await originalEnumerate();
      } catch (err) {
        /* a host with no devices at all still gets ours */
      }
      const fake = {
        ...FAKE_MIC,
        toJSON() {
          return FAKE_MIC;
        },
      };
      // Ours first, so a client that picks the head of the list picks the one that works.
      return [fake, ...devices];
    };
  }

  // Permission queries must answer "granted", or Teams shows a prompt that cannot be clicked
  // in a headless browser.
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
   * Matched against labels and text rather than against markup hooks, because the sentence a
   * product shows a human is the most durable thing on a page — and Teams writes exactly such
   * a sentence, both into the roster row's accessible name and into the announcement it makes
   * when a hand goes up.
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
   * Each is a control the avatar's own client renders continuously, and each would otherwise
   * fire on every single scan: "Raise hand" is the reactions control, "Lower hand" is what it
   * becomes once pressed, and "Lower all hands" is the organiser control. Checked first, and
   * by substring, because Teams appends keyboard hints inside the same label.
   *
   * Deliberately **not** applied to the markup pass. An organiser sees a "Lower hand" action
   * on *other people's* roster rows, so excluding a row whose text contains that phrase would
   * discard exactly the hands an organiser most needs to see.
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

  /* The name in "Dev Choudhary, hand raised" — the label with the phrase taken out. */
  function handName(text, match) {
    if (!text || !match) return null;
    const name = (text.slice(0, match.at) + ' ' + text.slice(match.at + match.phrase.length))
      .replace(/[(),.;:•\-–—]/g, ' ')
      .replace(/\s+/g, ' ')
      .trim();
    return name ? name.slice(0, 120) : null;
  }

  /*
   * The participant row an indicator sits inside, so an icon with no text of its own still
   * resolves to a person.
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
    // The row's own `data-tid` first: it carries the display name and does not change when the
    // person mutes, which is what keeps this hand's key — and therefore the "already up" latch
    // on both sides — stable for as long as the hand is up. See `nameFromTid`.
    const fromTid = cleanName(nameFromTid(row));
    if (fromTid && !handMatch(fromTid)) return fromTid.slice(0, 120);
    const selectors = CONFIG.handNameSelectors || [];
    for (const selector of selectors) {
      try {
        const el = row.querySelector(selector);
        const text = el ? el.getAttribute('title') || el.textContent || '' : '';
        // Through `cleanName`, which every path out of here now is: a name element's text
        // carries the status decorations too, and an uncleaned one is the key that flapped.
        const name = cleanName(text);
        if (name && !handMatch(name)) return name.slice(0, 120);
      } catch (err) {
        /* as above */
      }
    }
    const label = (row.getAttribute('aria-label') || row.textContent || '')
      .split('\n')[0]
      .trim();
    const match = handMatch(label);
    const name = cleanName(match ? handName(label, match) : label);
    return name ? name.slice(0, 120) : null;
  }

  /*
   * One signal, resolved to the participant it is about.
   *
   * An unnamed hand is reported only where `allowAnonymous` says so — from the row-markup
   * pass, where the row is a real participant whose name simply is not spelled out in it, and
   * where Python can put a name on it by elimination. The selector and label passes drop it
   * instead: an unattributable trigger from an unknown element reappears on every scan, so
   * keying it would interrupt the avatar forever, which is a far more expensive mistake than
   * a missed hand.
   */
  function handResolve(node, name, how, allowAnonymous) {
    const row = handRow(node);
    if (!name && row) name = handRowName(row);

    if (!name) {
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
   * Markup hooks that mean "this row has a raised hand", matched against the row's own HTML
   * rather than against a label.
   *
   * **Compound tokens only, never a bare "hand", and that is a correctness requirement rather
   * than tidiness.** The row's HTML contains the participant's *name*, and plenty of real
   * names contain those four letters — "Chandra", "Handa", "Chand". Matching `hand` would
   * raise a permanent false hand for anybody so named, interrupting the avatar every cooldown
   * for the whole meeting. None of the tokens below can occur inside a name.
   *
   * Teams builds its UI out of `data-tid` hooks and Fluent icon component names, which is
   * what these are drawn from. They are the least certain list in this file — verify against
   * a live meeting with `MC_TEAMS_WEB__HEADLESS=false` and read the `handsIdle` diagnostics,
   * which report what a participant row actually contains when nothing matched.
   */
  const HAND_MARKUP = [
    'raised-hand',
    'raisedhand',
    'raised_hand',
    'hand-raised',
    'handraised',
    'raisehand',
    'raise-hand',
    'raise_hand',
    // Fluent's hand glyphs, which are how Teams draws the indicator itself.
    'handright',
    'hand-right',
  ];

  /*
   * Whether a participant row's markup says its owner has a hand up.
   *
   * Reads `innerHTML`, which catches the `data-tid`, the class name, the SVG `href`, the icon
   * ligature and any data attribute at once — so it keeps working when Teams renames one of
   * them, which a selector per shape does not.
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

  /*
   * Everything on the page that currently looks like a raised hand.
   *
   * Three passes, because Teams expresses the same state more than one way and none of them
   * is reliable alone. The row-markup pass reads the icon Teams renders inside a participant
   * row — precise, and dependent on hooks a release can rename. The selector pass reads the
   * indicator elements directly. The label pass walks accessible names and tooltips for a
   * trigger phrase — slower, and durable against a rename. A Teams release has to break all
   * three before the feature goes quiet, and the diagnostics say which one is finding things.
   */
  function handCandidates() {
    const found = new Map();

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

    // Bounded, because an unbounded sweep of a page Teams is still building shows up as
    // dropped audio frames rather than as an error. `[aria-label]` and `[title]` are where
    // Teams puts the sentence; reading attributes forces no layout.
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
   * **The roster indicator does not exist in a panel nobody opened.** Teams also draws a hand
   * on the participant's tile, but a tile is rendered only while that person is on screen —
   * so with the panel closed the observer is correct, running, and blind to anybody Teams has
   * paginated away. This is one of two visible actions the connector takes inside the meeting,
   * which is why it is a switch (`MC_TEAMS_WEB__HAND_RAISE_OPEN_PANEL`).
   *
   * Once, not on every scan: clicking a toggle repeatedly would close it again on the next
   * pass, which is a slow way to make the panel flicker for everybody watching a screen share.
   */
  function openParticipantsPanel() {
    if (state.panelOpened || !CONFIG.handOpenPanel) return;
    // The same two guards ``openPanelOnce`` applies, and for the same live failure: the app
    // rail's "People" button navigates out of the meeting, and nothing may be clicked before
    // the call exists. Duplicated rather than shared because this runs on the hand observer's
    // faster timer and is reached before the meeting observers start.
    if (!inMeeting()) return;
    for (const selector of CONFIG.participantsPanelSelectors || []) {
      let candidates = [];
      try {
        candidates = Array.from(document.querySelectorAll(selector));
      } catch (err) {
        continue;
      }
      for (const el of candidates) {
        if (inAppRail(el)) {
          if (!state.railSkipped[selector]) {
            state.railSkipped[selector] = true;
            report('panelSelectorHitAppRail', { panel: 'participants', selector: selector });
          }
          continue;
        }
        state.panelOpened = true;
        state.lastPanelSelector = selector;
        try {
          el.click();
        } catch (err) {
          /* a click that throws is a control that was not one; nothing to recover */
        }
        report('participantsPanelOpened', { selector: selector });
        return;
      }
    }
  }

  function scanHands() {
    if (!CONFIG.handRaiseEnabled) return;

    if (!state.handsArmed) {
      state.handsArmed = true;
      // A baseline window, because a hand that was already up when the avatar joined is not
      // an interruption of anything the avatar was saying. Recorded as up, reported to
      // nobody.
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
      // Written for a hand that is merely still up as much as for one that has just gone up:
      // this is the timestamp the retirement pass below reads.
      state.handsSeenAt.set(candidate.key, now);
      if (state.handsUp.has(candidate.key)) continue;

      // Recorded as up *before* the baseline and cooldown gates rather than after, so a hand
      // that is up but deliberately not reported still counts as up. Otherwise the next scan
      // reads it as a fresh raise and the gate it just failed becomes a rate limit rather
      // than an edge.
      state.handsUp.add(candidate.key);
      if (baselining) continue;

      const last = state.handsLastSentAt.get(candidate.key) || 0;
      if (cooldownMs && now - last < cooldownMs) continue;
      state.handsLastSentAt.set(candidate.key, now);

      send({
        type: 'handRaise',
        id: candidate.key,
        name: candidate.name,
        // Reported, not acted on: only Python knows every name the avatar joined under.
        isSelf: candidate.isSelf,
      });
      state.handsSent += 1;
    }

    /*
     * A hand comes down when it has not been *seen* for a while, not when one scan missed it.
     * A raised hand genuinely disappears from the DOM for a moment when Teams re-renders a
     * row, virtualises a long roster, or the panel is scrolled — none of which is anybody
     * lowering their hand, and treating them as such would re-detect the same unmoved hand as
     * a brand new one on the next scan.
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
         * The page's own set is not enough: it is keyed on a name read out of a row Teams
         * re-renders constantly, and several frames run this observer independently — so a
         * hand that stays up but whose row disappears for longer than the grace window is
         * retired here and detected as a *fresh* raise on the very next scan. To the person
         * in the meeting nothing moved, and the avatar interrupts itself to say "ok, go
         * ahead" again. With a lower event, Python can treat a raise as an edge into a state
         * it holds across page re-renders, reloads and frames.
         */
        send({ type: 'handLower', id: key });
      }
    }

    /*
     * Nothing found yet: say what the page does have, a bounded number of times, spaced out
     * over the meeting rather than spent during the join — a diagnostic that cannot be
     * present at the failure is not one.
     *
     * **Reported only from a frame that can actually see the meeting.** Most of Teams' frames
     * have no roster in them, so an unconditional report buries the one frame that matters
     * under a stream of `rows: 0` from frames that were never going to find anything.
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
          // **A sample of the markup, which is what makes the next fix an edit rather than
          // another live meeting.** Knowing the count is zero says the selectors miss;
          // knowing what a row actually contains is what lets the matcher be corrected.
          sample: rowSample(rows),
        });
      }
    }
  }

  /*
   * The first couple of participant rows, reduced to what identifies a hand indicator: the
   * hooks present, and any markup reference that looks like an icon.
   *
   * Deliberately not the raw `innerHTML` — a row carries the participant's name and a
   * meeting's worth of Teams' own markup, and putting that in a log is both noisy and a
   * needless copy of somebody's name.
   */
  function rowSample(rows) {
    const out = [];
    try {
      for (const row of rows) {
        if (out.length >= 2) break;
        const hooks = new Set();
        const icons = new Set();
        const nodes = [row].concat(Array.from(row.querySelectorAll('*')).slice(0, 60));
        for (const node of nodes) {
          if (!node.getAttribute) continue;
          const cls = String(node.getAttribute('class') || '');
          for (const token of cls.split(/\s+/)) {
            if (token) hooks.add(token.slice(0, 60));
          }
          // Teams identifies its own elements with `data-tid` far more consistently than with
          // class names, so the hook worth reporting is usually this one.
          const tid = node.getAttribute('data-tid');
          if (tid) hooks.add('tid:' + String(tid).slice(0, 56));
          const href = node.getAttribute('href') || node.getAttribute('xlink:href') || '';
          if (href) icons.add(String(href).slice(0, 60));
          const tag = (node.tagName || '').toLowerCase();
          if (tag === 'use' || tag === 'svg') icons.add(tag + ':' + String(href).slice(0, 40));
        }
        out.push({
          hooks: Array.from(hooks).slice(0, 25),
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
   * Deliberately wider than the matcher: it ignores HAND_EXCLUDE and the trigger list, so it
   * captures the toolbar's own "Raise hand" alongside anything Teams writes when somebody
   * actually raises one. Both are useful — seeing only the toolbar entry proves the sweep is
   * running and the indicator is simply not a label, which is a different fix from a sweep
   * that is not running at all.
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
      // The participant rows' own text, because Teams may render the indicator as an icon
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
    // Polled rather than a MutationObserver, and that is the cheaper of the two here: Teams
    // mutates its roster and tile grid constantly, so an observer would fire hundreds of
    // times a second on the renderer thread that also encodes the avatar's audio, and every
    // one of those callbacks would run the same scan this timer runs twice a second.
    state.handsTimer = setInterval(() => {
      try {
        scanHands();
      } catch (err) {
        /* A scan that throws must not stop the next one, and must never reach the page:
           this shares a thread with the microphone. */
      }
    }, CONFIG.handScanMs || 500);
  }

  // -- meeting observation --------------------------------------------------

  /*
   * Everything below is a rendering rather than an event, and that is the honest cost of not
   * needing a tenant's consent. Worth stating once rather than apologising for in four places:
   *
   *   - a DOM reports a list of names, so a rejoin under the same name is invisible and two
   *     people called "Dev" are one person;
   *   - the active speaker is a ring drawn on a tile, which lags and flickers;
   *   - captions have to be enabled, by somebody, visibly.
   *
   * All three are what the Google Meet connector has always lived with, and the ledgers on the
   * Python side were built for that grade of signal — hold windows, merge gaps, name-keyed
   * history. Nothing downstream had to be weakened to accept these.
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
   * Strip the decorations Teams hangs off a name in the roster.
   *
   * "Dev Choudhary (Guest)", "Dev Choudhary (Organizer)" and "Dev Choudhary" are the same
   * person, and a roster that reported each would show the meeting gaining and losing an
   * attendee every time Teams re-rendered the row with a different suffix. The ledger keys on
   * the name, so this is the only place the variants can be reconciled.
   *
   * Teams' set is wider than Zoom's: it labels guests, external participants, unverified
   * users, and the meeting's roles.
   */
  const NAME_SUFFIX =
    /\s*\((?:[^)]*\b(?:you|me|guest|external|organizer|organiser|presenter|attendee|co-organizer|co-organiser|unverified|out of office)\b[^)]*)\)\s*$/i;

  /*
   * The unbracketed decorations, which are the ones that actually broke things.
   *
   * **Teams writes a participant's *state* into the same accessible name as their name**, and
   * a live meeting produced all three of these for one person inside five seconds:
   *
   *     "Dev Choudhary muted Context menu is available"
   *     "Dev Choudhary Context menu is available"
   *     "Dev Choudhary"
   *
   * `NAME_SUFFIX` above cannot touch any of it — none of it is in brackets. That matters far
   * more than the cosmetics, because `handResolve` keys a raised hand on this string: the
   * moment somebody muted, their hand's key changed, the old key was retired by the grace
   * pass below, and the very next scan reported the same unmoved hand as a *fresh* raise. The
   * avatar stopped itself to say "ok, go ahead" again, for as long as they left it up.
   *
   * Supplied as configuration (`nameNoisePhrases`, `nameStatusWords`) for the reason every
   * selector here is: a Teams rename should cost a settings edit. The fallbacks are the two
   * observed live, so a page running against an older config is still corrected for the case
   * that was actually reported.
   */
  const FALLBACK_NOISE = ['context menu is available', 'context menu'];
  const FALLBACK_STATUS = ['muted', 'unmuted'];

  /* Multi-word wording removed anywhere: no display name contains "context menu". */
  function stripNoise(value) {
    let name = value;
    for (const phrase of CONFIG.nameNoisePhrases || FALLBACK_NOISE) {
      if (!phrase) continue;
      for (let guard = 0; guard < 4; guard += 1) {
        const at = name.toLowerCase().indexOf(phrase);
        if (at === -1) break;
        name = (name.slice(0, at) + ' ' + name.slice(at + phrase.length)).replace(/\s+/g, ' ');
      }
    }
    return name.trim();
  }

  /*
   * Bare status words, removed only where they *trail* the label.
   *
   * Any one of these could be somebody's real name or part of one, so removing them anywhere
   * would eventually rename a participant. Teams appends them, so the end is the only place
   * they legitimately occur — and a single remaining word is kept whatever it says, because a
   * name scrubbed to nothing is worse than one that kept a stray word.
   */
  function statusWords() {
    return (CONFIG.nameStatusWords || FALLBACK_STATUS).map((w) => String(w || '').toLowerCase());
  }

  function stripStatus(value) {
    const words = statusWords();
    let name = value;
    // Brackets and bare words alternately, because Teams stacks them in either order: "Dev
    // (Guest) muted" needs the word off before the bracket is at the end to be seen.
    for (let guard = 0; guard < 4; guard += 1) {
      const before = name;
      name = name.replace(NAME_SUFFIX, '').trim();
      const parts = name.split(/\s+/);
      if (parts.length > 1) {
        const last = parts[parts.length - 1].replace(/[,.;:]+$/, '').toLowerCase();
        if (words.indexOf(last) !== -1) name = parts.slice(0, -1).join(' ');
      }
      if (name === before) break;
    }
    return name.trim();
  }

  /*
   * A label made only of status words is not a person.
   *
   * A name element that failed to resolve leaves the row's status pill as the whole label, and
   * admitting "muted" to the roster puts a participant in the meeting who does not exist —
   * which breaks the "exactly one other person here" inference exactly as the duplicate
   * spellings do. Python applies the same rule; see `meeting/names.py` for the trade.
   */
  function allStatus(value) {
    const words = statusWords();
    const parts = value.split(/\s+/);
    if (!parts.length || !value) return false;
    for (const part of parts) {
      if (words.indexOf(part.toLowerCase()) === -1) return false;
    }
    return true;
  }

  /*
   * Halve a name that is its own first half repeated.
   *
   * Teams renders the name twice inside one row — as the row's accessible name and again in
   * the name element — so a label read off the container arrives as "Dev Choudhary Dev
   * Choudhary". Word-wise, so a genuine repeated word ("Ann Ann Smith") is left alone.
   */
  function collapseRepeat(value) {
    const words = value.split(/\s+/);
    if (words.length >= 2 && words.length % 2 === 0) {
      const half = words.length / 2;
      let same = true;
      for (let i = 0; i < half; i += 1) {
        if (words[i].toLowerCase() !== words[half + i].toLowerCase()) {
          same = false;
          break;
        }
      }
      if (same) return words.slice(0, half).join(' ');
    }
    return value;
  }

  function cleanName(raw) {
    let name = String(raw || '').split('\n')[0].replace(/\s+/g, ' ').trim();
    name = stripStatus(stripNoise(name)).replace(/^[\s,.;:-]+|[\s,.;:-]+$/g, '');
    if (allStatus(name)) return '';
    return collapseRepeat(name).slice(0, 120);
  }

  /*
   * The name Teams wrote into the row's own `data-tid`.
   *
   * **The most stable name source this page has, and it was being ignored.** A live meeting's
   * roster rows carry `data-tid="participantsInCall-Dev Choudhary"` — the display name, in an
   * attribute that does not change when the person mutes, unmutes, or has a context menu
   * attached. Everything else available here is rendered text, which does.
   *
   * Returns `''` for a row whose tid carries no name, and the caller falls back to reading
   * the markup as before.
   */
  function nameFromTid(node) {
    if (!node || !node.getAttribute) return '';
    const tid = String(node.getAttribute('data-tid') || '');
    for (const prefix of CONFIG.rosterTidPrefixes || []) {
      if (prefix && tid.length > prefix.length && tid.indexOf(prefix) === 0) {
        return tid.slice(prefix.length).replace(/[_-]+/g, ' ').trim();
      }
    }
    return '';
  }

  /*
   * Drop any node that contains another node from the same list.
   *
   * A prefix selector can match a group container as well as the rows inside it, and a
   * container resolves to *one* name for everybody in it — so the roster would report one
   * participant where there were three. Keeping only the innermost matches removes that
   * whole failure mode without needing to know which shape Teams is currently rendering.
   *
   * Bounded: this is quadratic, and it runs on the thread that encodes the avatar's audio.
   * Past a townhall-sized roster the list is returned untouched, which is the pre-existing
   * behaviour rather than a new risk.
   */
  function innermost(nodes) {
    if (nodes.length < 2 || nodes.length > 60) return nodes;
    const out = [];
    for (const node of nodes) {
      let container = false;
      for (const other of nodes) {
        if (other !== node && node.contains && node.contains(other)) {
          container = true;
          break;
        }
      }
      if (!container) out.push(node);
    }
    return out.length ? out : nodes;
  }

  /*
   * Whether a row names the avatar itself.
   *
   * Advisory only: Python re-decides this against every name the avatar might have joined
   * under, and this knows only the one it was configured with. The two disagree whenever
   * `MC_TEAMS_WEB__DISPLAY_NAME` and the name in the `POST /sessions` request differ, which is
   * why nothing downstream is allowed to trust it.
   */
  function isSelfName(name) {
    const lowered = String(name || '').toLowerCase();
    if (!lowered) return false;
    if (SELF_WORDS.indexOf(lowered) !== -1) return true;
    return !!CONFIG.displayName && lowered === String(CONFIG.displayName).toLowerCase();
  }

  /*
   * A row Teams labelled with a pronoun rather than a name.
   *
   * **The one self-check the page has to make, and Python cannot.** Teams renders the local
   * participant as "You" — and `cleanName` strips the parenthetical form — so a row reading
   * exactly "You" would arrive in Python as a participant called "You". The self-name check
   * there matches on the avatar's actual names and would never recognise it, leaving a phantom
   * attendee in the ledger for the whole meeting and every headcount wrong by one.
   *
   * Where the row *does* carry the real name it is passed through untouched and Python filters
   * it, which keeps the authoritative self-check where it knows the most names.
   */
  function isPronounSelf(name) {
    return SELF_WORDS.indexOf(String(name || '').toLowerCase()) !== -1;
  }

  /*
   * Open a panel once, and only if configured to.
   *
   * **Each of these is a visible action in somebody else's meeting**, which is why there is
   * one switch per panel rather than one for all of them. An operator may well want the roster
   * (the panel is unobtrusive, and is already opened for hand raises) and not want the avatar
   * turning captions on for the room.
   */
  /*
   * Whether this element is part of Teams' app navigation rather than the meeting.
   *
   * **The guard that stops the connector walking itself out of the meeting.** A live run had
   * the panel observer match the app rail's "People" button instead of the calling toolbar's,
   * click it, and send the whole single-page app to the contacts page — meeting still live
   * behind it, every observer now reading a page with no meeting in it, and the tap silent.
   * See ``TeamsObserverSelectors.app_rail`` for why this is an exclusion rather than a
   * tighter positive match.
   */
  function inAppRail(el) {
    if (!el || !el.closest) return false;
    for (const selector of CONFIG.appRailSelectors || []) {
      try {
        if (el.closest(selector)) return true;
      } catch (err) {
        /* an unparseable selector is a config problem, not a reason to allow the click */
      }
    }
    return false;
  }

  /* Whether the page currently has a live meeting in it. */
  function inMeeting() {
    return queryAll(CONFIG.meetingMarkerSelectors).length > 0;
  }

  function openPanelOnce(key, selectors) {
    if (state.panelsOpened[key]) return;
    // **Nothing is clicked before the call exists.** The pre-join screen and the app home
    // both render navigation that looks like a panel toggle, and clicking one there is how a
    // session leaves the meeting it just joined.
    if (!inMeeting()) return;
    for (const selector of selectors || []) {
      let candidates = [];
      try {
        candidates = Array.from(document.querySelectorAll(selector));
      } catch (err) {
        continue;
      }
      for (const el of candidates) {
        if (inAppRail(el)) {
          // Reported once per selector, because a selector that only ever matches the rail is
          // a selector to fix — and silently skipping it would leave the panel unopened with
          // nothing saying why.
          if (!state.railSkipped[selector]) {
            state.railSkipped[selector] = true;
            report('panelSelectorHitAppRail', { panel: key, selector: selector });
          }
          continue;
        }
        state.panelsOpened[key] = true;
        state.lastPanelSelector = selector;
        try {
          el.click();
        } catch (err) {
          /* a click that throws is a control that was not one */
        }
        report('panelOpened', { panel: key, selector: selector });
        return;
      }
    }
  }

  /*
   * Notice that the meeting has gone, say so, and try once to get back.
   *
   * **A session that has navigated out of its own meeting is otherwise dead and silent**: the
   * call keeps running, the avatar keeps reporting healthy, and every observer reads a page
   * with no meeting in it. The guards above are what should make this unreachable; this is
   * what happens when they are not enough.
   *
   * One attempt, and only after the meeting has actually been seen — so a page that never
   * joined does not spend its session pressing Back. In the live run that prompted this, two
   * presses of Back restored the meeting, which is why `history.back()` is worth trying at
   * all rather than merely reporting.
   */
  function guardMeetingNavigation() {
    if (inMeeting()) {
      state.sawMeeting = true;
      return;
    }
    if (!state.sawMeeting || state.recoverAttempts >= (CONFIG.maxRecoverAttempts || 2)) return;
    state.recoverAttempts += 1;
    report('meetingLost', {
      attempt: state.recoverAttempts,
      note: 'the page navigated away from the meeting; going back',
      lastPanel: state.lastPanelSelector || null,
    });
    try {
      window.history.back();
    } catch (err) {
      /* a page that refuses to go back is one we can only report */
    }
  }

  /*
   * Who the page can see, reported as a level.
   *
   * Sent only on change, and the comparison is over the *sorted* list: Teams' roster is
   * virtualised and reorders rows as people speak, so an order-sensitive check would report a
   * roster change every few seconds in a meeting where nobody moved.
   */
  function scanRoster() {
    const names = [];
    for (const row of innermost(queryAll(CONFIG.rosterRowSelectors))) {
      // **The `data-tid` first, then the markup.** A live meeting's roster rows carry the
      // display name in the attribute (`participantsInCall-Dev Choudhary`) and the *label*
      // Teams renders beside it reads "Dev Choudhary muted Context menu is available" — so
      // reading the markup first meant the ledger identified one person by up to three
      // different names, and could therefore never say "there is exactly one other person
      // here", which is what answers "what is my name".
      const name = cleanName(
        nameFromTid(row) || firstText(row, CONFIG.rosterNameSelectors) || textOf(row)
      );
      if (!name || handMatch(name) || isPronounSelf(name)) continue;
      if (names.indexOf(name) === -1) names.push(name);
    }
    // Never an empty list. See `TeamsMeetingObserver._on_roster` — the avatar is always in its
    // own roster, so nothing found means this frame cannot see it, and reporting that as an
    // empty meeting would let a re-render wipe the ledger's view of the room.
    if (!names.length) return;
    state.rosterFound = true;

    const signature = names.slice().sort().join('\n');
    if (signature === state.rosterLast) return;
    state.rosterLast = signature;
    send({ type: 'roster', names: names });
  }

  /*
   * Who is talking, as Teams draws it.
   *
   * **Held for a moment before it is believed.** Teams marks the active speaker with an
   * animated ring driven by an audio level, so it flickers between syllables and between two
   * people talking at once. Reporting every flicker would hand the tracker a new turn several
   * times a second, and the agent a new "current speaker" with it. `speakerMinMs` is the floor
   * under that; the tracker's hold and merge windows do the rest on the Python side.
   */
  function scanSpeaker() {
    let speaking = null;
    for (const row of queryAll(CONFIG.speakerRowSelectors)) {
      let marked = false;
      for (const selector of CONFIG.speakerMarkerSelectors || []) {
        try {
          /*
           * Three directions, because the marker and the name are usually not on the same
           * element and which way to look depends on the layout: Teams puts the speaking
           * state on a wrapper around the tile in gallery view and on the tile itself
           * elsewhere. Checking only self-and-descendants finds nothing in the first case.
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
      // Same order as `scanRoster`, and it has to be: the tracker matches a speaker against the
      // roster's names, so a speaker read one way and a roster read the other never meet.
      const name = cleanName(
        nameFromTid(row) || firstText(row, CONFIG.rosterNameSelectors) || textOf(row)
      );
      // A pronoun row is the avatar itself, and reporting it would be worse here than in the
      // roster: the tracker would name "You" as the current speaker for as long as the avatar
      // talked, and the interrupt source — which cannot recognise it as self either — would
      // take it as somebody talking over the avatar and stop it. Every sentence.
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
    // Zeroed so the same continuous turn is reported once. A genuine second turn by the same
    // person arrives as a transition through `null` and re-arms this.
    state.speakerSince = 0;
    state.speakerFound = true;
    send({ type: 'speaker', name: speaking, isSelf: isSelfName(speaking) });
  }

  /*
   * Whether the panel exists to be read, as opposed to merely being empty so far.
   *
   * **This distinction is the whole point.** An empty result means one of two things — the
   * panel is open and nobody has typed, or the panel is not rendered yet — and they demand
   * opposite treatment. Treating "not rendered" as "open and empty" arms the observer against
   * a page it cannot see; treating "open and empty" as "not rendered" swallows the first real
   * message, which is a failure the Zoom-web connector shipped and had to fix.
   *
   * The container is the signal that separates them: the list element exists whether or not it
   * has any children. The timer is a last resort for a build whose container selectors have
   * been renamed, so a missing selector costs a delay rather than the feature.
   */
  function panelReady(key, containerSelectors) {
    if (queryAll(containerSelectors).length) return true;
    if (!state.watchSince[key]) state.watchSince[key] = Date.now();
    return Date.now() - state.watchSince[key] >= (CONFIG.panelReadyTimeoutMs || 10000);
  }

  /*
   * Record what is already on screen without reporting it, once the panel can be seen.
   *
   * The backlog this exists to suppress is whatever is in the panel *at the moment it opens*.
   * If that is nothing, then nothing is suppressed, and the very next message is new.
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
   * A set cannot express this, and two opposite bugs on the Zoom-web connector are why the
   * shape is a high-water mark instead. Keyed on `index + name + text`, a virtualised chat
   * list renumbers messages nobody touched and every shift makes old messages look new — the
   * avatar re-answers the backlog out loud. Keyed on `name + text` in a set, **re-sending an
   * identical message does nothing at all**, because the content is already in the set; a
   * participant repeating a question the avatar did not answer is ignored, which is exactly
   * when people repeat themselves.
   *
   * The question is not boolean. The panel shows *N* copies of a line and the avatar has
   * answered *M* of them; anything beyond M is new. The mark never decreases, which is what
   * makes virtualisation safe: messages scrolling out of the DOM lower N, and nothing is
   * re-emitted when they scroll back.
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

    // **Nothing is emitted or recorded before the panel is readable.** Messages seen while the
    // panel is still rendering cannot be classified — they may be a backlog or they may be
    // brand new — so they are left alone until the next pass, by which time they can be.
    const ready = panelReady('chat', CONFIG.chatContainerSelectors);
    if (!state.chatArmed && !ready) return;
    const baseline = armWhenReady('chatArmed', ready, found.length);

    // Counted **in document order**, so the second copy of a line is occurrence 2 whether it
    // was pasted a second ago or an hour ago.
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
   * Teams' live captions.
   *
   * **Settled, not streamed.** A caption line is rewritten in place while the recogniser
   * revises its guess, so reading it on every scan yields a dozen partial versions of one
   * sentence. A line is emitted once its text has stopped changing for `captionSettleMs`,
   * which is what `final` on the wire means. Interim text is what makes a caption panel feel
   * live and is worthless as a record — and worse than worthless to an agent, which would
   * answer a half-parsed question.
   */
  function scanCaptions() {
    openPanelOnce('captions', CONFIG.captionsButtonSelectors);
    const now = Date.now();
    const items = queryAll(CONFIG.captionItemSelectors);
    // Armed on the same terms as the chat. The settle rule alone would hold back an in-flight
    // sentence, but a caption panel opened mid-meeting is already full of settled ones —
    // without a baseline the avatar is handed the whole meeting so far as though it had just
    // been said.
    //
    // The empty case matters more here than for chat: when `captions_auto_enable` is on the
    // avatar switches captions on itself, so the panel is *always* empty at that moment and
    // the first line transcribed is *always* genuinely new. Arming on first content would have
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
   * -1 means the selector itself is unparseable, which is a config bug rather than a Teams
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
   * Every hook on the page containing one of `needles`.
   *
   * **This is the diagnostic that actually fixes a selector.** Knowing a count is zero says
   * only that the guess was wrong; reporting what Teams is really calling things turns the
   * next fix into an edit rather than another live meeting.
   *
   * Both `data-tid` and `class` are swept, and `data-tid` is the one to read: Teams identifies
   * its own elements with it far more consistently than with class names, whose Fluent-emitted
   * hashes change between builds by design.
   *
   * Hooks rather than markup, for the reason `rowSample` gives: a row carries somebody's name,
   * and a diagnostic should not be a copy of it in a log.
   */
  function hookTokenSample(needles, limit) {
    const found = new Set();
    const consider = (raw, prefix) => {
      if (typeof raw !== 'string') return; // SVG className is not a string
      for (const token of raw.split(/\s+/)) {
        const lowered = token.toLowerCase();
        for (const needle of needles) {
          if (lowered.indexOf(needle) !== -1) {
            found.add(prefix + token.slice(0, 60));
            break;
          }
        }
      }
    };
    try {
      for (const node of document.querySelectorAll('[data-tid]')) {
        consider(node.getAttribute('data-tid'), 'tid:');
        if (found.size >= limit) break;
      }
      for (const node of document.querySelectorAll('[class]')) {
        consider(node.getAttribute('class'), '');
        if (found.size >= limit) break;
      }
    } catch (err) {
      /* a diagnostic must never be the thing that breaks the scan */
    }
    return Array.from(found).slice(0, limit);
  }

  /*
   * The hooks on the meeting's own tile and roster elements, right now.
   *
   * Scoped and shallow where `hookTokenSample` sweeps the whole page: this runs on every scan
   * rather than every fifteen seconds, on the thread that also encodes the avatar's audio, so
   * it reads two attributes off a few dozen elements and does not descend.
   */
  const CHURN_ROOTS = [
    "[data-tid*='participant' i]",
    "[data-tid*='stream' i]",
    "[class*='voice' i]",
    "[class*='speak' i]",
  ];

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
        if (!node.getAttribute) continue;
        const tid = node.getAttribute('data-tid');
        if (typeof tid === 'string' && tid) found.add('tid:' + tid.slice(0, 56));
        const raw = node.getAttribute('class');
        if (typeof raw !== 'string') continue;
        for (const token of raw.split(/\s+/)) {
          if (token) found.add(token.slice(0, 60));
        }
      }
    }
    return found;
  }

  /*
   * **Which hooks come and go.**
   *
   * `observerIdle` samples the page every fifteen seconds, and almost none of those moments
   * are moments when somebody is talking — so a snapshot repeatedly fails to contain the
   * active-speaker marker, and absence from a snapshot is not absence from the DOM. The
   * Zoom-web connector lost two live runs to exactly that.
   *
   * A state marker is, by definition, a hook that *toggles*. So instead of asking what is on
   * the page at an arbitrary instant, this records what changes between instants — which needs
   * no correlation with audio and cannot miss a marker that appeared at all. Layout containers
   * are constant and drop out of the answer for free, which is the distinction a snapshot
   * cannot make.
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
   * Spaced rather than burst, because firing them all during the join samples the one window
   * in which nothing has happened yet.
   */
  const OBSERVER_PROBES = [
    {
      flag: 'rosterFound',
      enabled: () => CONFIG.rosterEnabled,
      selectors: () => CONFIG.rosterRowSelectors,
      needles: ['participant', 'roster', 'display-name', 'displayname'],
    },
    {
      flag: 'speakerFound',
      enabled: () => CONFIG.speakerEnabled,
      selectors: () => CONFIG.speakerMarkerSelectors,
      needles: ['speak', 'active', 'voice', 'volume', 'dominant'],
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
      needles: ['caption', 'transcript', 'subtitle'],
    },
  ];

  function observerDiagnostics() {
    const now = Date.now();
    if (state.diagCount >= (CONFIG.observerDiagMax || 24)) return;
    if (now - state.diagLastAt < (CONFIG.observerDiagIntervalMs || 15000)) return;

    for (const probe of OBSERVER_PROBES) {
      if (!probe.enabled() || state[probe.flag]) continue;

      const counts = selectorCounts(probe.selectors());
      // What Teams actually calls things right now. This is the field to read.
      const tokens = hookTokenSample(probe.needles, 24);

      /*
       * **Reported only from a frame that can actually see the meeting.**
       *
       * `add_init_script` runs in every frame Chromium creates, and most of Teams' are helper
       * frames with no meeting UI in them at all. Without this, every one of them reports
       * `counts: all zero, tokens: []` on every interval — and the one frame that mattered is
       * buried. Worse than useless: the observer may be working correctly in the meeting frame
       * the whole time while the log says it is finding nothing.
       *
       * A frame that can see the meeting always has *something* to say: the hook sweep is far
       * wider than the selectors, so all-zero-and-nothing-found is not a diagnosis, it is a
       * frame that was never going to find anything.
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
      // the marker. Layout constants never appear.
      if (probe.flag === 'speakerFound') {
        detail.churn = Array.from(state.speakerChurn).slice(0, 40);
      }
      report('observerIdle', detail);
      // One observer per interval rather than all four at once: they share a socket with the
      // avatar's voice, and four hook sweeps in the same tick is a visible audio glitch.
      return;
    }
  }

  function startMeetingObservers() {
    // One timer for all four rather than four timers, because they share a thread with the
    // microphone and the capture graph: four independent intervals means four chances to land
    // in the same tick as an audio callback, for no benefit — none of these needs to run at a
    // different rate from the others.
    setInterval(() => {
      try {
        // Cheap — a readyState comparison — and it means the channel is checked at the
        // observers' cadence too, not only on its own timer.
        ensureConnected();
      } catch (err) {
        /* as below: one failure must not stop the others */
      }
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
        // **Before the diagnostics and after the passes**: a frame that has lost its meeting
        // should be trying to get back rather than reporting on what it cannot see.
        guardMeetingNavigation();
      } catch (err) {
        /* as above */
      }
      try {
        // Every scan, not every diagnostic interval: the whole point is to catch a hook that
        // is only present while somebody is speaking.
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

  // Connect immediately rather than on first use: the socket has to be open before Teams asks
  // for a microphone, and the join begins as soon as the page loads.
  connect();
  // **Its own timer, and started first.** The observers' shared timer also calls this, but the
  // observers can be switched off entirely — and the channel is what carries the avatar's
  // voice, which is not optional. One second, because a mute avatar is measured in seconds.
  setInterval(() => {
    try {
      ensureConnected();
    } catch (err) {
      /* a liveness check that throws must not stop the next one */
    }
  }, CONFIG.channelCheckMs || 1000);
  ensureBuilt().catch(() => {});
  // Before the join, and that is load-bearing: the patches only see graphs and elements
  // created after them, and Teams builds its playout graph while joining.
  installAudioTap();
  startHandObserver();
  startMeetingObservers();
})();
