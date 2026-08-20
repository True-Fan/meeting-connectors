/*
 * mc-zoom-capture — the meeting's audio, out of Zoom's own playout graph and into the
 * avatar's input format.
 *
 * Runs on the Web Audio rendering thread inside an AudioContext that `inject.js`
 * constructs with `{ sampleRate: 16000 }`. That single constructor option is what makes
 * this file trivial: the browser's own graph resamples whatever Zoom is rendering — 48 kHz
 * in every observed case — down to 16 kHz before this processor sees a sample, so there is
 * no resampler here, no filter design and no aliasing to reason about. Web Audio's
 * resampler is better than anything worth writing in a worklet, and it runs in native code.
 *
 * The job that remains is float32 -> s16le, downmix to mono, and batching to a fixed frame
 * size.
 *
 * WHY THIS DOWNMIXES AND THE GOOGLE MEET EQUIVALENT DOES NOT
 * ---------------------------------------------------------
 * Meet's capture node is fed one `MediaStreamAudioSourceNode` per remote track, each of
 * which the graph presents as mono. This one is fed whatever Zoom happened to connect to
 * its destination, and Zoom's playout node is stereo. Reading `input[0]` alone would take
 * the left channel only — which is not silence, so it would never be noticed, and would
 * quietly halve the level of anything Zoom panned right.
 *
 * The mixdown is an average rather than a sum, because a sum clips whenever both channels
 * carry the same signal — which, for a mono conference upmixed to stereo, is always.
 *
 * Asymmetric scaling on the conversion is deliberate: two's-complement int16 spans
 * -32768..32767, so multiplying negatives by 0x8000 and positives by 0x7FFF uses the full
 * range without letting -1.0 wrap to positive.
 *
 * Render quanta are 128 frames, and a 20 ms frame at 16 kHz is 320 samples, so a frame
 * boundary never lines up with a quantum boundary. Hence the carry buffer: samples
 * accumulate across `process` calls and a message is posted only when a whole frame exists.
 * Emitting partial frames instead would push that arithmetic onto the Python boundary,
 * where `AudioFrame` would reject it.
 */

class McZoomCaptureProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const opts = (options && options.processorOptions) || {};
    this._frameSamples = opts.frameSamples || 320;
    this._pcm = new Int16Array(this._frameSamples);
    this._filled = 0;
  }

  process(inputs) {
    // Every tapped node is connected to this one processor, so `inputs[0]` is already the
    // sum of them — Web Audio mixes fan-in for us. A second input would mean a second
    // meeting.
    const input = inputs[0];
    // No connected source yet, or Zoom has torn its graph down. Silence is the truthful
    // output: nothing is forwarded, and the pipeline treats a gap as a gap rather than
    // needing filler.
    if (!input || input.length === 0) {
      return true;
    }
    const left = input[0];
    if (!left) {
      return true;
    }
    const right = input.length > 1 ? input[1] : null;
    const channels = right ? 2 : 1;

    for (let i = 0; i < left.length; i += 1) {
      let sample = right ? (left[i] + right[i]) / channels : left[i];
      if (sample > 1) {
        sample = 1;
      } else if (sample < -1) {
        sample = -1;
      }
      this._pcm[this._filled] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
      this._filled += 1;

      if (this._filled === this._frameSamples) {
        // Copy, then transfer the copy's buffer. Transferring `this._pcm` itself would
        // detach the buffer this processor is still writing into.
        const frame = this._pcm.slice(0);
        this.port.postMessage(frame, [frame.buffer]);
        this._filled = 0;
      }
    }

    return true;
  }
}

registerProcessor('mc-zoom-capture', McZoomCaptureProcessor);
