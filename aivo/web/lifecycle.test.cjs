const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const test = require('node:test');
const flush = () => new Promise(setImmediate);

function harness({deferred = false} = {}) {
  const nodes = new Map(), timers = new Map(), sent = [], sources = [], decodes = [];
  let nextTimer = 0;
  class Element {
    constructor() { this.dataset = {}; this.children = []; this.options = []; this.value = ''; }
    addEventListener() {}
    replaceChildren(...children) { this.children = children; this.options = children; }
    append(...children) { this.children.push(...children); }
    add(option) { this.options.push(option); }
    remove() {}
  }
  const audioContext = {
    destination: {}, resume: async () => {}, close: async () => {},
    decodeAudioData(data) {
      const buffer = {id: new Uint8Array(data)[0]};
      if (!deferred) return Promise.resolve(buffer);
      return new Promise((resolve) => decodes.push(() => resolve(buffer)));
    },
    createBufferSource() {
      const source = {connect() {}, disconnect() {}, stop() { this.stopped = true; }, start() { this.started = true; }};
      sources.push(source); return source;
    },
  };
  const sandbox = {
    document: {getElementById: (id) => { if (!nodes.has(id)) nodes.set(id, new Element()); return nodes.get(id); }, createElement: () => new Element()},
    window: {addEventListener() {}, AudioWorkletNode: class {}},
    navigator: {mediaDevices: {enumerateDevices: async () => [], addEventListener() {}, getUserMedia: () => new Promise(() => {})}},
    Option: class { constructor(text, value) { this.text = text; this.value = value; } },
    fetch: () => new Promise(() => {}), AbortSignal: {timeout() {}}, WebSocket: {OPEN: 1},
    location: {protocol: 'http:', host: 'localhost:8000'},
    setTimeout: (fn) => { timers.set(++nextTimer, fn); return nextTimer; }, clearTimeout: (id) => timers.delete(id),
    performance: {now: () => 100}, atob: (text) => Buffer.from(text, 'base64').toString('binary'),
    testSocket: {readyState: 1, send: (raw) => sent.push(JSON.parse(raw)), close() { throw new Error('Old stop timer closed the connection'); }},
    testAudioContext: audioContext,
  };
  const ctx = vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(`${__dirname}/app.js`, 'utf8'), ctx);
  vm.runInContext('socket = testSocket; context = testAudioContext; running = accepting = true; turn = 7;', ctx);
  const receive = (message) => {
    sandbox.testMessage = {turn_id: 7, ...message};
    return vm.runInContext('receive(testMessage, socket)', ctx);
  };
  const audio = (id) => ({type: 'audio', wav: Buffer.from([id]).toString('base64'), chunk_index: id - 1, final: false});
  return {ctx, nodes, timers, sent, sources, decodes, receive, audio, sandbox};
}

test('restart ignores a stopped turn until the server acknowledgement and cancels its timer', async () => {
  const h = harness();
  vm.runInContext('stop(); void start();', h.ctx);
  assert.equal(h.timers.size, 1);
  await h.receive({type: 'transcript', role: 'assistant', text: 'stale'});
  await h.receive({type: 'audio', wav: 'invalid'});
  assert.equal(h.nodes.get('transcript')?.children.length ?? 0, 0);
  assert.equal(vm.runInContext('state', h.ctx), 'Preparing');
  await h.receive({type: 'state', state: 'Idle', turn_id: 8});
  assert.equal(h.timers.size, 0);
  assert.equal(vm.runInContext('turn', h.ctx), 8);
});

test('chunks decode and play in order; early audio_end waits for both decode and playback to drain', async () => {
  const h = harness({deferred: true});
  const first = h.receive(h.audio(1));
  const second = h.receive(h.audio(2));
  await h.receive({type: 'audio_end', metrics: {server_ms: 90}});
  assert.equal(h.sent.length, 0);
  await flush();
  assert.equal(h.decodes.length, 1, 'decode work is serialized, even if chunk events arrive together');
  h.decodes.shift()(); await first; await flush();
  assert.equal(h.sources.length, 1);
  assert.equal(h.sources[0].buffer.id, 1);
  h.decodes.shift()(); await second;
  assert.equal(h.sources.length, 1, 'decoded chunks never overlap playback');
  assert.equal(h.sent.length, 0);
  h.sources[0].onended();
  assert.equal(h.sources.length, 2);
  assert.equal(h.sources[1].buffer.id, 2);
  assert.equal(h.sent.length, 0, 'finishing one chunk never acknowledges the whole turn');
  h.sources[1].onended();
  assert.deepEqual(h.sent, [{type: 'played', turn_id: 7}]);
  await h.receive({type: 'audio_end'});
  assert.equal(h.sent.length, 1, 'duplicate end messages do not duplicate acknowledgements');
});

test('a gap after a chunk remains Speaking and is not acknowledged before audio_end', async () => {
  const h = harness();
  await h.receive(h.audio(1));
  h.sources[0].onended();
  assert.equal(h.sent.length, 0);
  assert.equal(vm.runInContext('state', h.ctx), 'Speaking');
  await h.receive(h.audio(2));
  h.sources[1].onended();
  assert.equal(h.sent.length, 0);
  await h.receive({type: 'audio_end'});
  assert.deepEqual(h.sent, [{type: 'played', turn_id: 7}]);
});

test('stop drops pending decode, queued playback and an already queued old onended callback', async () => {
  const h = harness({deferred: true});
  const first = h.receive(h.audio(1)); await flush();
  h.decodes.shift()(); await first;
  const staleEnded = h.sources[0].onended;
  const second = h.receive(h.audio(2)); await flush();
  await h.receive({type: 'audio_end'});
  vm.runInContext('stop()', h.ctx);
  assert.equal(h.sources[0].stopped, true);
  assert.equal(h.sources[0].onended, null);
  h.decodes.shift()(); await second;
  staleEnded();
  assert.equal(h.sources.length, 1, 'late decode cannot start a source after stop');
  assert.deepEqual(h.sent, [{type: 'stop'}], 'late callbacks cannot acknowledge a cancelled turn');
});

test('legacy whole-response audio is acknowledged after playback', async () => {
  const h = harness();
  await h.receive({type: 'audio', wav: Buffer.from([1]).toString('base64')});
  assert.equal(h.sent.length, 0);
  h.sources[0].onended();
  assert.deepEqual(h.sent, [{type: 'played', turn_id: 7}]);
});

test('warmup failure stops microphone state and server Idle acknowledges cleanup', async () => {
  const h = harness();
  await h.receive({type: 'state', state: 'Preparing'});
  await h.receive({type: 'error', message: 'Warmup failed'});
  assert.equal(vm.runInContext('running', h.ctx), false);
  assert.equal(vm.runInContext('context', h.ctx), null);
  assert.equal(vm.runInContext('state', h.ctx), 'Idle');
  assert.deepEqual(h.sent, [{type: 'stop'}]);
  await h.receive({type: 'state', state: 'Idle', turn_id: 8});
  assert.equal(h.timers.size, 0);
});

test('configuration displays actual precision and GPU allocation; first-audio timing is a server estimate', async () => {
  const h = harness();
  h.sandbox.fetch = async () => ({ok: true, json: async () => ({
    ready: true, stt: true, ollama: true, voicevox: true, voice_interruption: true, response_protocol: 'final-json-v1', runtime_id: 'abc12345',
    stt_model: 'whisper', stt_device: 'auto', actual_stt_device: 'cuda', actual_stt_compute: 'int8_float16',
    llm_model: 'qwen', quantization: 'Q4_K_M', gpu_loaded: true, size_vram: 1073741824, speaker: 3,
  })});
  await vm.runInContext('refresh()', h.ctx);
  const configuration = h.nodes.get('configuration').textContent;
  assert.match(configuration, /cuda \/ int8_float16/);
  assert.match(configuration, /Q4_K_M/);
  assert.match(configuration, /1024 MiB/);
  assert.match(configuration, /final-json-v1/);
  assert.match(configuration, /abc12345/);
  vm.runInContext('showMetrics({first_audio_ready_ms: 185}, 4)', h.ctx);
  const metricNodes = h.nodes.get('metrics').children;
  const timingIndex = metricNodes.findIndex((node) => node.textContent.includes('サーバー概算'));
  assert.ok(timingIndex >= 0);
  assert.equal(metricNodes[timingIndex + 1].textContent, '185 ms');
});
test('microphone resume rejects queued old frames until the current worklet readiness acknowledgement', async () => {
  const h = harness();
  h.sandbox.captureCommands = [];
  h.sandbox.capturedFrames = [];
  vm.runInContext(`capture = {port: {postMessage: (message) => captureCommands.push(message)}, disconnect() {}};
    socket.send = (frame) => capturedFrames.push(frame);
    showState('Processing'); showState('Listening');`, h.ctx);
  const currentEpoch = vm.runInContext('captureEpoch', h.ctx);
  assert.equal(h.nodes.get('state').textContent, 'マイクを再開しています');
  assert.equal(h.sandbox.captureCommands.length, 1);
  assert.equal(h.sandbox.captureCommands[0].enabled, true);
  h.sandbox.workletEpoch = currentEpoch;
  vm.runInContext(`handleCapture({type:'frame', epoch:workletEpoch - 1, pcm:'old'});
    handleCapture({type:'ready', epoch:workletEpoch - 1});
    handleCapture({type:'frame', epoch:workletEpoch, pcm:'too-early'});`, h.ctx);
  assert.equal(h.sandbox.capturedFrames.length, 0);
  vm.runInContext(`handleCapture({type:'ready', epoch:workletEpoch});
    handleCapture({type:'frame', epoch:workletEpoch, pcm:'new'});`, h.ctx);
  assert.deepEqual(h.sandbox.capturedFrames, ['new']);
  assert.equal(h.nodes.get('state').textContent, '聞いています');
  assert.equal(h.sandbox.captureCommands.length, 1, 'ready acknowledgement does not create another reset loop');
});

test('legacy server without final response validation is clearly identified and cannot start a conversation', async () => {
  const h = harness();
  vm.runInContext('running = false', h.ctx);
  h.sandbox.fetch = async () => ({ok: true, json: async () => ({
    ready: true, stt: true, ollama: true, voicevox: true,
    stt_model: 'whisper', stt_device: 'cpu', llm_model: 'qwen', speaker: 3,
  })});
  await vm.runInContext('refresh()', h.ctx);
  assert.equal(vm.runInContext('ready', h.ctx), false);
  assert.equal(h.nodes.get('start').disabled, true);
  assert.equal(h.nodes.get('error').hidden, false);
  assert.match(h.nodes.get('error').textContent, /旧バージョン：サーバーの再起動が必要/);
  assert.ok(h.nodes.get('services').children.some((node) => node.textContent.includes('旧バージョン')));
  h.sandbox.fetch = async () => ({ok: true, json: async () => ({ready: true,
    response_protocol: 'final-json-v1', runtime_id: 'reloaded', stt: true, ollama: true, voicevox: true,
  })});
  await vm.runInContext('refresh()', h.ctx);
  assert.equal(vm.runInContext('ready', h.ctx), true);
  assert.equal(h.nodes.get('start').disabled, false);
  assert.equal(h.nodes.get('error').hidden, true, 'successful new server verification clears only the stale-version warning');
});

test('voice interruption stops playback and queued decodes without acknowledging old turn or resetting the live mic', async () => {
  const h = harness({deferred: true});
  h.sandbox.captureCommands = [];
  h.sandbox.capturedFrames = [];
  vm.runInContext(`voiceInterruption = true;
    capture = {port: {postMessage: (message) => captureCommands.push(message)}, disconnect() {}};
    const baseSend = socket.send;
    socket.send = (frame) => typeof frame === 'string' && frame.startsWith('{') ? baseSend(frame) : capturedFrames.push(frame);
    showState('Listening');
    handleCapture({type:'ready', epoch:captureEpoch});`, h.ctx);
  const captureEpoch = vm.runInContext('captureEpoch', h.ctx);
  const first = h.receive(h.audio(1)); await flush();
  h.decodes.shift()(); await first;
  assert.match(h.nodes.get('hint').textContent, /話しかけると読み上げを止めます/);
  vm.runInContext(`handleCapture({type:'frame', epoch:captureEpoch, pcm:'while-speaking'})`, h.ctx);
  assert.deepEqual(h.sandbox.capturedFrames, ['while-speaking'], 'microphone stays live while AI speaks');
  const staleEnded = h.sources[0].onended;
  const second = h.receive(h.audio(2)); await flush();
  await h.receive({type: 'audio_end'});
  await h.receive({type: 'interrupted', turn_id: 8, interrupted_turn_id: 7});
  assert.equal(h.sources[0].stopped, true);
  assert.equal(h.sources[0].onended, null);
  assert.equal(vm.runInContext('state', h.ctx), 'Listening');
  assert.equal(vm.runInContext('captureEpoch', h.ctx), captureEpoch);
  assert.equal(vm.runInContext('captureReady', h.ctx), true);
  assert.equal(h.sandbox.captureCommands.length, 1, 'Speaking to Listening preserves the current user audio buffers');
  await h.receive(h.audio(3)); // Late PCM from the interrupted server turn.
  await h.receive({type: 'audio_end'});
  h.decodes.shift()(); await second;
  staleEnded();
  assert.equal(h.sources.length, 1, 'queued and late old audio never play');
  assert.deepEqual(h.sent, [], 'interrupted turns are never acknowledged as fully played');
  vm.runInContext(`handleCapture({type:'frame', epoch:captureEpoch, pcm:'continuing-user'})`, h.ctx);
  assert.deepEqual(h.sandbox.capturedFrames, ['while-speaking', 'continuing-user']);
  assert.equal(vm.runInContext('running && accepting && context !== null', h.ctx), true);
});


test('lookup status shows actual request result and ignores stale turns', async () => {
  const h = harness();
  await h.receive({type: 'lookup_status', text: '外部リクエスト 3件：取得完了'});
  assert.equal(h.nodes.get('lookup-status').textContent, '外部リクエスト 3件：取得完了');
  assert.equal(h.nodes.get('lookup-status').hidden, false);
  await h.receive({type: 'lookup_status', text: 'stale', turn_id: 6});
  assert.equal(h.nodes.get('lookup-status').textContent, '外部リクエスト 3件：取得完了');
});
