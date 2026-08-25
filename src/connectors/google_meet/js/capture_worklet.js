/*
 * mc-capture — conference audio to the avatar's input format.
 *
 * Runs on the Web Audio rendering thread inside an AudioContext that
 * `bridge.js` constructs with `{ sampleRate: 16000 }`. That single constructor
 * option is what makes this file trivial: the browser's own graph resamples
 * every remote 48 kHz track down to 16 kHz before this processor ever sees a
 * sample, so there is no resampler here, no filter design, and no aliasing to
 * reason about. Web Audio's resampler is better than anything worth writing in
 * a worklet, and it runs in native code.
 *
 * The job that remains is float32 -> s16le and batching to a fixed frame size.
 *
 * Asymmetric scaling on the conversion is deliberate: two's-complement int16
 * spans -32768..32767, so multiplying negatives by 0x8000 and positives by
 * 0x7FFF uses the full range without letting -1.0 wrap to positive.
 *
 * Render quanta are 128 frames, and a 20 ms frame at 16 kHz is 320 samples, so
 * a frame boundary never lines up with a quantum boundary. Hence the carry
 * buffer: samples accumulate across `process` calls and a message is posted
 * only when a whole frame exists. Emitting partial frames instead would push
 * that arithmetic onto the Python boundary, where `AudioFrame` would reject it.
 */

class McCaptureProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const opts = (options && options.processorOptions) || {};
    this._frameSamples = opts.frameSamples || 320;
    this._pcm = new Int16Array(this._frameSamples);
    this._filled = 0;
  }

  process(inputs) {
    const input = inputs[0];
    // No connected source yet, or every remote track has ended. Silence is the
    // truthful output: nobody is speaking, so nothing is forwarded. The avatar
    // pipeline treats a gap as a gap rather than needing filler.
    if (!input || input.length === 0) {
      return true;
    }
    const channel = input[0];
    if (!channel) {
      return true;
    }

    for (let i = 0; i < channel.length; i += 1) {
      let sample = channel[i];
      if (sample > 1) {
        sample = 1;
      } else if (sample < -1) {
        sample = -1;
      }
      this._pcm[this._filled] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
      this._filled += 1;

      if (this._filled === this._frameSamples) {
        // Copy, then transfer the copy's buffer. Transferring `this._pcm`
        // itself would detach the buffer this processor is still writing into.
        const frame = this._pcm.slice(0);
        this.port.postMessage(frame, [frame.buffer]);
        this._filled = 0;
      }
    }

    return true;
  }
}

registerProcessor('mc-capture', McCaptureProcessor);
