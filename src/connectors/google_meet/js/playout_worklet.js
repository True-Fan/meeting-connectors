/*
 * mc-playout — the avatar's PCM into a real MediaStreamTrack.
 *
 * The source end of the synthetic microphone. `bridge.js` posts int16 PCM here
 * as it arrives from Python; this processor hands it to Web Audio at exactly
 * the rate the AudioContext pulls, and a `MediaStreamAudioDestinationNode`
 * downstream turns that into the audio track Meet publishes.
 *
 * A ring buffer sits between the two because the arrival cadence and the render
 * cadence are independent. The Pacer sends 20 ms frames on a media clock in
 * another process, across a socket; Web Audio pulls 128-sample quanta on the
 * audio device's clock. Those two never align, and the offset between them
 * drifts, so decoupling them is not an optimisation — a direct hand-off would
 * glitch on every mismatch.
 *
 * Two policies, and both are about staying in real time rather than staying
 * complete:
 *
 * - Overflow drops the OLDEST audio. Python is ahead of the sound card, which
 *   means latency is accumulating; keeping the stale end would let the avatar
 *   fall permanently behind the conversation. This mirrors the DROP_OLDEST
 *   policy the Python queues use, for the same reason.
 * - Underrun emits silence and never stalls. `process` returning false would
 *   tear the node out of the graph and kill the microphone track for good, so
 *   a gap in the audio is filled and counted instead.
 *
 * Both counters are reported upward so the Python side can see them: this
 * processor makes no judgement about whether they matter.
 */

class McPlayoutProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const opts = (options && options.processorOptions) || {};
    this._capacity = opts.capacitySamples || 24000;
    this._ring = new Float32Array(this._capacity);
    this._read = 0;
    this._write = 0;
    this._available = 0;

    this._underruns = 0;
    this._dropped = 0;
    this._reportEvery = opts.reportEverySamples || 48000;
    this._sinceReport = 0;

    this.port.onmessage = (event) => {
      const data = event.data;
      if (data && data.type === 'reset') {
        this._read = 0;
        this._write = 0;
        this._available = 0;
        return;
      }
      if (data instanceof Int16Array) {
        this._push(data);
      }
    };
  }

  _push(pcm) {
    for (let i = 0; i < pcm.length; i += 1) {
      // Symmetric inverse of the capture scaling: 0x8000 covers the negative
      // half, so the round trip through int16 is lossless at full scale.
      this._ring[this._write] = pcm[i] / 0x8000;
      this._write = (this._write + 1) % this._capacity;

      if (this._available < this._capacity) {
        this._available += 1;
      } else {
        // Full: the write just overwrote the oldest unread sample, so advance
        // the read cursor to match and count the loss.
        this._read = (this._read + 1) % this._capacity;
        this._dropped += 1;
      }
    }
  }

  process(_inputs, outputs) {
    const output = outputs[0];
    if (!output || output.length === 0) {
      return true;
    }
    const channel = output[0];

    for (let i = 0; i < channel.length; i += 1) {
      if (this._available > 0) {
        channel[i] = this._ring[this._read];
        this._read = (this._read + 1) % this._capacity;
        this._available -= 1;
      } else {
        channel[i] = 0;
        this._underruns += 1;
      }
    }

    // Mono in, mono out. Anything Meet wants in stereo it can upmix itself.
    for (let c = 1; c < output.length; c += 1) {
      output[c].set(channel);
    }

    this._sinceReport += channel.length;
    if (this._sinceReport >= this._reportEvery) {
      this._sinceReport = 0;
      this.port.postMessage({
        type: 'stats',
        underruns: this._underruns,
        dropped: this._dropped,
        buffered: this._available,
      });
    }

    return true;
  }
}

registerProcessor('mc-playout', McPlayoutProcessor);
