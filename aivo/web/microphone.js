// Resample the actual capture rate to 16 kHz with a streaming area average.
// This preserves sample counts across render blocks, including at 44.1 kHz.
class MicrophoneProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / 16000;
    this.enabled = false;
    this.epoch = 0;
    this.port.onmessage = ({data}) => {
      if (data.type !== 'capture') return;
      this.enabled = data.enabled === true;
      this.epoch = data.epoch;
      this.reset();
      if (this.enabled) this.port.postMessage({type: 'ready', epoch: this.epoch});
    };
    this.reset();
  }
  reset() {
    this.remaining = this.ratio;
    this.sum = 0;
    this.frame = new Float32Array(512);
    this.position = 0;
  }
  process(inputs) {
    const input = inputs[0]?.[0];
    if (!this.enabled || !input) return true;
    for (const sample of input) {
      let available = 1;
      while (available > 1e-9) {
        const weight = Math.min(available, this.remaining);
        this.sum += sample * weight;
        this.remaining -= weight;
        available -= weight;
        if (this.remaining < 1e-9) {
          this.frame[this.position++] = Math.max(-1, Math.min(1, this.sum / this.ratio));
          this.remaining = this.ratio;
          this.sum = 0;
          if (this.position === 512) {
            this.port.postMessage({type: 'frame', epoch: this.epoch, pcm: this.frame.buffer}, [this.frame.buffer]);
            this.frame = new Float32Array(512);
            this.position = 0;
          }
        }
      }
    }
    return true;
  }
}
registerProcessor('aivo-microphone', MicrophoneProcessor);
