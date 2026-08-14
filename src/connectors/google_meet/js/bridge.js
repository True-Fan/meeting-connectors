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
    CHAT_MESSAGE: 0x0c,
    HAND_RAISE: 0x0d,
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

    // In-flight graph builds. A boolean flag assigned *after* an await cannot stop a second
    // caller entering, and two graphs mean the one Meet is attached to is the orphaned one.
    captureBuild: null,
    playoutBuild: null,

    canvas: null,
    canvasCtx: null,
    cameraTrack: null,
    micTrack: null,
    videoFrames: 0,
    cameraClones: 0,
    micClones: 0,

    // Outbound enforcement. `ourAudioTracks` is how a reconciliation pass tells a track it
    // already installed from one Meet supplied itself, without which it would replace the
    // track on every tick and renegotiate forever.
    ourAudioTracks: new Set(),
    // One clone reused for the session's outbound audio. Minting a new one per pass leaked a
    // live track every two seconds whenever Meet contested ours.
    sendTrack: null,
    peerConnections: new Set(),
    audioSendersForced: 0,
    audioSendersSeen: 0,
    forceErrors: 0,

    meetState: null,
    rosterSignature: '',
    ready: false,

    // Chat. `chatSeen` is the dedupe set: Meet re-renders the message list on almost every
    // DOM mutation, so without identity one typed question is reported on every scan.
    chatSeen: new Set(),
    chatPanelOpened: false,
    chatOpenAttempts: 0,
    chatMessagesSent: 0,
    chatBaselined: false,
    // Whether the in-call arming has happened. Chat is only reachable once admitted, so the
    // open-attempt budget is spent there rather than on the pre-join screen.
    chatWasJoined: false,
    chatGaveUp: false,
    // Wall-clock, because the retry budget is a duration rather than a number of DOM scans.
    chatArmedAt: 0,
    chatLastAttemptAt: 0,
    // When the "everything here is history" window closes. Timed from the panel opening rather
    // than from the first message, which is what stopped the first message being answered.
    chatBaselineUntil: 0,

    // Raised hands. `handsUp` is the set currently up, which is what makes this edge-triggered:
    // a hand reports once when it appears in the set and again only after it has left it.
    // Keyed by participant id where Meet gives one, by name where it does not.
    handsUp: new Set(),
    // key -> the last time that hand was actually observed on the page. What makes "still up"
    // survive a scan that could not see it: the broad sweep runs twice a second and the narrow
    // selector pass in between finds nothing in most Meet layouts, so presence is only ever
    // sampled. Membership of `handsUp` is retired from this rather than from the last scan.
    handsSeenAt: new Map(),
    // key -> last report time. A hand that flickers off and on during a re-render must not
    // interrupt the avatar twice, and Meet re-renders constantly.
    handsLastSentAt: new Map(),
    handsWasJoined: false,
    // Hands already up when we walked in are not requests to interrupt us — we were not
    // speaking yet. Timed from admission, for the reason the chat window is.
    handsBaselineUntil: 0,
    handsArmedAt: 0,
    // The label sweep is rate limited independently of the scan: it reads every labelled
    // element on the page, which is affordable twice a second and not sixty times.
    handsLastSweepAt: 0,
    // How many "nothing found" diagnostics have been emitted. Bounded, because a meeting where
    // nobody raises a hand is the normal case and must not fill the log.
    handsDiagnostics: 0,
    handRaisesSent: 0,
  };

  function send(buffer) {
    const socket = state.socket;
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(buffer);
    }
  }

  /*
   * Stage tracking.
   *
   * Bootstrap runs before the socket is open, so a stage that throws would otherwise be
   * invisible: `send()` silently drops anything written before onopen. These are buffered and
   * flushed on connect, which is what makes "the last stage that completed" recoverable after
   * a renderer crash -- the case this was added for.
   */
  const stages = [];

  function stage(name, phase, detail) {
    stages.push({ stage: name, phase, detail: detail || null, t: Math.round(performance.now()) });
    report('stage', { stage: name, phase, detail: detail || null });
    // Out-of-band escape hatch, defined only by the diagnostic harness. The buffer above can
    // only be flushed once the socket opens, so a fault during bootstrap -- or a renderer that
    // dies before connecting -- would otherwise leave no trace at all. That is exactly the
    // case this exists for. Absent in production, and wrapped because a hook that throws must
    // not be able to break the stage it is reporting on.
    const hook = window.__MC_STAGE__;
    if (typeof hook === 'function') {
      try {
        hook(name + ':' + phase + (detail ? ' ' + String(detail).slice(0, 300) : ''));
      } catch (err) {
        /* diagnostics must never affect behaviour */
      }
    }
  }

  function runStage(name, fn) {
    stage(name, 'begin');
    try {
      const result = fn();
      stage(name, 'ok');
      return result;
    } catch (err) {
      // Reported, then rethrown: bootstrap ordering matters and a failed stage must not look
      // like a successful one, but the report has to escape first.
      stage(name, 'threw', String((err && err.stack) || err));
      throw err;
    }
  }

  async function runStageAsync(name, fn) {
    stage(name, 'begin');
    try {
      const result = await fn();
      stage(name, 'ok');
      return result;
    } catch (err) {
      stage(name, 'threw', String((err && err.stack) || err));
      throw err;
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
      state.cameraClones += 1;
      return state.cameraTrack.clone();
    }
    const stream = state.canvas.captureStream(0);
    state.cameraTrack = stream.getVideoTracks()[0];
    state.cameraClones += 1;
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

  /*
   * Build the playout graph once, even under concurrent callers.
   *
   * The obvious `if (state.playoutContext) return;` guard does not hold, because the flag is
   * only assigned after two awaits. Two callers arriving in that window each build a whole
   * graph: two AudioContexts, two worklet nodes, two destination streams. The last assignment
   * wins, so `state.playoutNode` and `state.micTrack` come from the second graph -- while the
   * track Meet was already handed belongs to the first, which nothing feeds any more. The
   * avatar's first utterance plays and every one after it is silence.
   *
   * It only became likely when `superviseOutboundAudio` started calling this from an interval
   * and from three connection events; before that the callers happened to be sequential.
   * Memoising the promise makes concurrent callers await the same build, which is the property
   * the flag was pretending to have. Cleared on failure so a transient error can be retried.
   */
  function ensurePlayout() {
    if (!state.playoutBuild) {
      state.playoutBuild = buildPlayout().catch((err) => {
        state.playoutBuild = null;
        throw err;
      });
    }
    return state.playoutBuild;
  }

  async function buildPlayout() {
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
        // The depth the ring is trimmed back to through silence. The capacity above is the
        // hard ceiling that drops audio outright; this is the standing depth the buffer is
        // kept near, so latency does not ratchet up to that ceiling and stay there.
        targetSamples: Math.round(
          CONFIG.publishSampleRateHz * (CONFIG.playoutTargetSeconds || 0)
        ),
        trimBlockSamples: Math.max(Math.round(CONFIG.publishSampleRateHz * 0.005), 1),
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
      state.micClones += 1;
      const clone = state.micTrack.clone();
      state.ourAudioTracks.add(clone);
      return clone;
    }
    return null;
  }

  /*
   * Put the avatar's audio on the wire, whatever Meet decided to publish.
   *
   * `getUserMedia` interception is necessary but not sufficient. It only works if Meet asks
   * us for the microphone, at a moment when we are ready to answer, and then keeps what we
   * gave it. If Meet acquired a device before the patch installed, or asked only for video,
   * or swapped our track out during a renegotiation, the avatar is inaudible and every other
   * signal still reads healthy: PCM delivered, worklet rendering, track live, mic unmuted.
   *
   * The RTP sender is downstream of all of that and is the thing that actually decides what
   * the meeting hears, so it is what gets enforced. `replaceTrack` needs no renegotiation
   * when the kind is unchanged, so this is cheap and invisible to Meet's signalling.
   *
   * Transceivers rather than senders, because a sender whose track is null -- which is what
   * Meet has while its microphone is muted -- carries no `kind` of its own. The receiver's
   * track supplies it.
   *
   * **Only transceivers Meet intends to send on.** Direction is not a detail here. A
   * `recvonly` audio transceiver is how another participant's voice arrives in this page, and
   * attaching a send track to one flips it to `sendrecv`, which fires `negotiationneeded` and
   * makes Meet renegotiate an m-line it never meant to send on -- taking the *receive*
   * direction down with it. Doing that to every audio transceiver published the greeting once
   * and then broke the conversation in both directions at once, which is the failure this
   * filter exists to prevent.
   */
  function sendsAudio(transceiver) {
    const direction = transceiver.direction || '';
    if (direction !== 'sendrecv' && direction !== 'sendonly') {
      return false;
    }
    const sent = transceiver.sender && transceiver.sender.track;
    if (sent) {
      return sent.kind === 'audio';
    }
    // A muted Meet leaves the sender's track null, so the kind has to come from the other
    // half of the same m-line.
    const received = transceiver.receiver && transceiver.receiver.track;
    return !!received && received.kind === 'audio';
  }

  /*
   * One clone for the session, reused.
   *
   * `microphoneTrack()` mints a fresh clone per call, which is right for `getUserMedia` and
   * wrong for a loop that runs every two seconds: if Meet ever replaces our track back, a
   * new live MediaStreamTrack is created on every pass and none are ever stopped. That grows
   * without bound inside the renderer and starves the capture worklet, which is the other
   * half of why audio stopped arriving from the meeting.
   */
  async function ensureSendTrack() {
    await ensurePlayout();
    const source = state.micTrack;
    if (!source || source.readyState !== 'live') {
      return null;
    }
    if (state.sendTrack && state.sendTrack.readyState === 'live') {
      return state.sendTrack;
    }
    const clone = source.clone();
    clone.enabled = true;
    state.ourAudioTracks.add(clone);
    state.sendTrack = clone;
    return clone;
  }

  async function forceOutboundAudio(pc) {
    if (!pc || pc.signalingState === 'closed' || typeof pc.getTransceivers !== 'function') {
      return;
    }

    let seen = 0;
    for (const transceiver of pc.getTransceivers()) {
      const sender = transceiver.sender;
      if (!sender || typeof sender.replaceTrack !== 'function' || !sendsAudio(transceiver)) {
        continue;
      }
      seen += 1;

      const current = sender.track;
      // Already ours, and still usable. Replacing again would be churn.
      if (current && state.ourAudioTracks.has(current) && current.readyState === 'live') {
        continue;
      }

      const replacement = await ensureSendTrack();
      if (!replacement) {
        return;
      }
      try {
        await sender.replaceTrack(replacement);
        // Meet mutes by clearing `enabled` on the track it holds. It now holds ours, so its
        // mute button keeps working -- but a track we just installed must start audible, or
        // the avatar stays silent until someone toggles the button.
        replacement.enabled = true;
        state.audioSendersForced += 1;
        report('audioSenderForced', {
          replaced: current ? current.kind + ':' + current.id.slice(0, 8) : null,
          hadTrack: !!current,
          total: state.audioSendersForced,
        });
      } catch (err) {
        state.forceErrors += 1;
        report('audioSenderForceFailed', { error: String(err) });
      }
    }
    // Assigned, not accumulated: this is how many sending audio transceivers exist right
    // now, and a counter incremented by a loop that runs every two seconds would climb
    // forever and mean nothing.
    state.audioSendersSeen = seen;
  }

  /*
   * Keep enforcing for the connection's life.
   *
   * One pass at creation is not enough: Meet's audio transceiver does not exist until
   * negotiation, and Meet may replace the track again on a device change or an ICE restart.
   * The events cover the normal cases and the interval covers the ones they miss, which is
   * why both are here rather than either alone.
   */
  function superviseOutboundAudio(pc) {
    const pump = () => {
      forceOutboundAudio(pc).catch((err) => report('audioSenderForceFailed', {
        error: String(err),
      }));
    };

    for (const event of ['negotiationneeded', 'signalingstatechange', 'connectionstatechange']) {
      pc.addEventListener(event, pump);
    }

    const timer = setInterval(() => {
      if (pc.signalingState === 'closed') {
        clearInterval(timer);
        state.peerConnections.delete(pc);
        return;
      }
      pump();
    }, CONFIG.audioEnforceIntervalMs || 2000);

    pump();
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

  /*
   * Same single-build guarantee as `ensurePlayout`, and needed for the same reason.
   *
   * `attachRemoteTrack` calls this once per remote audio track, straight from the `track`
   * event. Two participants whose tracks arrive in the same tick both enter, both build a
   * capture graph, and the first participant's source node stays connected to a mix that is no
   * longer `state.captureMix` -- so that person is inaudible to the avatar with nothing
   * reporting a fault. This race predates the outbound work; it is the inbound half of the same
   * mistake.
   */
  function ensureCapture() {
    if (!state.captureBuild) {
      state.captureBuild = buildCapture().catch((err) => {
        state.captureBuild = null;
        throw err;
      });
    }
    return state.captureBuild;
  }

  async function buildCapture() {
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
    // Claimed before the await, not after it. The `has` check above and the `set` below used
    // to sit on either side of `ensureCapture()`, which is the same check-then-await-then-
    // assign shape as the graph builders had: two `track` events in one tick both pass the
    // check, and the track ends up with two source nodes feeding the mix — double amplitude
    // into a worklet that then clips, plus a duplicate <audio> sink. A placeholder makes the
    // claim atomic with respect to the await.
    state.remoteTracks.set(track.id, null);
    try {
      await ensureCapture();
    } catch (err) {
      state.remoteTracks.delete(track.id);
      throw err;
    }

    // The track may have ended while the graph was being built, in which case `detach` already
    // dropped our claim. Wiring it up now would leave a source node and an <audio> element for
    // a dead track that no `ended` event will ever clean up.
    if (!state.remoteTracks.has(track.id) || track.readyState === 'ended') {
      state.remoteTracks.delete(track.id);
      syncCaptureGraph();
      return;
    }

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
    if (!state.remoteTracks.has(trackId)) {
      return;
    }
    const entry = state.remoteTracks.get(trackId);
    state.remoteTracks.delete(trackId);

    // A null entry is a claim staked by `attachRemoteTrack` before its await — a track that
    // ended while its graph was still being built. There is nothing to unwire, and releasing
    // the claim is the whole job. Returning early *without* deleting, as the `!entry` guard
    // used to, left a phantom participant in `remoteTracks` for the rest of the session: the
    // capture mix could then never be disconnected and the roster count never fell to zero.
    if (!entry) {
      syncCaptureGraph();
      report('remoteAudioClaimReleased', { trackId, total: state.remoteTracks.size });
      return;
    }
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

      // The outbound half. Until this existed the tap was inbound-only, so nothing ever
      // checked what Meet was actually sending — see `forceOutboundAudio`.
      state.peerConnections.add(pc);
      superviseOutboundAudio(pc);

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

  /*
   * The page's visible text, read once.
   *
   * `innerText` is not a cheap property. It forces a synchronous layout of the whole document
   * and then serialises the rendered text out of it, and on a Meet call that is a large tree
   * that is being mutated continuously. **It is also the single most expensive thing this
   * bridge does**, and it used to be done four times per scan on a scan driven by Meet's own
   * mutations — hundreds of full-page reflows a second, on the one main thread that also
   * encodes the avatar's camera track, runs Meet's own WebRTC, and hands PCM to the playout
   * worklet. Everything downstream of that thread arrives late when it is busy, which is what
   * a listener hears as the avatar answering slowly.
   */
  function bodyText() {
    return document.body ? document.body.innerText || '' : '';
  }

  function textPresent(needles, haystack) {
    const text = (haystack || '').toLowerCase();
    return (needles || []).some((needle) => text.includes(needle.toLowerCase()));
  }

  function observedState() {
    const s = CONFIG.selectors;
    // One read, four checks. The checks and their order are unchanged; only the number of
    // times the page is laid out to answer them is.
    const text = bodyText();
    // Order matters and encodes precedence: a terminal condition must win over
    // "still in call", because the leave button lingers in the DOM for a beat
    // after a host removes us and the wrong answer there causes a rejoin loop
    // against a meeting that has explicitly rejected the avatar.
    if (textPresent(s.ejectedText, text)) {
      return 'ejected';
    }
    if (textPresent(s.deniedText, text)) {
      return 'denied';
    }
    if (textPresent(s.endedText, text)) {
      return 'ended';
    }
    if (matchesAny(s.inCall)) {
      return 'joined';
    }
    if (matchesAny(s.lobby) || textPresent(s.lobbyText, text)) {
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

  /*
   * Meeting chat, observed from the panel's DOM.
   *
   * Meet gives a participant no chat API, so the rendered panel is the only source. Two
   * consequences shape everything here:
   *
   * - **The panel must be open.** With it closed, a message flashes past as a transient popup
   *   and leaves nothing in the DOM. So this opens it, and keeps checking that it is open —
   *   Meet closes it on some layout changes. Without the click the feature reads nothing at
   *   all, which would look exactly like an avatar that ignores typed questions.
   * - **Messages already present when we arrive are history, not questions.** Opening the panel
   *   renders the whole backlog at once. Answering it would have the avatar respond to a
   *   conversation that happened before it joined, so the first scan only records ids and
   *   forwards nothing. `chatBaselined` is that one-shot.
   */
  /*
   * Find the chat button by reading its label, not by matching a fixed selector.
   *
   * The selector list is tried first because it is precise and cheap. This is the fallback, and
   * it is what a person does when the selector misses: look at every button and pick the one
   * whose accessible name mentions chat. Meet's exact label has moved more than once
   * ("Chat with everyone", "Chat", "Open chat"), and a substring match on the rendered label
   * survives all of those where an equality match survives none.
   *
   * `aria-label` first, then the tooltip Meet attaches to some controls, then the visible text.
   */
  function findChatButtonByLabel() {
    let nodes;
    try {
      nodes = document.querySelectorAll('button, div[role="button"]');
    } catch (err) {
      return null;
    }
    for (const node of nodes) {
      const label = (
        node.getAttribute('aria-label') ||
        node.getAttribute('data-tooltip') ||
        node.textContent ||
        ''
      ).toLowerCase();
      if (!label || label.indexOf('chat') === -1) {
        continue;
      }
      // "Chat with everyone" / "Chat" opens it. Skip anything that is plainly about something
      // else the word appears in, so we do not toggle a setting instead.
      if (label.indexOf('turn off') !== -1 || label.indexOf('close') !== -1) {
        continue;
      }
      return node;
    }
    return null;
  }

  function ensureChatPanel() {
    const s = CONFIG.selectors;
    if (matchesAny(s.chatPanel)) {
      if (!state.chatPanelOpened) {
        state.chatPanelOpened = true;
        report('chatPanelOpen', { attempts: state.chatOpenAttempts });
      }
      return true;
    }

    /*
     * **Bounded by time, not by scan count — and that distinction is the whole bug.**
     *
     * The budget used to be ten *scans*, and scans are driven by DOM mutations. Meet mutates
     * continuously, so ten attempts elapsed in about one and a half seconds: observed as
     * `attempt 5` through `attempt 10` inside the same second, then a permanent give-up. Meet's
     * in-call control bar does not even exist that early — the avatar had been "joined" for
     * barely a moment — so the feature gave up before the button it wanted had rendered.
     *
     * A wall-clock window with a minimum gap between clicks is what "keep trying while Meet
     * finishes drawing" actually means.
     */
    const now = Date.now();
    const windowMs = CONFIG.chatOpenWindowMs || 90000;
    const retryMs = CONFIG.chatOpenRetryMs || 1500;

    if (now - state.chatArmedAt > windowMs) {
      if (!state.chatGaveUp) {
        state.chatGaveUp = true;
        report('chatOpenGaveUp', {
          attempts: state.chatOpenAttempts,
          seconds: Math.round((now - state.chatArmedAt) / 1000),
          panelSelectors: (s.chatPanel || []).length,
          buttonSelectors: (s.chatOpenButton || []).length,
          // The labels actually on the page, so a selector fix needs no guessing next time.
          buttonsSeen: chatButtonLabels(),
        });
      }
      return false;
    }
    if (now - state.chatLastAttemptAt < retryMs) {
      return false;
    }
    state.chatLastAttemptAt = now;
    state.chatOpenAttempts += 1;

    let clicked = clickFirst(s.chatOpenButton);
    let how = clicked ? 'selector' : null;
    if (!clicked) {
      const button = findChatButtonByLabel();
      if (button) {
        try {
          button.click();
          clicked = true;
          how = 'label';
        } catch (err) {
          /* a control that refuses a synthetic click is reported as not clicked */
        }
      }
    }
    report('chatOpenAttempt', { attempt: state.chatOpenAttempts, clicked: !!clicked, how });
    // The panel is not open yet even on a successful click — it animates in, and the next
    // scan will see it.
    return false;
  }

  /*
   * Every button label containing "chat", for diagnosis.
   *
   * Emitted once, when opening is abandoned. Guessing Meet's ARIA labels from the outside cost
   * two rounds of this; reporting what the page actually has replaces the guess with a reading.
   */
  function chatButtonLabels() {
    const found = [];
    try {
      for (const node of document.querySelectorAll('button, div[role="button"]')) {
        const label = (
          node.getAttribute('aria-label') ||
          node.getAttribute('data-tooltip') ||
          ''
        ).trim();
        if (label && label.toLowerCase().indexOf('chat') !== -1) {
          found.push(label.slice(0, 80));
        }
        if (found.length >= 10) {
          break;
        }
      }
    } catch (err) {
      /* diagnostics must never throw into the scan */
    }
    return found;
  }

  function chatMessageId(node, text) {
    // Meet's own id when it exposes one, because it is stable across re-renders. Otherwise a
    // content key, which is weaker: two identical messages from the same person collapse into
    // one. That is the right trade — answering a duplicate twice is worse than missing an
    // exact repeat, and a repeat is usually somebody re-sending because we did not answer.
    //
    // The message's position was in this key and has been removed: Meet recycles list nodes, so
    // a shifted index turns an already-answered message into a new one and the avatar answers
    // it again. Position is the one part of a chat row guaranteed *not* to be stable.
    const own =
      node.getAttribute('data-message-id') ||
      node.getAttribute('data-id') ||
      '';
    if (own) {
      return own;
    }
    const sender = chatSender(node) || '';
    return `${sender}|${text}`;
  }

  function chatSender(node) {
    const s = CONFIG.selectors;
    const own = node.getAttribute('data-sender-name');
    if (own) {
      return own.trim();
    }
    for (const selector of s.chatSender || []) {
      let found;
      try {
        found = node.querySelector(selector) || (node.parentElement
          ? node.parentElement.querySelector(selector)
          : null);
      } catch (err) {
        continue;
      }
      if (found) {
        const name = (found.getAttribute('data-sender-name') || found.innerText || '').trim();
        if (name) {
          return name.slice(0, 120);
        }
      }
    }
    return null;
  }

  function scanChat() {
    if (!CONFIG.chatEnabled) {
      return;
    }

    /*
     * Only once actually admitted, and this is the bug that made chat never work at all.
     *
     * `installObservers` starts scanning at DOMContentLoaded — on the **pre-join screen**, where
     * Meet has no chat button because you cannot chat in a call you have not entered. Meet
     * mutates the DOM continuously, so the ten-attempt budget was spent within seconds of the
     * page loading, long before the avatar was admitted. By the time there was a chat button to
     * click, `ensureChatPanel` had already given up permanently.
     *
     * The budget is meant to bound attempts *in the call*, so that is where it is spent. The
     * counters reset on entry, which also covers a rejoin: a fresh call gets a fresh budget.
     */
    if (state.meetState !== 'joined') {
      state.chatWasJoined = false;
      return;
    }
    if (!state.chatWasJoined) {
      state.chatWasJoined = true;
      state.chatOpenAttempts = 0;
      state.chatGaveUp = false;
      state.chatPanelOpened = false;
      state.chatArmedAt = Date.now();
      state.chatLastAttemptAt = 0;
      state.chatBaselineUntil = 0;
      // A rejoin renders the panel's history again; re-baselining stops the avatar answering
      // messages it has already seen, and `chatSeen` still guards the individual ids.
      state.chatBaselined = false;
      report('chatArmed', {});
    }

    if (!ensureChatPanel()) {
      return;
    }

    const s = CONFIG.selectors;
    let nodes = [];
    for (const selector of s.chatMessage || []) {
      try {
        const found = document.querySelectorAll(selector);
        if (found.length) {
          nodes = Array.from(found);
          break;
        }
      } catch (err) {
        /* a malformed selector must not stop the scan */
      }
    }
    /*
     * The history window starts when the panel opens, **not when a message first appears.**
     *
     * This is what swallowed the first message anybody typed. Baselining used to mean "the
     * first scan that sees any messages is history", and the scan returned early when the panel
     * was empty — so with an empty panel the flag was never set, and the *user's first message*
     * became the thing that got baselined away. Replies only started from the second message.
     *
     * A short wall-clock window fixes both cases at once: a real backlog renders within it and
     * is correctly skipped, and an empty panel simply lets the window lapse so the first real
     * message is forwarded like any other.
     */
    if (!state.chatBaselineUntil) {
      state.chatBaselineUntil = Date.now() + (CONFIG.chatBaselineMs || 3000);
    }
    const baselining = Date.now() < state.chatBaselineUntil;

    for (const node of nodes) {
      const text = (node.innerText || node.textContent || '').trim();
      if (!text) {
        continue;
      }
      const id = chatMessageId(node, text);
      if (state.chatSeen.has(id)) {
        continue;
      }
      state.chatSeen.add(id);
      if (baselining) {
        continue; // history that predates the avatar
      }

      const sender = chatSender(node);
      send(
        encodeJson(TYPE.CHAT_MESSAGE, {
          id,
          text: text.slice(0, 4000),
          sender,
          // Compared against the name Meet shows for our own account. The bridge filters on
          // this rather than trusting it blindly, but the page is the only side that can see
          // which row is ours.
          isSelf: !!sender && !!CONFIG.displayName && sender === CONFIG.displayName,
        })
      );
      state.chatMessagesSent += 1;
    }

    // Reported once, when the window lapses — so the log shows the moment chat went live and
    // how much backlog was skipped. `history: 0` is the normal case for an empty panel, and is
    // exactly what the first-message bug never printed.
    if (!baselining && !state.chatBaselined) {
      state.chatBaselined = true;
      report('chatBaselined', { history: state.chatSeen.size });
    }
  }

  /*
   * Raised hands, read from whatever Meet happens to render.
   *
   * Meet gives a participant no hand-raise API, so this is the DOM — and unlike chat there is
   * no single place to look. The same raised hand shows up as a toast ("Dev raised their
   * hand"), as a badge on the speaker tile, and as a row in the people panel, and which of
   * those exist depends on the layout, the panel state and the participant count.
   *
   * **So this matches on wording, not on structure, and that is deliberate.** The first
   * attempt used a list of attribute selectors and found nothing in a live meeting — the same
   * failure the chat button had, fixed the same way: look at every label on the page and take
   * the one that reads like a raised hand. Class names and `jsname` attributes are build
   * artefacts that change without notice; the sentence Meet shows a human is the most durable
   * thing on the page.
   *
   * **A hand is a state, and what matters is the edge.** Meet renders it for as long as it is
   * up and re-renders constantly, so reporting what is currently raised would interrupt the
   * avatar dozens of times per hand. `handsUp` holds the current set; only entering it is
   * reported, and a hand must be lowered before it can be reported again.
   *
   * **What that set is built from is the whole difficulty**, and getting it wrong produced an
   * avatar that said "ok, go ahead" every few seconds at somebody whose hand had not moved.
   * Presence is *sampled*, not observed: the sweep that can actually find a hand runs twice a
   * second, and the scans in between see nothing. So the set is aged out by when each hand was
   * last **seen** rather than rebuilt from the latest scan — see `scanHands`.
   *
   * Nothing here decides whether a raised hand should interrupt the avatar, or what the agent
   * is told. The page reports the edge; Python decides the rest.
   */
  const HAND_TRIGGERS = [
    'raised their hand',
    'raised your hand',
    'raised a hand',
    'has their hand raised',
    'hand is raised',
    'raised hand',
    'hand raised',
  ];

  /*
   * Labels and text that contain a trigger phrase and are not somebody raising a hand.
   *
   * Checked first, and each one is a false trigger that would otherwise fire continuously:
   * "Raise hand" is the control in every meeting's toolbar — observed live as
   * `Raise hand (ctrl + ⌘ + h)`, which is why the match is a substring and not an equality —
   * "Lower hand" is what it becomes once pressed, and "Raised hands" is the people panel's
   * section heading, present for as long as the panel is open.
   */
  const HAND_EXCLUDE = ['raise hand', 'lower hand', 'lower all hands', 'raised hands'];

  /*
   * Material icon ligatures Meet draws a raised hand with.
   *
   * **This is the signal that actually exists.** A live meeting reported exactly one label
   * containing the word "hand" — the avatar's own toolbar button — while a participant's hand
   * was up. Meet marks the raised hand on the participant's tile with an icon font, and an
   * icon font glyph is a *text node* holding the glyph's name: `front_hand`. No aria-label, no
   * data attribute, nothing a selector or a label sweep can see, and the whole reason the
   * first two attempts found nothing.
   *
   * `pan_tool` is the older name and `back_hand` a sibling; all three cost nothing to check.
   */
  const HAND_ICONS = ['front_hand', 'pan_tool', 'back_hand'];

  const SELF_WORDS = ['you', 'your hand', 'you (you)'];

  function handTextMatches(text) {
    if (!text) {
      return null;
    }
    const lowered = text.toLowerCase();
    for (const phrase of HAND_EXCLUDE) {
      if (lowered.indexOf(phrase) !== -1) {
        return null;
      }
    }
    for (const phrase of HAND_TRIGGERS) {
      const at = lowered.indexOf(phrase);
      if (at !== -1) {
        return { phrase, at };
      }
    }
    return null;
  }

  /*
   * The name in a label like "Dev Choudhary raised their hand" — the label with the phrase
   * taken out, and its leftover punctuation trimmed.
   */
  function handNameFrom(text, match) {
    if (!text || !match) {
      return null;
    }
    const name = (text.slice(0, match.at) + ' ' + text.slice(match.at + match.phrase.length))
      .replace(/[(),.;:•\-–—]/g, ' ')
      .replace(/\s+/g, ' ')
      .trim();
    return name ? name.slice(0, 120) : null;
  }

  function handHolder(node) {
    try {
      return node.closest('[data-participant-id], [data-requested-participant-id]');
    } catch (err) {
      return null;
    }
  }

  /*
   * One signal, resolved to the participant it is about.
   *
   * Returns null when nothing names anybody — no participant container and no name in the
   * text. **Skipped rather than keyed as "somebody",** because an unattributable trigger that
   * reappears on every scan would interrupt the avatar forever, and a missed hand is a far
   * cheaper mistake than that.
   */
  function handResolve(node, name, how, source) {
    const holder = handHolder(node);
    if (!name && holder) {
      const label = (holder.getAttribute('aria-label') || holder.innerText || '')
        .trim()
        .split('\n')[0];
      const holderMatch = handTextMatches(label);
      name = holderMatch ? handNameFrom(label, holderMatch) : label.slice(0, 120) || null;
    }

    const id = holder
      ? holder.getAttribute('data-participant-id') ||
        holder.getAttribute('data-requested-participant-id') ||
        ''
      : '';
    const key = id || (name ? 'name:' + name.toLowerCase() : '');
    if (!key) {
      return null;
    }

    const lowered = (name || '').toLowerCase();
    const isSelf =
      SELF_WORDS.indexOf(lowered) !== -1 ||
      (source || '').toLowerCase().indexOf('raised your hand') !== -1 ||
      (!!name && !!CONFIG.displayName && name === CONFIG.displayName);
    return { key, name: name || null, isSelf, how };
  }

  function handCandidate(node, text, how) {
    const match = handTextMatches(text);
    if (!match) {
      return null;
    }
    return handResolve(node, handNameFrom(text, match), how, text);
  }

  /*
   * Walk the page's text, which is where the evidence turned out to live.
   *
   * A `TreeWalker` over text nodes rather than `querySelectorAll` plus `innerText`: it reads
   * the DOM without forcing layout, which matters because this runs beside a live media path,
   * and it reaches the icon glyphs and notification text that carry no attributes at all.
   *
   * Bounded, because an unbounded walk over a page Meet is still building is the kind of thing
   * that shows up as dropped frames rather than as an error.
   */
  function handTextWalk(visit) {
    let walker;
    try {
      walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    } catch (err) {
      return;
    }
    let seen = 0;
    let node = walker.nextNode();
    while (node && seen < 6000) {
      seen += 1;
      const raw = node.nodeValue;
      // Long runs are script bodies and inlined JSON, never a label a human reads.
      if (raw && raw.length < 200) {
        const text = raw.trim();
        if (text) {
          visit(text, node.parentElement);
        }
      }
      node = walker.nextNode();
    }
  }

  /*
   * Every element that could be carrying the news, cheapest first.
   *
   * The configured selectors run on every scan because they are precise and narrow. The rest
   * is what actually finds it, and it is rate limited: Meet mutates the DOM continuously, and
   * a full sweep per animation frame would cost more than the media path.
   */
  function handCandidates(sweep) {
    const s = CONFIG.selectors;
    const found = [];
    const seen = new Set();

    const consider = (node, text, how) => {
      if (!node || seen.has(node)) {
        return;
      }
      seen.add(node);
      const candidate = handCandidate(node, text, how);
      if (candidate) {
        found.push(candidate);
      }
    };

    for (const selector of s.handRaised || []) {
      try {
        for (const node of document.querySelectorAll(selector)) {
          const label =
            node.getAttribute('aria-label') ||
            node.getAttribute('data-tooltip') ||
            (node.innerText || '').trim().split('\n')[0];
          // A structural selector match is evidence in itself, so a node that carries no
          // readable label is still resolved through its participant holder.
          consider(node, label || 'hand raised', 'selector');
        }
      } catch (err) {
        /* a malformed selector must not stop the scan */
      }
    }

    if (!sweep) {
      return found;
    }

    try {
      for (const node of document.querySelectorAll('[aria-label], [data-tooltip]')) {
        consider(
          node,
          node.getAttribute('aria-label') || node.getAttribute('data-tooltip'),
          'label'
        );
      }
    } catch (err) {
      /* a hostile DOM must not stop the scan */
    }

    handTextWalk((text, parent) => {
      if (!parent) {
        return;
      }
      // The icon glyph on a participant's tile. **Only inside a participant container**, which
      // is what separates it from the identical glyph in our own toolbar button — that button
      // exists in every meeting and would otherwise raise a hand the moment we joined.
      if (HAND_ICONS.indexOf(text.toLowerCase()) !== -1) {
        if (seen.has(parent)) {
          return;
        }
        seen.add(parent);
        const candidate = handResolve(parent, null, 'icon', text);
        if (candidate) {
          found.push(candidate);
        }
        return;
      }
      // Meet's own announcement — the toast, and whatever the people panel renders. Plain
      // text, no attributes, invisible to every selector.
      consider(parent, text, 'text');
    });

    return found;
  }

  /*
   * What the page has that mentions a hand, for diagnosis.
   *
   * Emitted while nothing has ever been detected, a bounded number of times. The first live
   * run of this feature returned exactly one label — our own toolbar button — which is what
   * proved the signal was not in the labels at all. Reporting the *text* and the icon glyphs
   * as well is what makes the next miss diagnosable in one round instead of three.
   */
  function handDiagnostics() {
    const labels = [];
    const texts = [];
    const icons = [];
    try {
      for (const node of document.querySelectorAll('[aria-label], [data-tooltip]')) {
        const label = (
          node.getAttribute('aria-label') ||
          node.getAttribute('data-tooltip') ||
          ''
        ).trim();
        if (label && label.toLowerCase().indexOf('hand') !== -1 && labels.length < 10) {
          labels.push(label.slice(0, 80));
        }
      }
    } catch (err) {
      /* diagnostics must never throw into the scan */
    }
    try {
      handTextWalk((text, parent) => {
        const lowered = text.toLowerCase();
        if (HAND_ICONS.indexOf(lowered) !== -1 && icons.length < 10) {
          icons.push(text + (handHolder(parent || {}) ? ' [in participant]' : ' [loose]'));
        } else if (lowered.indexOf('hand') !== -1 && texts.length < 10) {
          texts.push(text.slice(0, 80));
        }
      });
    } catch (err) {
      /* as above */
    }
    let participants = 0;
    try {
      participants = document.querySelectorAll(
        '[data-participant-id], [data-requested-participant-id]'
      ).length;
    } catch (err) {
      /* as above */
    }
    return { labels, texts, icons, participants };
  }

  function scanHands() {
    if (!CONFIG.handRaiseEnabled) {
      return;
    }

    // Only in the call, for the reason chat is: the pre-join screen has no participants, and
    // arming there would spend the baseline window before anybody could raise anything.
    if (state.meetState !== 'joined') {
      state.handsWasJoined = false;
      state.handsUp.clear();
      state.handsSeenAt.clear();
      return;
    }
    if (!state.handsWasJoined) {
      state.handsWasJoined = true;
      state.handsUp.clear();
      state.handsSeenAt.clear();
      state.handsLastSentAt.clear();
      state.handsArmedAt = Date.now();
      state.handsBaselineUntil = Date.now() + (CONFIG.handRaiseBaselineMs || 3000);
      state.handsLastSweepAt = 0;
      state.handsDiagnostics = 0;
      // So a log from a live meeting shows the feature is running at all — the first question
      // to answer when nobody's raised hand produces anything.
      report('handsArmed', { selectors: (CONFIG.selectors.handRaised || []).length });
    }

    const now = Date.now();
    const sweepMs = CONFIG.handRaiseSweepMs || 500;
    const sweep = now - state.handsLastSweepAt >= sweepMs;
    if (sweep) {
      state.handsLastSweepAt = now;
    }

    const candidates = handCandidates(sweep);
    const baselining = now < state.handsBaselineUntil;
    const cooldownMs = CONFIG.handRaiseCooldownMs || 0;

    for (const candidate of candidates) {
      // Seen now, whatever happens next: this is the timestamp the retirement pass below
      // reads, and it has to be written for a hand that is merely still up as much as for one
      // that has just gone up.
      state.handsSeenAt.set(candidate.key, now);
      if (state.handsUp.has(candidate.key)) {
        continue; // already up; this is a re-render, not a new request
      }
      // Recorded before the baseline and cooldown gates rather than after, so a hand that is
      // up but deliberately not reported still counts as up. Otherwise every later scan reads
      // it as a fresh raise and the gate it just failed is the only thing holding it back —
      // which is a rate limit, not an edge.
      state.handsUp.add(candidate.key);
      if (baselining) {
        continue; // up before we arrived, so not an interruption of anything
      }
      const last = state.handsLastSentAt.get(candidate.key) || 0;
      if (cooldownMs && now - last < cooldownMs) {
        continue; // the same hand flickering, or somebody tapping it repeatedly
      }
      state.handsLastSentAt.set(candidate.key, now);
      send(
        encodeJson(TYPE.HAND_RAISE, {
          id: candidate.key,
          name: candidate.name,
          // Reported, not acted on: only the page can see whose row it is, and only Python
          // knows the name the account actually joined under.
          isSelf: candidate.isSelf,
        })
      );
      state.handRaisesSent += 1;
      report('handRaise', { name: candidate.name, how: candidate.how, self: candidate.isSelf });
    }

    /*
     * A hand comes down when it has not been *seen* for a while — not when one scan missed it.
     *
     * **This is the fix for an avatar that says "ok, go ahead" over and over at a person whose
     * hand never moved.** The set used to be replaced wholesale by whatever the current scan
     * found, and only the full sweep can find anything: the narrow selector pass that runs in
     * between matches nothing in the layouts Meet actually renders, because the evidence is in
     * text and icon glyphs rather than in attributes (see `handCandidates`). So every scan
     * between sweeps emptied the set, the next sweep re-detected the same unmoved hand as a
     * brand new one, and the only thing standing between that and a continuous interrupt was
     * the cooldown — which is why the interruptions arrived on a timer, one every cooldown, in
     * the middle of somebody typing questions into the chat.
     *
     * Retiring on a grace window instead makes the set say what it claims to say. The window
     * has to cover several sweeps, because a raised hand genuinely disappears from the DOM for
     * a moment when Meet re-renders a tile, closes the people panel, or scrolls a participant
     * out of its virtualised list — none of which is anybody lowering their hand.
     *
     * Only on a sweep, for the same reason: a partial scan is not evidence that a hand is
     * gone, so it may not be allowed to age one out.
     */
    if (sweep) {
      const graceMs = CONFIG.handRaiseDownGraceMs || 0;
      for (const key of Array.from(state.handsUp)) {
        const seenAt = state.handsSeenAt.get(key) || 0;
        if (now - seenAt >= graceMs) {
          state.handsUp.delete(key);
          state.handsSeenAt.delete(key);
        }
      }
    }

    // Nothing found yet: say what the page does have, a bounded number of times.
    const diagMs = CONFIG.handRaiseDiagMs || 0;
    if (
      diagMs &&
      state.handRaisesSent === 0 &&
      state.handsDiagnostics < 4 &&
      now - state.handsArmedAt >= diagMs * (state.handsDiagnostics + 1)
    ) {
      state.handsDiagnostics += 1;
      const diag = handDiagnostics();
      report('handRaiseNothingSeen', {
        seconds: Math.round((now - state.handsArmedAt) / 1000),
        labelsWithHand: diag.labels,
        textWithHand: diag.texts,
        iconsSeen: diag.icons,
        participantNodes: diag.participants,
      });
    }
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
        // First line only, exactly as `handSignal` already does. `innerText` on a participant
        // container is not the name — it is the name plus every control Meet renders on the
        // tile, so an unbounded read produced roster entries like "frame_person Reframe
        // visual_effects Backgrounds and effects more_vert More options for <name>". The name
        // is the first line; everything after it is the hover toolbar.
        const raw = (node.getAttribute('aria-label') || node.innerText || '').trim();
        const name = raw.split('\n')[0].trim();
        // Meet marks your own entry in the text it renders, and that is the only reliable
        // self signal the page has: `CONFIG.displayName` is what we *asked* to be called,
        // which a signed-in profile ignores in favour of the account's own name. Reported
        // rather than resolved here — the bridge decides what it means.
        const isSelf = /\(\s*you\s*\)|\byou\b\s*$/i.test(name);
        participants.push({ id, name: name.slice(0, 120), isSelf });
      }
      if (participants.length) {
        break;
      }
    }

    // A signature rather than a deep compare: the roster is rescanned on every
    // DOM mutation in a UI that mutates constantly, and re-sending an identical
    // roster would flood the channel with no new information.
    const signature = participants.map((p) => `${p.id}|${p.name}|${p.isSelf ? 1 : 0}`).join(';');
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

  /*
   * Read the page, on a floor.
   *
   * Coalescing per animation frame bounds the scan rate at Meet's own repaint rate, which
   * sounded like enough and is not: Meet mutates on essentially every frame, so the scans ran
   * at ~60 Hz for the whole call. Each pass lays the document out (`observedState`), reads
   * `innerText` off every chat row, and reads it again off every participant row — and it does
   * that on **the renderer's main thread**, the same one encoding a 720p25 camera track and
   * posting the avatar's PCM into the playout worklet. Starving that thread does not show up
   * as an error anywhere; it shows up as media arriving late, which is what "the avatar is
   * slow to answer" is.
   *
   * A floor between scans is the whole fix, and it costs nothing that matters: a quarter of a
   * second is far below what a person notices in a reply and far above what the DOM needs to
   * settle. Chat, the roster and the meeting state are all things that changed while a human
   * was typing or clicking; none of them needs sixty looks a second.
   *
   * The trailing timer is not optional. Dropping a scan that arrives inside the window would
   * mean a chat message typed during a burst of mutations waits for the *next* mutation, and
   * in a still meeting the next one may be seconds away.
   */
  function installObservers() {
    let scheduled = false;
    let lastScanAt = 0;
    let trailing = null;

    const runScans = () => {
      lastScanAt = Date.now();
      scanState();
      scanRoster();
      scanChat();
      scanHands();
    };

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
        const throttleMs = CONFIG.scanThrottleMs || 0;
        const since = Date.now() - lastScanAt;
        if (throttleMs && since < throttleMs) {
          if (trailing === null) {
            trailing = setTimeout(() => {
              trailing = null;
              runScans();
            }, throttleMs - since);
          }
          return;
        }
        if (trailing !== null) {
          clearTimeout(trailing);
          trailing = null;
        }
        runScans();
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
      // Replay everything that happened before the socket existed, so a crash during
      // bootstrap still tells Python how far it got.
      send(encodeJson(TYPE.PAGE_EVENT, { event: 'stages', detail: { stages } }));
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
            if (enabled('capture')) {
              await runStageAsync('capture', ensureCapture);
            }
            if (enabled('playout')) {
              await runStageAsync('playout', ensurePlayout);
            }
            if (enabled('canvas')) {
              runStage('canvas', ensureCanvas);
            }
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

  /*
   * A read-only snapshot of the page's own view of the media path.
   *
   * `state` is closure-private, deliberately -- nothing outside this script may mutate it. But
   * when an avatar joins a meeting and its tile stays blank, the question is *which* link
   * broke, and every counter that answers it lives in here. The heartbeat carries some of them
   * on a timer; this exposes all of them on demand, which is what a diagnostic needs.
   *
   * A getter, not the object: callers can read but cannot reach in and change anything.
   */
  window.__MC_BRIDGE_STATS__ = () => ({
    videoFramesDrawn: state.videoFrames,
    canvas: state.canvas
      ? { width: state.canvas.width, height: state.canvas.height }
      : null,
    cameraTrack: state.cameraTrack
      ? {
          readyState: state.cameraTrack.readyState,
          muted: state.cameraTrack.muted,
          enabled: state.cameraTrack.enabled,
          hasRequestFrame: typeof state.cameraTrack.requestFrame === 'function',
          settings: state.cameraTrack.getSettings ? state.cameraTrack.getSettings() : null,
        }
      : null,
    micTrack: state.micTrack
      ? { readyState: state.micTrack.readyState, enabled: state.micTrack.enabled }
      : null,
    // How many clones Meet was handed. If this is 0 the getUserMedia patch never fired, which
    // means Meet is publishing something other than our canvas.
    cameraClonesIssued: state.cameraClones,
    micClonesIssued: state.micClones,
    // The outbound truth, independent of whether Meet ever called getUserMedia:
    // how many of Meet's audio senders we saw, and how many we put our track on.
    audioSendersSeen: state.audioSendersSeen,
    audioSendersForced: state.audioSendersForced,
    peerConnections: state.peerConnections.size,
    forceErrors: state.forceErrors,
    remoteTracks: state.remoteTracks.size,
    captureConnected: state.captureConnected,
    chatPanelOpen: state.chatPanelOpened,
    chatOpenAttempts: state.chatOpenAttempts,
    chatMessagesSent: state.chatMessagesSent,
    handsUp: state.handsUp.size,
    handRaisesSent: state.handRaisesSent,
    playout: state.playoutStats,
    socketOpen: !!(state.socket && state.socket.readyState === 1),
    stages: stages.map((s) => s.stage + ':' + s.phase),
  });

  // ------------------------------------------------------------ bootstrap

  // Each stage is individually skippable, so a renderer crash can be bisected to one
  // component without editing this file. CONFIG.stages is a list of names; absent means all.
  // See MC_GOOGLE_MEET__INJECT_STAGES.
  const WANTED = CONFIG.stages || null;
  const enabled = (name) => !WANTED || WANTED.indexOf(name) !== -1;

  stage('bootstrap', 'begin', { wanted: WANTED });
  if (enabled('devices')) {
    runStage('devices', installDevicePatches);
  }
  if (enabled('rtc')) {
    runStage('rtc', installPeerConnectionTap);
  }
  if (enabled('observers')) {
    runStage('observers', installObservers);
  }
  if (enabled('heartbeat')) {
    runStage('heartbeat', heartbeat);
  }
  stage('bootstrap', 'ok');
  if (enabled('socket')) {
    /*
     * Deferred to DOMContentLoaded, and this is load-bearing rather than tidiness.
     *
     * Opening a WebSocket to loopback *synchronously from a document-start init script*
     * SIGSEGVs the Chromium renderer on a real meet.google.com document -- "Aw, Snap!",
     * error code 11. Isolated by bisecting the bootstrap stages: installing the device
     * patches, the RTCPeerConnection tap and the DOM observers are all fine, and the crash
     * lands on `socket:begin` and never reaches `socket:ok`.
     *
     * The same socket, to the same endpoint, from the same origin, opens without complaint a
     * few seconds later once Meet has initialised. So the fault is the timing, not the
     * connection: the init script runs before the document is set up, and the loopback path
     * through Chromium's Local Network Access code is evidently not ready for it.
     *
     * A renderer crash is always a browser bug, not ours -- but we can stop provoking it, and
     * waiting costs nothing: the Python side already waits up to bridge_ready_timeout_s for
     * the page to attach, and the join flow takes far longer than this deferral.
     */
    const openSocket = () => runStage('socket', connect);
    if (document.readyState === 'loading') {
      stage('socket', 'deferred', 'waiting for DOMContentLoaded');
      document.addEventListener('DOMContentLoaded', openSocket, { once: true });
    } else {
      openSocket();
    }
  }
})();
