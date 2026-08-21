/*
 * mc-playout — the avatar's PCM into a real MediaStreamTrack.
 *
 * The source end of the synthetic microphone. `inject.js` posts int16 PCM here as it
 * arrives from Python; this processor hands it to Web Audio at exactly the rate the
 * AudioContext pulls, and a `MediaStreamAudioDestinationNode` downstream turns that into
 * the audio track Teams publishes.
 *
 * A ring buffer sits between the two because the arrival cadence and the render cadence are
 * independent. The Pacer sends 20 ms frames on a media clock in another process, across a
 * socket; Web Audio pulls 128-sample quanta on the audio device's clock. Those two never
 * align, and the offset between them drifts, so decoupling them is not an optimisation — a
 * direct hand-off would glitch on every mismatch.
 *
 * Two policies, and both are about staying in real time rather than staying complete:
 *
 * - Overflow drops the OLDEST audio. Python is ahead of the sound card, which means latency
 *   is accumulating; keeping the stale end would let the avatar fall permanently behind the
 *   conversation. This mirrors the DROP_OLDEST policy the Python queues use.
 * - Underrun emits silence and never stalls. `process` returning false would tear the node
 *   out of the graph and kill the microphone track for good, so a gap in the audio is filled
 *   and counted instead.
 *
 * **What the buffer holds is latency, and it is given back through silence.** The two clocks
 * either side of this ring are independent and neither is corrected: every scheduling
 * hiccup, every burst that arrives while the main thread is busy, adds to the standing
 * depth — and with overflow at the far end of half a second as the only thing that ever
 * removed samples, the depth ratchets upward for the life of the call. A listener hears that
 * as the avatar answering late, getting later the longer the meeting runs, with nothing
 * anywhere reporting a fault.
 *
 * So the ring is trimmed back towards a target depth, but **only ever through silence**,
 * which is what makes it free. A discarded silent block shortens a pause between utterances
 * by a few milliseconds and nobody can hear it; a discarded audible block is a hole in a
 * word and everybody can. The Pacer publishes continuously, so the gaps between the avatar's
 * sentences are full of exactly the silence this needs.
 *
 * Byte-for-byte the same processor the Zoom-web and Google Meet connectors use, and
 * duplicated rather than shared for the reason `page/protocol.py` gives: an asset a page
 * loads is part of that connector's wire contract, and two connectors that must be able to
 * change their page independently cannot share one.
 */

const SILENCE_FLOOR = 512 / 0x8000;
/* Amplitude at or above which a sample counts as sound, matching `pacer.SILENCE_FLOOR`
 * on int16's scale (about -36 dBFS). Above lossy-decode noise, far below speech. */

class McPlayoutProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const opts = (options && options.processorOptions) || {};
    this._capacity = opts.capacitySamples || 24000;
    this._ring = new Float32Array(this._capacity);
    this._read = 0;
    this._write = 0;
    this._available = 0;

    // The depth to trim back towards. Deep enough to absorb the jitter between a 20 ms
    // arrival cadence and a 128-sample render quantum; shallow enough that what it costs a
    // conversation is not worth measuring. Zero disables trimming entirely.
    this._target = opts.targetSamples || 0;
    // Trimmed in whole blocks, and the size is a floor rather than a preference: speech
    // crosses zero on every cycle, so a per-sample test would cut into a vowel at the first
    // zero crossing it found. A block that is *entirely* below the floor cannot be part of a
    // voiced sound — the lowest pitch a human produces still completes half a cycle well
    // inside it.
    this._trimBlock = opts.trimBlockSamples || 240;

    this._underruns = 0;
    this._dropped = 0;
    this._trimmed = 0;
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
        this._trim();
      }
    };
  }

  /*
   * Whether the `count` oldest queued samples are all silence.
   *
   * Reads without consuming, so a block that turns out to carry speech is left exactly where
   * it was and gets played.
   */
  _silentAhead(count) {
    let index = this._read;
    for (let i = 0; i < count; i += 1) {
      const sample = this._ring[index];
      if (sample >= SILENCE_FLOOR || sample <= -SILENCE_FLOOR) {
        return false;
      }
      index = (index + 1) % this._capacity;
    }
    return true;
  }

  /*
   * Give back accumulated latency, one silent block at a time.
   *
   * Stops at the first block that carries any sound, so speech is never the thing that gets
   * shortened — a backlog behind a sentence that is still being spoken simply waits for the
   * pause after it, which is where it costs nothing.
   */
  _trim() {
    if (!this._target) {
      return;
    }
    const block = this._trimBlock;
    while (this._available - this._target >= block && this._silentAhead(block)) {
      this._read = (this._read + block) % this._capacity;
      this._available -= block;
      this._trimmed += block;
    }
  }

  _push(pcm) {
    for (let i = 0; i < pcm.length; i += 1) {
      // Symmetric inverse of the capture scaling: 0x8000 covers the negative half, so the
      // round trip through int16 is lossless at full scale.
      this._ring[this._write] = pcm[i] / 0x8000;
      this._write = (this._write + 1) % this._capacity;

      if (this._available < this._capacity) {
        this._available += 1;
      } else {
        // Full: the write just overwrote the oldest unread sample, so advance the read
        // cursor to match and count the loss.
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

    // Mono in, mono out. Anything Teams wants in stereo it can upmix itself.
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
        trimmed: this._trimmed,
        buffered: this._available,
      });
    }

    return true;
  }
}

registerProcessor('mc-playout', McPlayoutProcessor);
