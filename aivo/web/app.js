const $ = (id) => document.getElementById(id);
const states = {
  Idle: ['会話をはじめましょう', '開始すると、マイクの使用許可を求めます。'],
  Preparing: ['マイクを準備しています', '初回はモデルの読み込みに時間がかかる場合があります。'],
  Listening: ['聞いています', '話し終わったら、少し間をあけてください。'],
  Processing: ['考えています', 'このPCで、声を認識して回答を作っています。'],
  Speaking: ['aivoが話しています', '話しかけると読み上げを止めます。'],
};
let socket, stream, context, capture, input, playback, audioSession;
let state = 'Idle', running = false, accepting = false, generation = 0, turn = 0, ready = false;
let stopTimer;
let ttsBackend = 'voicevox';
let speechTest;
let protocolWarning = false;
let voiceInterruption = false, interruptionSupported = false;
let captureEpoch = 0, captureReady = false;
let stopAcknowledgement = Promise.resolve(), acknowledgeStop = null;

function capturesDuring(value) {
  return value === 'Listening' || (value === 'Speaking' && voiceInterruption);
}
function updateVoiceInterruption() {
  const wasCapturing = capturesDuring(state);
  voiceInterruption = interruptionSupported;
  if (capture && wasCapturing !== capturesDuring(state)) {
    captureReady = false;
    capture.port.postMessage({type: 'capture', enabled: capturesDuring(state), epoch: ++captureEpoch});
  }
  showState(state);
}
function showState(value) {
  const captureChanged = capturesDuring(state) !== capturesDuring(value);
  const warmedUp = state === 'Preparing' && value === 'Listening';
  if (captureChanged && capture) {
    captureReady = false;
    capture.port.postMessage({type: 'capture', enabled: capturesDuring(value), epoch: ++captureEpoch});
  }
  state = value;
  $('state').textContent = value === 'Listening' && capture && !captureReady
    ? 'マイクを再開しています' : states[value][0];
  $('hint').textContent = states[value][1];
  $('orb').dataset.state = value;
  $('start').disabled = running || !ready;
  $('stop').disabled = !running;
  $('test-speech').disabled = running || ttsBackend !== 'browser';
  $('microphone').disabled = running;
  if (warmedUp) void refresh();
}
function error(message) {
  $('error').textContent = message;
  $('error').hidden = !message;
}
function command(type, extra = {}) {
  if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({type, ...extra}));
}
function handleCapture(message) {
  if (!message || message.epoch !== captureEpoch || !capturesDuring(state) || !running || !accepting) return;
  if (message.type === 'ready') {
    captureReady = true;
    showState(state);
    return;
  }
  if (message.type !== 'frame' || !captureReady || socket?.readyState !== WebSocket.OPEN) return;
  if (socket.bufferedAmount > 1_000_000) {
    stop(); error('音声の送信が追いつかなくなりました。再開してください。'); return;
  }
  socket.send(message.pcm);
}
function releaseAudio() {
  clearPlayback();
  captureReady = false;
  captureEpoch++;
  if (capture) capture.port.onmessage = null;
  capture?.disconnect(); input?.disconnect();
  capture = input = null;
  stream?.getTracks().forEach((track) => track.stop());
  stream = null;
  if (context) { void context.close().catch(() => {}); context = null; }
}
function stop() {
  generation++;
  running = accepting = false;
  releaseAudio();
  showState('Idle');
  if (socket?.readyState === WebSocket.OPEN) {
    // Wait for the server's Idle acknowledgement before allowing a restart.
    if (!acknowledgeStop) {
      stopAcknowledgement = new Promise((resolve) => { acknowledgeStop = resolve; });
      command('stop');
      const stoppedSocket = socket;
      stopTimer = setTimeout(() => {
        if (acknowledgeStop && socket === stoppedSocket) stoppedSocket.close();
      }, 3000);
    }
  }
}
function finishStop() {
  clearTimeout(stopTimer);
  stopTimer = undefined;
  acknowledgeStop?.();
  acknowledgeStop = null;
}
function addMessage(role, text) {
  $('empty')?.remove();
  const row = document.createElement('article');
  row.className = `message ${role === 'assistant' ? 'assistant' : 'user'}`;
  const avatar = document.createElement('div'); avatar.className = 'avatar';
  avatar.textContent = role === 'assistant' ? 'ai' : '私';
  const body = document.createElement('div'); body.className = 'message-body';
  const speaker = document.createElement('p'); speaker.className = 'speaker';
  speaker.textContent = role === 'assistant' ? 'aivo' : 'あなた';
  const content = document.createElement('p'); content.className = 'message-text'; content.textContent = text;
  body.append(speaker, content); row.append(avatar, body); $('transcript').append(row);
  $('transcript').scrollTop = $('transcript').scrollHeight;
}
function addSources(sources) {
  const body = $('transcript').lastElementChild?.querySelector('.message-body');
  if (!body) return;
  const list = document.createElement('p'); list.className = 'sources';
  list.append('参照元: ');
  for (const source of sources.slice(0, 3)) {
    try {
      const url = new URL(source.url);
      if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password) continue;
      const link = document.createElement('a');
      link.href = url.href; link.textContent = source.title;
      link.target = '_blank'; link.rel = 'noopener noreferrer';
      list.append(link, ' ');
    } catch { /* Ignore malformed provider links. */ }
  }
  body.append(list);
}
function showMetrics(metrics, playbackMs) {
  const entries = [
    ['VAD 終端待機（設定値）', metrics.vad_ms], ['音声認識', metrics.stt_ms],
    ['LLM 最初のトークン', metrics.ttft_ms], ['LLM 全文生成', metrics.llm_ms],
    [metrics.native_tts ? '返答テキスト準備（サーバー概算）' : '最初の音声準備（サーバー概算）', metrics.first_audio_ready_ms],
    [metrics.native_tts ? '端末読み上げ（サーバー計測外）' : '音声合成', metrics.tts_ms], ['サーバー処理（概算）', metrics.server_ms],
    ['音声受信 → 再生予約', playbackMs],
  ];
  $('metrics').replaceChildren();
  for (const [label, value] of entries) {
    const dt = document.createElement('dt'), dd = document.createElement('dd');
    dt.textContent = label; dd.textContent = Number.isFinite(value) ? `${Math.round(value)} ms` : '—';
    $('metrics').append(dt, dd);
  }
}
function clearPlayback() {
  const session = audioSession;
  audioSession = null;
  clearTimeout(session?.speechTimer);
  clearTimeout(session?.completionTimer);
  if (session?.utterance) {
    session.utterance.onstart = session.utterance.onend = session.utterance.onerror = null;
    window.speechSynthesis?.cancel();
  }
  if (playback) { playback.onended = null; playback.stop(); playback.disconnect(); playback = null; }
}
function currentAudio(session) {
  return audioSession === session && session.epoch === generation && session.turn === turn && running && accepting;
}
function sessionForAudio(message) {
  if (!audioSession || audioSession.turn !== turn || audioSession.epoch !== generation) {
    audioSession = {epoch: generation, turn, context, queue: [], decoding: 0, ended: false,
      acknowledged: false, decode: Promise.resolve(), received: performance.now(), firstPlaybackMs: undefined, metrics: {}};
  }
  Object.assign(audioSession.metrics, message.metrics || {});
  return audioSession;
}
function completePlayback(session) {
  if (!currentAudio(session) || session.acknowledged) return;
  session.acknowledged = true;
  const acknowledge = () => {
    if (currentAudio(session)) command('played', {turn_id: session.turn});
  };
  // Keep capture closed while the last sound and room echo decay.
  if (voiceInterruption) acknowledge();
  else session.completionTimer = setTimeout(acknowledge, 350);
}
function playQueuedAudio(session) {
  if (!currentAudio(session) || playback || session.acknowledged) return;
  const buffer = session.queue.shift();
  if (!buffer) {
    if (session.ended && session.decoding === 0) {
      completePlayback(session);
    }
    return;
  }
  const source = session.context.createBufferSource();
  source.buffer = buffer;
  source.connect(session.context.destination);
  source.onended = () => {
    if (!currentAudio(session) || playback !== source) return;
    source.disconnect(); playback = null;
    playQueuedAudio(session);
  };
  playback = source;
  source.start();
  if (session.firstPlaybackMs === undefined) {
    session.firstPlaybackMs = performance.now() - session.received;
    showMetrics(session.metrics, session.firstPlaybackMs);
  }
}
function enqueueAudio(message) {
  const session = sessionForAudio(message);
  showState('Speaking');
  // A legacy whole-response WAV is also accepted during rolling upgrades.
  if (message.final === true || (message.final === undefined && message.chunk_index === undefined)) session.ended = true;
  session.decoding++;
  session.decode = session.decode.then(async () => {
    if (!currentAudio(session)) return;
    const bytes = Uint8Array.from(atob(message.wav), (char) => char.charCodeAt(0));
    const buffer = await session.context.decodeAudioData(bytes.buffer);
    if (!currentAudio(session)) return;
    await session.context.resume();
    if (!currentAudio(session)) return;
    session.queue.push(buffer);
  }).catch(() => {
    if (currentAudio(session)) error('音声を再生できませんでした。ブラウザの音声設定を確認してください。');
  }).finally(() => {
    session.decoding--;
    playQueuedAudio(session);
  });
  return session.decode;
}
function localJapaneseVoice() {
  return window.speechSynthesis?.getVoices().find((voice) => voice.localService === true && /^ja(?:[-_]|$)/i.test(voice.lang));
}
function playQueuedSpeech(session) {
  if (!currentAudio(session) || session.utterance || session.acknowledged) return;
  const chunk = session.speechQueue.shift();
  if (!chunk) {
    if (session.ended) completePlayback(session);
    return;
  }
  const voice = localJapaneseVoice();
  if (!voice || !window.SpeechSynthesisUtterance) {
    stop(); error('端末の日本語ローカル音声が見つかりません。Windowsの音声機能で日本語の音声合成を追加してください。'); return;
  }
  const utterance = new window.SpeechSynthesisUtterance(chunk.text);
  utterance.voice = voice; utterance.lang = voice.lang;
  utterance.rate = Math.max(.5, Math.min(2, chunk.rate || 1));
  session.utterance = utterance;
  utterance.onstart = () => {
    if (!currentAudio(session)) return;
    clearTimeout(session.speechTimer);
    session.firstPlaybackMs ??= performance.now() - session.received;
    showMetrics(session.metrics, session.firstPlaybackMs);
  };
  utterance.onend = () => {
    if (!currentAudio(session) || session.utterance !== utterance) return;
    clearTimeout(session.speechTimer);
    session.utterance = null;
    playQueuedSpeech(session);
  };
  utterance.onerror = (event) => {
    if (!currentAudio(session)) return;
    stop(); error(`読み上げに失敗しました（${event.error || '原因不明'}）。設定の「音声テスト」を押してください。`);
  };
  session.speechTimer = setTimeout(() => {
    if (!currentAudio(session) || session.utterance !== utterance) return;
    stop(); error('読み上げが開始されませんでした。設定の「音声テスト」で、サイトの音声許可と出力先を確認してください。');
  }, 8000);
  try {
    window.speechSynthesis.resume();
    window.speechSynthesis.speak(utterance);
  } catch (exc) { utterance.onerror({error: exc.message}); }
}
function enqueueSpeech(message) {
  const session = sessionForAudio(message);
  session.mode = 'speech'; session.speechQueue ??= [];
  session.speechQueue.push({text: message.text, rate: message.rate});
  if (message.final === true) session.ended = true;
  showState('Speaking'); playQueuedSpeech(session);
}
function testSpeech() {
  if (running) return;
  const synth = window.speechSynthesis, voice = localJapaneseVoice();
  if (!voice || !window.SpeechSynthesisUtterance) {
    error('日本語のローカル音声がブラウザに見つかりません。Windowsの日本語音声を追加し、ブラウザを再起動してください。'); return;
  }
  if (speechTest) {
    clearTimeout(speechTest.timer);
    speechTest.onstart = speechTest.onend = speechTest.onerror = null;
  }
  synth.cancel(); synth.resume();
  const utterance = new window.SpeechSynthesisUtterance('こんにちは。aivoの音声テストです。');
  speechTest = utterance; utterance.voice = voice; utterance.lang = voice.lang;
  const status = $('speech-test-status');
  status.textContent = `${voice.name}：再生を開始しています…`;
  const finish = (message) => {
    if (speechTest !== utterance) return;
    clearTimeout(utterance.timer); status.textContent = message;
  };
  utterance.onstart = () => finish('音声を再生中です。聞こえない場合は、サイトのミュート・Windowsの音量ミキサー・出力先を確認してください。');
  utterance.onend = () => { finish('音声テストが終了しました。'); speechTest = null; };
  utterance.onerror = (event) => { finish(`音声テストに失敗しました（${event.error || '原因不明'}）。サイトの音声許可を確認してください。`); speechTest = null; };
  utterance.timer = setTimeout(() => {
    if (speechTest !== utterance) return;
    finish('音声が開始されませんでした。サイトの音声許可を確認し、ChromeまたはEdgeを再起動してください。');
    utterance.onstart = utterance.onend = utterance.onerror = null;
    speechTest = null; synth.cancel();
  }, 8000);
  try { synth.speak(utterance); } catch (exc) { utterance.onerror({error: exc.message}); }
}
async function receive(message, fromSocket) {
  if (socket !== fromSocket) return;
  if (message.type === 'state' && message.state === 'Idle') {
    turn = Math.max(turn, message.turn_id);
    finishStop();
    return;
  }
  if (!running || !accepting || message.turn_id < turn) return;
  turn = message.turn_id;
  if (message.type === 'interrupted') {
    clearPlayback();
    showState('Listening');
    return;
  }
  if (message.type === 'state' && states[message.state]) showState(message.state);
  if (message.type === 'history_status') { $('settings-message').textContent = message.message; }
  if (message.type === 'lookup_status') {
    $('lookup-status').textContent = message.text;
    $('lookup-status').hidden = !message.text;
  }
  if (message.type === 'transcript') {
    if (message.role === 'user') $('lookup-status').hidden = true;
    addMessage(message.role, message.text);
  }
  if (message.type === 'sources' && Array.isArray(message.sources)) addSources(message.sources);
  if (message.type === 'error') {
    clearPlayback();
    error(message.message);
    if (state === 'Preparing') stop();
  }
  if (message.type === 'audio') return enqueueAudio(message);
  if (message.type === 'speech') return enqueueSpeech(message);
  if (message.type === 'audio_end') {
    const session = sessionForAudio(message);
    session.ended = true;
    showMetrics(session.metrics, session.firstPlaybackMs);
    if (session.mode === 'speech') playQueuedSpeech(session);
    else playQueuedAudio(session);
  }
}

function connect() {
  if (socket?.readyState === WebSocket.OPEN) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const candidate = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/ws`);
    socket = candidate; turn = 0;
    const timeout = setTimeout(() => { candidate.close(); reject(new Error('接続がタイムアウトしました。')); }, 5000);
    candidate.onopen = () => { clearTimeout(timeout); resolve(); };
    candidate.onerror = () => { clearTimeout(timeout); reject(new Error('アプリに接続できません。起動状態を確認してください。')); };
    candidate.onmessage = (event) => {
      try { void receive(JSON.parse(event.data), candidate); }
      catch { error('サーバーから無効な応答を受信しました。'); stop(); }
    };
    candidate.onclose = () => {
      clearTimeout(timeout);
      if (socket !== candidate) return;
      socket = null; finishStop();
      if (running) { stop(); error('接続が切れました。再度「会話を開始」を押してください。'); }
      reject(new Error('接続が閉じられました。'));
    };
  });
}
async function microphones() {
  if (!navigator.mediaDevices?.enumerateDevices) return;
  const selected = $('microphone').value;
  const devices = await navigator.mediaDevices.enumerateDevices();
  $('microphone').replaceChildren(new Option('システムの既定', ''));
  devices.filter((d) => d.kind === 'audioinput' && d.deviceId).forEach((d, index) => {
    $('microphone').add(new Option(d.label || `マイク ${index + 1}`, d.deviceId));
  });
  if ([...$('microphone').options].some((option) => option.value === selected)) $('microphone').value = selected;
}
async function start() {
  if (running) return;
  if (speechTest) {
    clearTimeout(speechTest.timer);
    speechTest.onstart = speechTest.onend = speechTest.onerror = null;
    speechTest = null; window.speechSynthesis.cancel();
  }
  window.speechSynthesis?.resume();
  running = true;
  accepting = false;
  const epoch = ++generation;
  error(''); showState('Preparing');
  try {
    if (!navigator.mediaDevices?.getUserMedia || !window.AudioWorkletNode) throw new Error('音声入力に対応したChromeまたはEdgeで、localhostから開いてください。');
    await stopAcknowledgement;
    if (epoch !== generation) return;
    await connect();
    if (epoch !== generation) return;
    const microphone = $('microphone').value;
    const acquired = await navigator.mediaDevices.getUserMedia({audio: {
      deviceId: microphone ? {exact: microphone} : undefined,
      channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true,
    }});
    if (epoch !== generation) { acquired.getTracks().forEach((track) => track.stop()); return; }
    stream = acquired;
    stream.getAudioTracks().forEach((track) => track.onended = () => {
      if (running) { stop(); error('マイクが切断されました。接続を確認して再開してください。'); }
    });
    context = new AudioContext();
    const audioContext = context;
    await audioContext.resume();
    await audioContext.audioWorklet.addModule('/static/microphone.js');
    if (epoch !== generation) return;
    input = audioContext.createMediaStreamSource(stream);
    capture = new AudioWorkletNode(audioContext, 'aivo-microphone');
    capture.port.onmessage = (event) => {
      if (epoch !== generation) return;
      handleCapture(event.data);
    };
    input.connect(capture); capture.connect(audioContext.destination);
    void microphones().catch(() => {});
    accepting = true;
    command('start');
  } catch (exc) {
    if (epoch !== generation) return;
    stop();
    const messages = {NotAllowedError: 'マイクの使用が許可されていません。アドレスバーのサイト設定で許可して、再開してください。', NotFoundError: 'マイクが見つかりません。マイクを接続してください。', NotReadableError: 'マイクを使用できません。他のアプリの使用状況を確認してください。'};
    error(messages[exc.name] || exc.message || 'マイクの準備に失敗しました。');
  }
}
async function refresh() {
  $('refresh').disabled = true;
  try {
    const response = await fetch('/api/health', {signal: AbortSignal.timeout(8000), cache: 'no-store'});
    if (!response.ok) throw new Error();
    const health = await response.json();
    interruptionSupported = health.voice_interruption === true;
    updateVoiceInterruption();
    const protocolCurrent = health.response_protocol === 'final-json-v1';
    ttsBackend = health.tts_backend || "voicevox";
    ready = health.ready && protocolCurrent && (ttsBackend !== "browser" || !!localJapaneseVoice());
    if (!protocolCurrent && running) stop();
    $('services').replaceChildren();
    for (const [key, name] of [['stt', '音声認識'], ['ollama', 'Ollama'], ['voicevox', 'VOICEVOX']]) {
      const tag = document.createElement('span'); tag.className = `service ${(health[key] || (key === 'voicevox' && ttsBackend === 'browser' && localJapaneseVoice())) ? 'ready' : ''}`;
      tag.textContent = key === 'voicevox' && ttsBackend === 'browser' ? '読み上げ：端末標準（VOICEVOX不要）' : `${name} ${health[key] ? '準備完了' : '未準備'}`; $('services').append(tag);
    }
    const protocolTag = document.createElement('span');
    protocolTag.className = `service ${protocolCurrent ? 'ready' : ''}`;
    protocolTag.textContent = protocolCurrent
      ? `応答方式: ${health.response_protocol} · 実行ID: ${health.runtime_id || '不明'}`
      : '旧バージョン：サーバーの再起動が必要です';
    $('services').append(protocolTag);
    if (!protocolCurrent) {
      error('旧バージョン：サーバーの再起動が必要です。起動中のaivoを停止して起動し直し、この画面を再読み込みしてください。');
      protocolWarning = true;
    } else if (protocolWarning) {
      error('');
      $('hint').textContent = states[state][1];
      protocolWarning = false;
    }
    const sttRuntime = health.actual_stt_device
      ? `${health.actual_stt_device} / ${health.actual_stt_compute || '不明'}（実行）`
      : `未ロード（設定: ${health.stt_device} / ${health.stt_compute || 'auto'}）`;
    const gpuRuntime = health.gpu_loaded
      ? `GPU常駐 ${Math.round((health.size_vram || 0) / 1048576)} MiB` : 'GPU常駐なし';
    $('configuration').textContent = `STT: ${health.stt_model} [${sttRuntime}] / LLM: ${health.llm_model} [${health.quantization || '量子化不明'}, ${gpuRuntime}] / 読み上げ: ${ttsBackend === "browser" ? "端末の日本語ローカル音声" : `VOICEVOX speaker ${health.speaker}`} / 応答方式: ${health.response_protocol || '旧バージョン'} / 実行ID: ${health.runtime_id || '不明'} / 検索処理: ${health.lookup_revision || "旧版"} / 外部検索: ${health.web_search ? "有効" : "無効"} / 音声割り込み: ${voiceInterruption ? '有効（自動）' : '無効（サーバー未対応）'}`;
    if (!ready) $('hint').textContent = protocolCurrent
      ? 'READMEの初期設定を完了し、サービスを起動して「再確認」を押してください。'
      : 'サーバーを再起動してから、この画面を再読み込みしてください。';
    if (protocolCurrent && health.ready && ttsBackend === 'browser' && !localJapaneseVoice()) $('hint').textContent = '端末の日本語ローカル音声が必要です。Windowsの言語と音声の設定から日本語の音声合成を追加してください。';
  } catch {
    ready = false; $('services').textContent = 'アプリの接続を確認できません。';
  } finally {
    $('refresh').disabled = false; $('start').disabled = running || !ready;
    $('test-speech').disabled = running || ttsBackend !== 'browser';
  }
}
async function localRequest(path, options = {}) {
  const response = await fetch(path, {cache: 'no-store', ...options});
  const body = await response.json();
  if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : '操作に失敗しました。');
  return body;
}
async function loadPreferences() {
  try {
    const data = await localRequest('/api/settings');
    const value = data.current;
    $('pref-model').value = value.llm;
    $('available-models').replaceChildren(...data.models.map((name) => new Option(name, name)));
    const voices = data.voices.filter((voice) => voice.id !== value.speaker);
    $('pref-speaker').replaceChildren(new Option(data.voices.find((voice) => voice.id === value.speaker)?.name || `speaker ${value.speaker}`, value.speaker),
      ...voices.map((voice) => new Option(voice.name, voice.id)));
    $('pref-speaker').value = String(value.speaker);
    $('pref-speed').value = value.speed;
    $('pref-device').value = value.stt_device;
    $('pref-search').checked = value.web_search;
    $('pref-history').checked = value.save_history;
  } catch (exc) { $('settings-message').textContent = exc.message; }
}
async function loadHistory() {
  try {
    const data = await localRequest('/api/history');
    $('saved-history').replaceChildren();
    if (!data.conversations.length) $('saved-history').textContent = '保存した会話はありません。';
    for (const conversation of data.conversations) {
      const row = document.createElement('div'); row.className = 'saved-conversation';
      const open = document.createElement('button'); open.type = 'button'; open.className = 'text-button';
      open.textContent = `${new Date(conversation.updated_at).toLocaleString("ja-JP", {timeZone: "Asia/Tokyo"})} / ${conversation.turns}往復 / ${conversation.preview}`;
      open.addEventListener('click', async () => {
        try {
          const result = await localRequest(`/api/history/${encodeURIComponent(conversation.id)}`);
          $('saved-messages').replaceChildren();
          for (const message of result.messages) {
            const text = document.createElement('p'); text.className = 'message-text';
            text.textContent = `${message.role === 'user' ? 'あなた' : 'aivo'}: ${message.content}`;
            $('saved-messages').append(text);
          }
        } catch (exc) { $('settings-message').textContent = exc.message; }
      });
      const remove = document.createElement('button'); remove.type = 'button'; remove.className = 'text-button'; remove.textContent = '削除';
      remove.addEventListener('click', async () => {
        try {
          await localRequest(`/api/history/${encodeURIComponent(conversation.id)}`, {method: 'DELETE'});
          $('saved-messages').replaceChildren(); await loadHistory();
        } catch (exc) { $('settings-message').textContent = exc.message; }
      });
      row.append(open, remove); $('saved-history').append(row);
    }
  } catch (exc) { $('settings-message').textContent = exc.message; }
}
$('settings-panel').addEventListener('toggle', () => { if ($('settings-panel').open) void loadPreferences(); });
$('history-panel').addEventListener('toggle', () => { if ($('history-panel').open) void loadHistory(); });
$('refresh-history').addEventListener('click', loadHistory);
$('clear-history').addEventListener('click', async () => {
  if (!window.confirm('保存した会話をすべて削除しますか？')) return;
  try {
    await localRequest('/api/history', {method: 'DELETE'});
    $('saved-messages').replaceChildren(); await loadHistory();
  } catch (exc) { $('settings-message').textContent = exc.message; }
});
$('preferences').addEventListener('submit', async (event) => {
  event.preventDefault(); $('save-preferences').disabled = true;
  try {
    const result = await localRequest('/api/settings', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({
      llm: $('pref-model').value.trim(), speaker: Number($('pref-speaker').value), speed: Number($('pref-speed').value),
      stt_device: $('pref-device').value, web_search: $('pref-search').checked, save_history: $('pref-history').checked,
    })});
    $('settings-message').textContent = result.message;
  } catch (exc) { $('settings-message').textContent = exc.message; }
  finally { $('save-preferences').disabled = false; }
});
$('start').addEventListener('click', start);
$('stop').addEventListener('click', stop);
$('reset').addEventListener('click', async () => {
  $('reset').disabled = true;
  stop();
  await stopAcknowledgement;
  command('reset');
  $('lookup-status').hidden = true;
  $('transcript').replaceChildren();
  const empty = document.createElement('p'); empty.id = 'empty'; empty.className = 'empty';
  empty.textContent = '新しい会話をはじめましょう。'; $('transcript').append(empty);
  error(''); $('reset').disabled = false;
});
$('refresh').addEventListener('click', refresh);
navigator.mediaDevices?.addEventListener('devicechange', () => { void microphones().catch(() => {}); });
window.addEventListener('pagehide', () => { running = false; generation++; releaseAudio(); socket?.close(); });
window.speechSynthesis?.addEventListener('voiceschanged', () => { if (!running) void refresh(); });
showState('Idle'); void refresh(); void microphones().catch(() => {});




$('test-speech').addEventListener('click', testSpeech);

