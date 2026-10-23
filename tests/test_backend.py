import asyncio
import json
import sys
import threading
from types import SimpleNamespace

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from aivo.core import FRAME, Settings
from aivo.backends import Backends


class FakeVAD:
    def __call__(self, audio, sample_rate):
        assert sample_rate == 16000
        return SimpleNamespace(item=lambda: float(np.max(np.abs(audio)) > .1))

    def reset_states(self):
        pass


class FakeBackends:
    def __init__(self):
        self.actual_stt_device = self.actual_stt_compute = None
        self.messages = []
        self.fail = False
        self.block = False
        self.stt_calls = 0
        self.gate = None
        self.generated = False
        self.cancelled_generations = []
        self.tts_gate = None
        self.tts_started = threading.Event()
        self.tts_calls = 0
        self.tts_cancelled = False

    def vad(self):
        return FakeVAD()

    def transcribe(self, audio):
        assert audio.dtype == np.float32
        self.stt_calls += 1
        return "test utterance"

    async def prepare(self, client):
        pass

    async def chat_stream(self, client, messages, dialogue=None):
        self.messages.append([dict(m) for m in messages])
        generation = len(self.messages)
        gate = self.gate
        try:
            if self.block:
                await asyncio.Event().wait()
            if self.fail:
                self.fail = False
                raise RuntimeError("test service failure")
            yield "test ", 1.0
            if gate is not None:
                while not gate.is_set():
                    await asyncio.sleep(.01)
            yield "answer", 1.0
            self.generated = True
        except asyncio.CancelledError:
            self.cancelled_generations.append(generation)
            raise

    async def synthesize(self, client, text):
        self.tts_calls += 1
        if self.tts_gate is not None and self.tts_calls >= 2:
            self.tts_started.set()
            try:
                gate = self.tts_gate
                while not gate.is_set():
                    await asyncio.sleep(.01)
            except asyncio.CancelledError:
                self.tts_cancelled = True
                raise
        return b"RIFFtest"


@pytest.fixture
def session(monkeypatch):
    from aivo import server
    fake = FakeBackends()
    monkeypatch.setattr(server, "backends", fake)
    monkeypatch.setattr(server, "settings", Settings(silence_ms=800))
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(from_numpy=lambda value: value))

    async def ready(settings):
        return {"ready": True}

    monkeypatch.setattr(server, "readiness", ready)
    with TestClient(server.app, base_url="http://localhost") as client:
        yield client, fake


def connect(client):
    return client.websocket_connect("ws://localhost/ws", headers={"origin": "http://localhost"})


def send_turn(ws):
    for _ in range(8):
        ws.send_bytes(np.full(FRAME, .5, dtype="<f4").tobytes())
    for _ in range(25):
        ws.send_bytes(np.zeros(FRAME, dtype="<f4").tobytes())


def start(ws):
    ws.send_json({"type": "start"})
    assert ws.receive_json()["state"] == "Preparing"
    assert ws.receive_json()["state"] == "Listening"


def receive_audio(ws):
    assert ws.receive_json()["state"] == "Processing"
    assert ws.receive_json()["role"] == "user"
    chunks = []
    assistant = None
    while True:
        event = ws.receive_json()
        if event["type"] == "audio_end":
            assert {"vad_ms", "stt_ms", "llm_ms", "ttft_ms", "tts_ms", "server_ms", "first_audio_ready_ms"} <= event["metrics"].keys()
            assert assistant == "test answer"
            assert len(chunks) == 2
            return event
        if event["type"] == "transcript":
            assert event["role"] == "assistant"
            assistant = event["text"]
        else:
            assert event["type"] == "audio"
            assert event["chunk_index"] == len(chunks)
            assert event["final"] is False
            assert event["wav"] == "UklGRnRlc3Q="
            chunks.append(event)


def test_ten_mocked_voice_turns_history_and_reset(session):
    client, fake = session
    with connect(client) as ws:
        assert ws.receive_json()["state"] == "Idle"
        start(ws)
        for index in range(10):
            send_turn(ws)
            audio = receive_audio(ws)
            assert len(fake.messages[-1]) == 2 + index * 2
            ws.send_json({"type": "played", "turn_id": audio["turn_id"]})
            assert ws.receive_json()["state"] == "Listening"
        assert fake.stt_calls == 10
        ws.send_json({"type": "reset"})
        assert ws.receive_json()["state"] == "Idle"
        start(ws)
        send_turn(ws)
        receive_audio(ws)
        assert len(fake.messages[-1]) == 2


def test_error_recovery_and_stop_discards_late_pcm(session):
    client, fake = session
    fake.fail = True
    with connect(client) as ws:
        ws.receive_json()
        start(ws)
        send_turn(ws)
        assert ws.receive_json()["state"] == "Processing"
        assert ws.receive_json()["role"] == "user"
        assert ws.receive_json()["type"] == "error"
        assert ws.receive_json()["state"] == "Listening"
        send_turn(ws)
        receive_audio(ws)
        assert len(fake.messages[-1]) == 2  # Failed turns are not conversation history.
        ws.send_json({"type": "stop"})
        assert ws.receive_json()["state"] == "Idle"
        send_turn(ws)
        ws.send_json({"type": "reset"})
        assert ws.receive_json()["state"] == "Idle"
        assert fake.stt_calls == 2


def test_stop_cancels_pending_generation(session):
    client, fake = session
    fake.block = True
    with connect(client) as ws:
        ws.receive_json()
        start(ws)
        send_turn(ws)
        assert ws.receive_json()["state"] == "Processing"
        assert ws.receive_json()["role"] == "user"
        ws.send_json({"type": "stop"})
        assert ws.receive_json()["state"] == "Idle"
        fake.block = False
        start(ws)
        send_turn(ws)
        receive_audio(ws)
        assert len(fake.messages[-1]) == 2


def test_invalid_frames_and_cross_origin_rejected(session):
    client, _ = session
    with pytest.raises(WebSocketDisconnect) as rejected:
        with client.websocket_connect("ws://localhost/ws", headers={"origin": "https://evil.example"}):
            pass
    assert rejected.value.code == 1008
    with connect(client) as ws:
        ws.receive_json()
        ws.send_bytes(b"invalid")
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
        assert closed.value.code == 1009
    assert client.get("/", headers={"host": "evil.example"}).status_code == 400


def test_nonfinite_audio_rejected(session):
    client, _ = session
    with connect(client) as ws:
        ws.receive_json()
        start(ws)
        ws.send_bytes(np.full(FRAME, np.nan, dtype="<f4").tobytes())
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
        assert closed.value.code == 1008


def test_ollama_stream_and_voicevox_contract():
    backend = Backends(Settings())
    seen = []

    def handler(request):
        seen.append(request.url.path)
        if request.url.path == "/api/generate":
            payload = json.loads(request.content)
            assert payload["stream"] is True and payload["raw"] is True and "hello" in payload["prompt"]
            return httpx.Response(200, text=json.dumps({"response": json.dumps({"reply": "hello world"}), "done": True}) + "\n")
        if request.url.path == "/audio_query":
            assert request.url.params["speaker"] == "3"
            return httpx.Response(200, json={"speedScale": 1})
        assert request.url.path == "/synthesis"
        assert json.loads(request.content) == {"speedScale": 1}
        return httpx.Response(200, content=b"RIFFtest")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            chunks = [chunk async for chunk in backend.chat_stream(client, [{"role": "user", "content": "hello"}])]
            answer = "".join(chunk[0] for chunk in chunks)
            assert answer == "hello world" and chunks[0][1] >= 0
            assert await backend.synthesize(client, answer) == b"RIFFtest"
    asyncio.run(run())
    assert seen == ["/api/generate", "/audio_query", "/synthesis"]

def test_early_playback_ack_cannot_unlock_processing(session):
    client, fake = session
    fake.block = True
    with connect(client) as ws:
        ws.receive_json()
        start(ws)
        send_turn(ws)
        processing = ws.receive_json()
        assert processing["state"] == "Processing"
        assert ws.receive_json()["role"] == "user"
        ws.send_json({"type": "played", "turn_id": processing["turn_id"]})
        assert ws.receive_json()["type"] == "error"
        send_turn(ws)
        ws.send_json({"type": "stop"})
        assert ws.receive_json()["state"] == "Idle"
        assert fake.stt_calls == 1

def test_frontend_and_health_are_served(session):
    client, _ = session
    page = client.get("/")
    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    assert "aivo" in page.text
    for asset in ("app.js", "microphone.js", "style.css"):
        assert client.get("/static/" + asset).status_code == 200
    assert client.get("/api/health").json()["ready"] is True


def test_first_audio_precedes_remaining_llm_generation(session):
    client, fake = session
    fake.gate = threading.Event()
    with connect(client) as ws:
        ws.receive_json()
        start(ws)
        send_turn(ws)
        assert ws.receive_json()["state"] == "Processing"
        assert ws.receive_json()["role"] == "user"
        first = ws.receive_json()
        assert first["type"] == "audio" and first["chunk_index"] == 0
        assert not fake.generated
        fake.gate.set()
        assert ws.receive_json()["type"] == "audio"
        assert ws.receive_json()["role"] == "assistant"
        end = ws.receive_json()
        assert end["type"] == "audio_end"
        ws.send_json({"type": "played", "turn_id": end["turn_id"]})
        assert ws.receive_json()["state"] == "Listening"


def test_stop_during_stream_discards_remaining_audio(session):
    client, fake = session
    fake.gate = threading.Event()
    with connect(client) as ws:
        ws.receive_json()
        start(ws)
        send_turn(ws)
        ws.receive_json()
        ws.receive_json()
        assert ws.receive_json()["type"] == "audio"
        ws.send_json({"type": "stop"})
        assert ws.receive_json()["state"] == "Idle"
        fake.gate.set()
        ws.send_json({"type": "reset"})
        assert ws.receive_json()["state"] == "Idle"


def send_voice_frames(ws, count):
    frame = np.full(FRAME, .5, dtype="<f4").tobytes()
    for _ in range(count):
        ws.send_bytes(frame)


def send_silent_frames(ws, count=25):
    frame = np.zeros(FRAME, dtype="<f4").tobytes()
    for _ in range(count):
        ws.send_bytes(frame)


def receive_interruption(ws, old_turn):
    event = ws.receive_json()
    assert event["type"] == "interrupted"
    assert event["interrupted_turn_id"] == old_turn
    assert event["turn_id"] > old_turn
    assert ws.receive_json()["state"] == "Listening"
    return event


def test_barge_in_after_audio_end_preserves_new_word_and_ignores_old_played(session):
    client, fake = session
    with connect(client) as ws:
        ws.receive_json()
        start(ws)
        send_turn(ws)
        old = receive_audio(ws)
        send_voice_frames(ws, 5)
        receive_interruption(ws, old["turn_id"])
        ws.send_json({"type": "played", "turn_id": old["turn_id"]})
        # The detector's five confirmed frames must seed the new utterance.
        # Three more frames produce the regular segmenter's 250 ms minimum.
        send_voice_frames(ws, 3)
        send_silent_frames(ws)
        new = receive_audio(ws)
        assert new["turn_id"] > old["turn_id"]
        assert fake.stt_calls == 2
        ws.send_json({"type": "played", "turn_id": new["turn_id"]})
        assert ws.receive_json()["state"] == "Listening"


def test_barge_in_cancels_pending_generation_and_stop_clears_new_utterance(session):
    client, fake = session
    fake.gate = threading.Event()
    with connect(client) as ws:
        ws.receive_json()
        start(ws)
        send_turn(ws)
        assert ws.receive_json()["state"] == "Processing"
        assert ws.receive_json()["role"] == "user"
        first = ws.receive_json()
        assert first["type"] == "audio"
        send_voice_frames(ws, 5)
        receive_interruption(ws, first["turn_id"])
        assert fake.cancelled_generations == [1]
        ws.send_json({"type": "played", "turn_id": first["turn_id"]})
        ws.send_json({"type": "stop"})
        assert ws.receive_json()["state"] == "Idle"
        send_voice_frames(ws, 8)
        send_silent_frames(ws)
        ws.send_json({"type": "reset"})
        assert ws.receive_json()["state"] == "Idle"
        assert fake.stt_calls == 1


def test_barge_in_cancels_pending_tts_before_new_turn_can_speak(session):
    client, fake = session
    fake.tts_gate = threading.Event()
    with connect(client) as ws:
        ws.receive_json()
        start(ws)
        send_turn(ws)
        assert ws.receive_json()["state"] == "Processing"
        assert ws.receive_json()["role"] == "user"
        first = ws.receive_json()
        assert first["type"] == "audio"
        assert fake.tts_started.wait(timeout=2), "Second TTS call must be in flight"
        send_voice_frames(ws, 5)
        receive_interruption(ws, first["turn_id"])
        assert fake.tts_cancelled
        fake.tts_gate = None
        send_voice_frames(ws, 3)
        send_silent_frames(ws)
        new = receive_audio(ws)
        assert new["turn_id"] > first["turn_id"]
        assert fake.stt_calls == 2


def test_playback_end_preserves_speech_shorter_than_barge_confirmation(session):
    client, fake = session
    with connect(client) as ws:
        ws.receive_json()
        start(ws)
        send_turn(ws)
        old = receive_audio(ws)
        send_voice_frames(ws, 2)
        ws.send_json({"type": "played", "turn_id": old["turn_id"]})
        assert ws.receive_json()["state"] == "Listening"
        send_voice_frames(ws, 6)
        send_silent_frames(ws)
        new = receive_audio(ws)
        assert new["turn_id"] > old["turn_id"]
        assert fake.stt_calls == 2
