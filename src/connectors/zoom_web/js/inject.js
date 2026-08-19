/*
 * The avatar's microphone, inside Zoom's web client.
 *
 * The same technique the Google Meet connector uses: PCM arrives over a loopback
 * WebSocket, an AudioWorklet turns it into a real MediaStreamTrack, and a patched
 * `getUserMedia` hands that track to the page instead of a physical device. No OS
 * audio device, nothing to install.
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
 * Deliberately much smaller than Meet's `bridge.js`, which also carries video,
 * roster, chat and hand raises. This is the microphone and nothing else.
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

  // Connect immediately rather than on first use: the socket has to be open before
  // Zoom asks for a microphone, and the join begins as soon as the page loads.
  connect();
  ensureBuilt().catch(() => {});
})();
