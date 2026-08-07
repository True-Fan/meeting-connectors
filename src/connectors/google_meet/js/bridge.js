/*
 * The Chromium page bridge.
 *
 * Injected by `automation/driver.py` with `add_init_script`, so it runs before
 * any Google Meet script in the page and can install its hooks on pristine
 * globals. That ordering is the whole trick: patching `getUserMedia` after Meet
 * has already captured a reference to it would do nothing.
 *
 * Four jobs, and nothing else:
 *
 *   1. Synthesise a camera and a microphone, and hand them to Meet through the
 *      ordinary `getUserMedia` path so Meet publishes them like real devices.
 *   2. Tap every inbound remote audio track, mix it, and ship it to Python as
 *      16 kHz mono PCM.
 *   3. Report what it observes: roster, admission state, track lifecycle.
 *   4. Carry frames both ways over one loopback WebSocket.
 *
 * It contains no business logic, no avatar knowledge, no decoding, no metrics
 * and no logging. It reports facts upward and applies decisions sent downward;
 * every judgement about what those facts mean is made in Python. Even the DOM
 * selectors arrive in CONFIG rather than being written here, so keeping up with
 * a Meet UI change is a settings edit rather than an asset edit.
 *
 * The wire codec below mirrors `websocket/protocol.py` byte for byte;
 * `tests/unit/test_google_meet_js_assets.py` checks the constants have not
 * drifted apart.
 */

(() => {
  'use strict';

  // Meet renders into iframes, and an init script runs in each of them. Media
  // capture and publication happen in the top frame, and opening one WebSocket
  // per frame would mean the bridge server rejecting all but the first.
  if (window !== window.top) {
    return;
  }
  if (window.__MC_BRIDGE_INSTALLED__) {
    return;
  }
  const CONFIG = window.__MC_BRIDGE_CONFIG__;
  const WORKLETS = window.__MC_BRIDGE_WORKLETS__;
  if (!CONFIG || !WORKLETS) {
    return;
  }
  window.__MC_BRIDGE_INSTALLED__ = true;

  // ------------------------------------------------------------------ wire

  const MAGIC = 0x474d4331; // 'GMC1'
  const WIRE_VERSION = 1;
  const HEADER_SIZE = 24;
  const AUDIO_HEADER_SIZE = 12;
  const VIDEO_HEADER_SIZE = 12;

  const TYPE = {
    VIDEO_I420: 0x01,
    AUDIO_PCM: 0x02,
    HELLO: 0x03,
    CONFIG: 0x04,
    READY: 0x05,
    LEAVE: 0x06,
    HEARTBEAT: 0x07,
    ERROR: 0x08,
    PARTICIPANTS: 0x09,
    MEET_STATE: 0x0a,
    PAGE_EVENT: 0x0b,
  };

  const FLAG = { NONE: 0x00, KEYFRAME: 0x01, SILENCE: 0x02, MIXED: 0x04 };

  const SAMPLE_FORMAT_S16LE = 1;

  function writeHeader(view, type, payloadLen, seq, ptsUs, flags) {
    view.setUint32(0, MAGIC, false);
    view.setUint8(4, WIRE_VERSION);
    view.setUint8(5, type);
    view.setUint8(6, flags);
    view.setUint8(7, 0); // reserved
    view.setUint32(8, seq >>> 0, false);
    view.setBigInt64(12, BigInt(Math.trunc(ptsUs)), false);
    view.setUint32(20, payloadLen, false);
  }

  function encodeJson(type, body) {
    const payload = new TextEncoder().encode(JSON.stringify(body));
    const buffer = new ArrayBuffer(HEADER_SIZE + payload.byteLength);
    writeHeader(new DataView(buffer), type, payload.byteLength, 0, 0, FLAG.NONE);
    new Uint8Array(buffer, HEADER_SIZE).set(payload);
    return buffer;
  }

  function encodeAudio(pcm, seq, ptsUs, flags) {
    const payloadLen = AUDIO_HEADER_SIZE + pcm.byteLength;
    const buffer = new ArrayBuffer(HEADER_SIZE + payloadLen);
    const view = new DataView(buffer);
    writeHeader(view, TYPE.AUDIO_PCM, payloadLen, seq, ptsUs, flags);

    view.setUint32(HEADER_SIZE + 0, CONFIG.captureSampleRateHz, false);
    view.setUint8(HEADER_SIZE + 4, 1); // channels — the mix is mono
    view.setUint8(HEADER_SIZE + 5, SAMPLE_FORMAT_S16LE);
    view.setUint16(HEADER_SIZE + 6, CONFIG.captureFrameMs, false);
    view.setUint32(HEADER_SIZE + 8, 0, false); // MIXED_SOURCE

    // The worklet already produced little-endian int16 in a typed array, which
    // is what s16le means on every platform Chromium targets, so this is a
    // straight byte copy rather than a per-sample conversion.
    new Uint8Array(buffer, HEADER_SIZE + AUDIO_HEADER_SIZE).set(
      new Uint8Array(pcm.buffer, pcm.byteOffset, pcm.byteLength)
    );
    return buffer;
  }

  function decode(buffer) {
    if (buffer.byteLength < HEADER_SIZE) {
      throw new Error('short message');
    }
    const view = new DataView(buffer);
    if (view.getUint32(0, false) !== MAGIC) {
      throw new Error('bad magic');
    }
    if (view.getUint8(4) !== WIRE_VERSION) {
      throw new Error('bad wire version');
    }
    const payloadLen = view.getUint32(20, false);
    return {
      type: view.getUint8(5),
      flags: view.getUint8(6),
      seq: view.getUint32(8, false),
      ptsUs: Number(view.getBigInt64(12, false)),
      payload: buffer.slice(HEADER_SIZE, HEADER_SIZE + payloadLen),
    };
  }

  function decodeJson(payload) {
    if (payload.byteLength === 0) {
      return {};
    }
    return JSON.parse(new TextDecoder().decode(payload));
  }

  // ----------------------------------------------------------------- state

  const state = {
    socket: null,
    audioSeq: 0,

    captureContext: null,
    captureNode: null,
    captureMix: null,
    captureConnected: false,
    remoteTracks: new Map(), // track.id -> { node, element }

    playoutContext: null,
    playoutNode: null,
    playoutStats: { underruns: 0, dropped: 0, buffered: 0 },

    canvas: null,
    canvasCtx: null,
    cameraTrack: null,
    micTrack: null,
    videoFrames: 0,

    meetState: null,
    rosterSignature: '',
    ready: false,
  };

  function send(buffer) {
    const socket = state.socket;
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(buffer);
    }
  }

  function report(event, detail) {
    // The only channel out for diagnostics. Deliberately not console.log: the
    // Python side owns logging, and a console message nobody collects is worse
    // than useless when a session fails in a headless browser.
    send(encodeJson(TYPE.PAGE_EVENT, { event, detail: detail || {} }));
  }

  function fail(code, message, fatal) {
    send(encodeJson(TYPE.ERROR, { code, message: String(message), fatal: !!fatal }));
  }

  // ------------------------------------------------------ synthetic camera

  /*
   * A canvas-backed track, driven frame for frame.
   *
   * `captureStream(0)` disables automatic sampling, so the track produces a
   * frame only when `requestFrame()` is called. That gives an exact 1:1 mapping
   * from a Python-paced frame to a published frame — the Pacer already runs the
   * media clock, and letting the canvas sample on its own timer would resample
   * that cadence and reintroduce the jitter the Pacer exists to remove.
   *
   * `drawImage` takes a WebCodecs `VideoFrame` directly, so I420 planes reach
   * the compositor with no JavaScript-side colour conversion. A
   * `MediaStreamTrackGenerator` would avoid even the blit, but it has been
   * renamed and respecified across Chromium versions; a canvas has not.
   */
  function ensureCanvas() {
    if (state.canvas) {
      return;
    }
    const canvas = document.createElement('canvas');
    canvas.width = CONFIG.videoWidth;
    canvas.height = CONFIG.videoHeight;
    state.canvas = canvas;
    state.canvasCtx = canvas.getContext('2d', { alpha: false, desynchronized: true });

    // Mid grey rather than black, so a tile that appears before the first
    // avatar frame reads as a camera warming up instead of a dead feed.
    state.canvasCtx.fillStyle = '#808080';
    state.canvasCtx.fillRect(0, 0, canvas.width, canvas.height);
  }

  function cameraTrack() {
    ensureCanvas();
    if (state.cameraTrack && state.cameraTrack.readyState === 'live') {
      // Clone per request: Meet may stop a track it was handed, and a stopped
      // master would leave every later request with a dead device.
      return state.cameraTrack.clone();
    }
    const stream = state.canvas.captureStream(0);
    state.cameraTrack = stream.getVideoTracks()[0];
    return state.cameraTrack.clone();
  }

  function drawVideoFrame(header, planes) {
    ensureCanvas();
    const ySize = header.strideY * header.height;
    const uvSize = header.strideUV * (header.height / 2);

    let frame;
    try {
      frame = new VideoFrame(planes, {
        format: 'I420',
        codedWidth: header.width,
        codedHeight: header.height,
        timestamp: header.ptsUs,
        // Explicit layout rather than an inferred one. An inferred stride that
        // is wrong produces a sheared image, which is a slow fault to identify
        // from the far side of a headless browser.
        layout: [
          { offset: 0, stride: header.strideY },
          { offset: ySize, stride: header.strideUV },
          { offset: ySize + uvSize, stride: header.strideUV },
        ],
      });
    } catch (err) {
      fail('VIDEO_FRAME', err, false);
      return;
    }

    try {
      state.canvasCtx.drawImage(frame, 0, 0, state.canvas.width, state.canvas.height);
    } finally {
      // VideoFrames hold non-GC'd media memory. Missing this leaks until the
      // renderer is killed.
      frame.close();
    }

    const track = state.cameraTrack;
    if (track && typeof track.requestFrame === 'function') {
      track.requestFrame();
    }
    state.videoFrames += 1;
  }

  // -------------------------------------------------- synthetic microphone

  async function ensurePlayout() {
    if (state.playoutContext) {
      return;
    }
    const context = new AudioContext({ sampleRate: CONFIG.publishSampleRateHz });
    await context.audioWorklet.addModule(blobUrl(WORKLETS.playout));

    const node = new AudioWorkletNode(context, 'mc-playout', {
      numberOfInputs: 0,
      numberOfOutputs: 1,
      outputChannelCount: [1],
      processorOptions: {
        capacitySamples: Math.round(CONFIG.publishSampleRateHz * CONFIG.playoutBufferSeconds),
        reportEverySamples: CONFIG.publishSampleRateHz,
      },
    });
    node.port.onmessage = (event) => {
      if (event.data && event.data.type === 'stats') {
        state.playoutStats = event.data;
      }
    };

    const destination = context.createMediaStreamDestination();
    node.connect(destination);

    // Chromium's autoplay policy can start a context suspended. The launch
    // flags disable that policy, and this is the belt to its braces: a
    // suspended context renders nothing and the microphone track would be
    // permanently silent with no error anywhere.
    if (context.state === 'suspended') {
      await context.resume();
    }

    state.playoutContext = context;
    state.playoutNode = node;
    state.micTrack = destination.stream.getAudioTracks()[0];
  }

  function microphoneTrack() {
    if (state.micTrack && state.micTrack.readyState === 'live') {
      return state.micTrack.clone();
    }
    return null;
  }

  function pushPlayoutPcm(payload) {
    const node = state.playoutNode;
    if (!node) {
      return;
    }
    const pcm = new Int16Array(payload, AUDIO_HEADER_SIZE);
    const copy = pcm.slice(0);
    node.port.postMessage(copy, [copy.buffer]);
  }

  // ------------------------------------------------------ device interception

  function blobUrl(source) {
    return URL.createObjectURL(new Blob([source], { type: 'application/javascript' }));
  }

  const FAKE_DEVICES = [
    { deviceId: 'mc-avatar-camera', kind: 'videoinput', label: 'Avatar Camera', groupId: 'mc' },
    { deviceId: 'mc-avatar-mic', kind: 'audioinput', label: 'Avatar Microphone', groupId: 'mc' },
    { deviceId: 'mc-avatar-out', kind: 'audiooutput', label: 'Avatar Output', groupId: 'mc' },
  ];

  function installDevicePatches() {
    const media = navigator.mediaDevices;
    if (!media) {
      fail('NO_MEDIA_DEVICES', 'navigator.mediaDevices is unavailable', true);
      return;
    }

    // getDisplayMedia is deliberately left alone: screen share is a real
    // capability the avatar does not provide, and faking it would make Meet
    // offer a feature that then produces a grey rectangle.
    media.getUserMedia = async (constraints) => {
      const wants = constraints || {};
      const stream = new MediaStream();
      try {
        if (wants.audio) {
          await ensurePlayout();
          const track = microphoneTrack();
          if (track) {
            stream.addTrack(track);
          }
        }
        if (wants.video) {
          stream.addTrack(cameraTrack());
        }
      } catch (err) {
        fail('GET_USER_MEDIA', err, true);
        throw err;
      }
      report('getUserMedia', {
        audio: !!wants.audio,
        video: !!wants.video,
        tracks: stream.getTracks().length,
      });
      return stream;
    };

    media.enumerateDevices = async () =>
      FAKE_DEVICES.map((device) => ({
        ...device,
        toJSON() {
          return device;
        },
      }));

    // Meet checks permissions before offering to turn the camera on. Without
    // this it renders a "camera blocked" state and never calls getUserMedia at
    // all, so the patch above would never fire.
    if (navigator.permissions && navigator.permissions.query) {
      const originalQuery = navigator.permissions.query.bind(navigator.permissions);
      navigator.permissions.query = (descriptor) => {
        const name = descriptor && descriptor.name;
        if (name === 'camera' || name === 'microphone') {
          return Promise.resolve({
            state: 'granted',
            name,
            onchange: null,
            addEventListener() {},
            removeEventListener() {},
            dispatchEvent() {
              return false;
            },
          });
        }
        return originalQuery(descriptor);
      };
    }
  }

  // ------------------------------------------------------- remote audio tap

  async function ensureCapture() {
    if (state.captureContext) {
      return;
    }
    // 16 kHz here is what removes the need for a resampler anywhere in this
    // repository: Web Audio downsamples every 48 kHz remote track into the
    // graph in native code, so the worklet's render quantum is already at the
    // avatar's fixed input rate.
    const context = new AudioContext({ sampleRate: CONFIG.captureSampleRateHz });
    await context.audioWorklet.addModule(blobUrl(WORKLETS.capture));

    const mix = context.createGain();
    mix.gain.value = 1;

    const node = new AudioWorkletNode(context, 'mc-capture', {
      numberOfInputs: 1,
      numberOfOutputs: 1,
      processorOptions: {
        frameSamples: Math.round(
          (CONFIG.captureSampleRateHz * CONFIG.captureFrameMs) / 1000
        ),
      },
    });
    node.port.onmessage = (event) => {
      const pcm = event.data;
      if (!(pcm instanceof Int16Array)) {
        return;
      }
      send(
        encodeAudio(
          pcm,
          state.audioSeq,
          Math.round(context.currentTime * 1e6),
          FLAG.MIXED
        )
      );
      state.audioSeq = (state.audioSeq + 1) >>> 0;
    };

    // Deliberately NOT connected to the worklet yet — see syncCaptureGraph().

    // A worklet whose output goes nowhere is not guaranteed to be pulled, so
    // the graph is terminated at the destination through a silent gain node.
    // Zero gain because the host has no speakers to play this to, and on a host
    // that does, playing the conference aloud would create an acoustic loop.
    const silence = context.createGain();
    silence.gain.value = 0;
    node.connect(silence);
    silence.connect(context.destination);

    if (context.state === 'suspended') {
      await context.resume();
    }

    state.captureContext = context;
    state.captureNode = node;
    state.captureMix = mix;
    syncCaptureGraph();
  }

  /*
   * Connect the mix to the capture worklet only while a remote track exists.
   *
   * A GainNode with no upstream sources still presents its output to a
   * connected worklet as a buffer of zeros, not as an absent input. So leaving
   * the mix permanently wired meant the worklet emitted a continuous stream of
   * digital silence from the moment the session started -- 50 frames a second
   * of nothing, to an avatar agent that has no one to listen to.
   *
   * That was not merely wasteful. It defeated the media watchdog outright:
   * `monitoring/watchdog.py` decides the capture graph has stalled by noticing
   * that no frames are arriving, and frames were *always* arriving. The one
   * failure the watchdog exists to catch was undetectable.
   *
   * Gating the connection makes "a frame arrived" mean "someone is in the call
   * and audible", which is what every consumer of it already assumed.
   */
  function syncCaptureGraph() {
    const mix = state.captureMix;
    const node = state.captureNode;
    if (!mix || !node) {
      return;
    }
    const wanted = state.remoteTracks.size > 0;
    if (wanted === state.captureConnected) {
      return;
    }
    try {
      if (wanted) {
        mix.connect(node);
      } else {
        mix.disconnect(node);
      }
      state.captureConnected = wanted;
    } catch (err) {
      fail('CAPTURE_GRAPH', err, false);
    }
  }

  async function attachRemoteTrack(track, stream) {
    if (track.kind !== 'audio' || state.remoteTracks.has(track.id)) {
      return;
    }
    await ensureCapture();

    const context = state.captureContext;
    const source = context.createMediaStreamSource(new MediaStream([track]));
    source.connect(state.captureMix);

    // Chromium will not pull RTP for a remote track that no sink consumes, and
    // a Web Audio source node alone does not always count as one. A muted
    // <audio> element is the reliable second sink. Muted so the host stays
    // silent; the element exists to make packets flow, not to be heard.
    const element = document.createElement('audio');
    element.muted = true;
    element.autoplay = true;
    element.srcObject = stream || new MediaStream([track]);
    element.style.display = 'none';
    document.documentElement.appendChild(element);
    element.play().catch(() => {
      /* autoplay is disabled by launch flag; a rejection here is not fatal */
    });

    state.remoteTracks.set(track.id, { source, element });
    syncCaptureGraph();
    report('remoteAudioAttached', { trackId: track.id, total: state.remoteTracks.size });

    track.addEventListener('ended', () => detachRemoteTrack(track.id));
  }

  function detachRemoteTrack(trackId) {
    const entry = state.remoteTracks.get(trackId);
    if (!entry) {
      return;
    }
    state.remoteTracks.delete(trackId);
    try {
      entry.source.disconnect();
    } catch (err) {
      /* already torn down with the context */
    }
    entry.element.srcObject = null;
    entry.element.remove();
    syncCaptureGraph();
    report('remoteAudioDetached', { trackId, total: state.remoteTracks.size });
  }

  function installPeerConnectionTap() {
    const Original = window.RTCPeerConnection;
    if (!Original) {
      fail('NO_RTC', 'RTCPeerConnection is unavailable', true);
      return;
    }

    function Patched(...args) {
      const pc = new Original(...args);
      // 'track' fires for inbound transceivers only, so our own synthetic
      // microphone can never be captured back into ingest. That is what makes
      // this tap structurally echo-free at the WebRTC layer; EchoGuard's
      // speaking gate still covers the acoustic path on a host with speakers.
      pc.addEventListener('track', (event) => {
        if (event.track && event.track.kind === 'audio') {
          attachRemoteTrack(event.track, event.streams && event.streams[0]).catch((err) =>
            fail('ATTACH_REMOTE', err, false)
          );
        }
      });
      pc.addEventListener('connectionstatechange', () =>
        report('pcState', { state: pc.connectionState })
      );
      return pc;
    }

    Patched.prototype = Original.prototype;
    Patched.generateCertificate = Original.generateCertificate;
    window.RTCPeerConnection = Patched;
    window.webkitRTCPeerConnection = Patched;
  }

  // ---------------------------------------------------- observing the page

  function matchesAny(selectors) {
    for (const selector of selectors || []) {
      try {
        if (document.querySelector(selector)) {
          return true;
        }
      } catch (err) {
        /* a malformed selector must not stop the scan */
      }
    }
    return false;
  }

  function textPresent(needles) {
    const body = document.body ? document.body.innerText || '' : '';
    const haystack = body.toLowerCase();
    return (needles || []).some((needle) => haystack.includes(needle.toLowerCase()));
  }

  function observedState() {
    const s = CONFIG.selectors;
    // Order matters and encodes precedence: a terminal condition must win over
    // "still in call", because the leave button lingers in the DOM for a beat
    // after a host removes us and the wrong answer there causes a rejoin loop
    // against a meeting that has explicitly rejected the avatar.
    if (textPresent(s.ejectedText)) {
      return 'ejected';
    }
    if (textPresent(s.deniedText)) {
      return 'denied';
    }
    if (textPresent(s.endedText)) {
      return 'ended';
    }
    if (matchesAny(s.inCall)) {
      return 'joined';
    }
    if (matchesAny(s.lobby) || textPresent(s.lobbyText)) {
      return 'lobby';
    }
    return 'joining';
  }

  function scanState() {
    const next = observedState();
    if (next === state.meetState) {
      return;
    }
    state.meetState = next;
    send(encodeJson(TYPE.MEET_STATE, { state: next, url: location.href }));
  }

  function scanRoster() {
    const s = CONFIG.selectors;
    const participants = [];
    for (const selector of s.participant || []) {
      let nodes;
      try {
        nodes = document.querySelectorAll(selector);
      } catch (err) {
        continue;
      }
      for (const node of nodes) {
        const id =
          node.getAttribute('data-participant-id') ||
          node.getAttribute('data-requested-participant-id') ||
          node.id ||
          '';
        const name = (node.getAttribute('aria-label') || node.innerText || '').trim();
        participants.push({ id, name: name.slice(0, 120) });
      }
      if (participants.length) {
        break;
      }
    }

    // A signature rather than a deep compare: the roster is rescanned on every
    // DOM mutation in a UI that mutates constantly, and re-sending an identical
    // roster would flood the channel with no new information.
    const signature = participants.map((p) => `${p.id}|${p.name}`).join(';');
    if (signature === state.rosterSignature) {
      return;
    }
    state.rosterSignature = signature;
    send(
      encodeJson(TYPE.PARTICIPANTS, {
        participants,
        selfName: CONFIG.displayName,
        count: participants.length,
      })
    );
  }

  function installObservers() {
    let scheduled = false;
    const schedule = () => {
      if (scheduled) {
        return;
      }
      scheduled = true;
      // Coalesce a burst of mutations into one scan on the next frame. Meet
      // mutates the DOM continuously, and scanning per mutation would spend
      // more time in querySelectorAll than in media.
      requestAnimationFrame(() => {
        scheduled = false;
        scanState();
        scanRoster();
      });
    };

    const start = () => {
      new MutationObserver(schedule).observe(document.documentElement, {
        childList: true,
        subtree: true,
        attributes: true,
        attributeFilter: ['aria-label', 'data-participant-id', 'jsname'],
      });
      schedule();
      // A periodic sweep as well: some Meet transitions replace text without a
      // mutation the observer is configured to see, and a missed 'ended' would
      // leave a session supervising a page that is no longer in a meeting.
      setInterval(schedule, CONFIG.scanIntervalMs);
    };

    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', start, { once: true });
    } else {
      start();
    }
  }

  // ------------------------------------------------------------- commands

  function clickFirst(selectors) {
    for (const selector of selectors || []) {
      let node;
      try {
        node = document.querySelector(selector);
      } catch (err) {
        continue;
      }
      if (node) {
        node.click();
        return true;
      }
    }
    return false;
  }

  function handleConfig(body) {
    report('configApplied', { keys: Object.keys(body || {}).length });
  }

  async function handleLeave() {
    clickFirst(CONFIG.selectors.leave);
    report('leaveRequested', {});
  }

  // ---------------------------------------------------------- the socket

  function heartbeat() {
    setInterval(() => {
      send(
        encodeJson(TYPE.HEARTBEAT, {
          sent_at_us: Math.round(performance.now() * 1000),
          video_frames: state.videoFrames,
          remote_tracks: state.remoteTracks.size,
          playout: state.playoutStats,
        })
      );
    }, CONFIG.heartbeatIntervalMs);
  }

  function connect() {
    const socket = new WebSocket(CONFIG.endpoint);
    socket.binaryType = 'arraybuffer';
    state.socket = socket;

    socket.onopen = () => {
      send(
        encodeJson(TYPE.HELLO, {
          wire_version: WIRE_VERSION,
          user_agent: navigator.userAgent,
          url: location.href,
          has_video_frame: typeof VideoFrame === 'function',
          // `'audioWorklet' in prototype`, never `prototype.audioWorklet`. WebIDL
          // attributes are accessor properties whose getters reject a non-instance
          // receiver, so reading it off the prototype throws "Illegal invocation" --
          // which, thrown from inside onopen, silently discarded this whole message.
          // The socket stayed open and heartbeats kept flowing, so the only symptom
          // was a HELLO that never arrived and no error anywhere.
          has_audio_worklet: 'audioWorklet' in (window.AudioContext || {}).prototype,
        })
      );
    };

    socket.onmessage = async (event) => {
      let message;
      try {
        message = decode(event.data);
      } catch (err) {
        fail('DECODE', err, true);
        return;
      }

      switch (message.type) {
        case TYPE.CONFIG:
          handleConfig(decodeJson(message.payload));
          try {
            await ensureCapture();
            await ensurePlayout();
            ensureCanvas();
          } catch (err) {
            fail('MEDIA_INIT', err, true);
            return;
          }
          state.ready = true;
          send(
            encodeJson(TYPE.READY, {
              capture_sample_rate_hz: state.captureContext.sampleRate,
              publish_sample_rate_hz: state.playoutContext.sampleRate,
              video_width: state.canvas.width,
              video_height: state.canvas.height,
            })
          );
          break;

        case TYPE.AUDIO_PCM:
          pushPlayoutPcm(message.payload);
          break;

        case TYPE.VIDEO_I420: {
          const view = new DataView(message.payload);
          drawVideoFrame(
            {
              width: view.getUint16(0, false),
              height: view.getUint16(2, false),
              strideY: view.getUint16(4, false),
              strideUV: view.getUint16(6, false),
              fps: view.getUint16(8, false),
              ptsUs: message.ptsUs,
            },
            new Uint8Array(message.payload, VIDEO_HEADER_SIZE)
          );
          break;
        }

        case TYPE.LEAVE:
          await handleLeave();
          break;

        case TYPE.HEARTBEAT:
          break;

        default:
          report('unexpectedMessage', { type: message.type });
      }
    };

    socket.onclose = () => {
      state.socket = null;
      // No reconnect from here. The Python side owns recovery, and a page that
      // reconnected on its own would race the bridge's own rejoin — two peers
      // both deciding to heal the same link is how you get a duplicate avatar
      // in the meeting.
    };

    socket.onerror = () => {
      /* onclose follows; nothing useful to add and nowhere to log it */
    };
  }

  // ------------------------------------------------------------ bootstrap

  installDevicePatches();
  installPeerConnectionTap();
  installObservers();
  heartbeat();
  connect();
})();
