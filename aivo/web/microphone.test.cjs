const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const test = require('node:test');

function harness(rate) {
  let Processor;
  const messages = [], frames = [];
  const sandbox = {
    sampleRate: rate,
    AudioWorkletProcessor: class { constructor() { this.port = {postMessage: (message) => {
      messages.push(message);
      if (message.type === 'frame') frames.push(new Float32Array(message.pcm));
    }}; } },
    registerProcessor: (_, implementation) => { Processor = implementation; },
  };
  vm.runInNewContext(fs.readFileSync(`${__dirname}/microphone.js`, 'utf8'), sandbox);
  const processor = new Processor();
  const gate = (enabled, epoch) => processor.port.onmessage({data: {type: 'capture', enabled, epoch}});
  return {processor, gate, messages, frames};
}
for (const rate of [16000, 44100, 48000]) {
  test(`capture produces 16 kHz PCM at ${rate} Hz, preserving fractional block boundaries`, () => {
    const {processor, gate, frames} = harness(rate);
    gate(true, 1);
    let samples = 0;
    while (samples < rate) {
      const block = new Float32Array(Math.min(128, rate - samples)).fill(.25);
      assert.equal(processor.process([[block]]), true);
      samples += block.length;
    }
    assert.equal(frames.length, 31);
    assert.equal(processor.position, 128);
    for (const frame of frames) {
      assert.equal(frame.length, 512);
      assert.ok(frame.every((sample) => Math.abs(sample - .25) < 1e-6));
    }
    assert.equal(processor.process([]), true);
    processor.process([[new Float32Array(rate).fill(2)]]);
    assert.ok(frames.at(-1).every((sample) => sample === 1));
  });
}

test('capture is disabled by default and discards speaker audio and partial resampler state across turns', () => {
  const {processor, gate, frames, messages} = harness(48000);
  processor.process([[new Float32Array(48000).fill(-.8)]]);
  assert.equal(messages.length, 0, 'startup audio is not captured');
  gate(true, 1);
  processor.process([[new Float32Array(257).fill(-.8)]]);
  assert.ok(processor.position > 0, 'a partially accumulated frame exists');
  gate(false, 2);
  processor.process([[new Float32Array(48000).fill(-.8)]]);
  assert.equal(frames.length, 0, 'AI playback while disabled creates no frames');
  gate(true, 3);
  processor.process([[new Float32Array(1536).fill(.25)]]);
  assert.equal(frames.length, 1);
  assert.ok(frames[0].every((sample) => Math.abs(sample - .25) < 1e-6), 'resumed input contains no samples from the previous turn');
  assert.deepEqual(messages.map((message) => [message.type, message.epoch]), [['ready', 1], ['ready', 3], ['frame', 3]]);
});
