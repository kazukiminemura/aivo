import asyncio
import base64
import contextlib
import json
import logging
import uuid
from pathlib import Path
from time import perf_counter
from urllib.parse import urlsplit

import httpx
import numpy as np
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from aivo.backends import Backends, readiness
from aivo.core import FRAME, SAMPLE_RATE, Segmenter, Settings, build_messages

load_dotenv(encoding="utf-8-sig")
settings = Settings()
backends = Backends(settings)
RUNTIME_ID = uuid.uuid4().hex[:8]
RESPONSE_PROTOCOL = "final-json-v1"
app = FastAPI(docs_url=None, redoc_url=None)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]"])
WEB = Path(__file__).parent / "web"
app.mount("/static", StaticFiles(directory=WEB), name="static")
logger = logging.getLogger("aivo")
logger.setLevel(logging.INFO)
if not logger.handlers:
    logger.addHandler(logging.StreamHandler())
logger.propagate = False


@app.get("/")
async def index():
    return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/api/health")
async def health():
    status = await readiness(settings)
    status["response_protocol"] = RESPONSE_PROTOCOL
    status["runtime_id"] = RUNTIME_ID
    status["actual_stt_device"] = backends.actual_stt_device
    status["actual_stt_compute"] = backends.actual_stt_compute
    return status


@app.websocket("/ws")
async def conversation(ws: WebSocket):
    origin = urlsplit(ws.headers.get("origin", ""))
    if origin.scheme not in {"http", "https"} or origin.netloc != ws.headers.get("host"):
        await ws.close(code=1008)
        return
    await ws.accept()
    session_id = uuid.uuid4().hex
    turn_id = 0
    task = None
    active = listening = awaiting_playback = False
    history = []
    segmenter = Segmenter(settings.threshold, settings.silence_ms)
    vad = None
    client = httpx.AsyncClient(timeout=httpx.Timeout(120, connect=5), trust_env=False)

    async def emit(kind, **data):
        await ws.send_json({"type": kind, "session_id": session_id, "turn_id": turn_id,
                            "timestamp": perf_counter(), **data})

    async def cancel():
        nonlocal task, active, listening, awaiting_playback, turn_id
        turn_id += 1
        active = listening = awaiting_playback = False
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            task = None
        segmenter.reset()
        if vad is not None:
            vad.reset_states()

    async def start_session():
        nonlocal vad, listening, active
        active = True
        try:
            await emit("state", state="Preparing")
            async with asyncio.timeout(180):
                status = await readiness(settings)
                if not status["ready"]:
                    raise RuntimeError("準備が必要です。接続状態とREADMEの初期設定を確認してください。")
                await backends.prepare(client)
                if vad is None:
                    vad = await asyncio.to_thread(backends.vad)
                listening = True
                active = False
                await emit("state", state="Listening")
        except Exception as exc:
            active = listening = False
            logger.warning("session=%s warmup_error=%s", session_id, type(exc).__name__)
            await emit("error", message=str(exc) if isinstance(exc, RuntimeError) else "モデルの準備に失敗しました。実行設定を確認してください。")
            await emit("state", state="Idle")

    async def process(raw, vad_wait):
        nonlocal active, awaiting_playback
        started = perf_counter()
        metrics = {"vad_ms": round(vad_wait, 1), "tts_ms": 0.0}
        producer = None
        try:
            async with asyncio.timeout(180):
                await emit("state", state="Processing")
                audio = np.frombuffer(raw, dtype="<f4").copy()
                text = await asyncio.to_thread(backends.transcribe, audio)
                metrics["stt_ms"] = round((perf_counter() - started) * 1000, 1)
                if not text:
                    active = False
                    await emit("state", state="Listening")
                    return
                await emit("transcript", role="user", text=text)
                messages = build_messages(history, text, settings.context)
                queue = asyncio.Queue(maxsize=2)
                parts = []
                llm_start = perf_counter()

                async def generate():
                    try:
                        async for chunk, ttft in backends.chat_stream(client, messages):
                            if chunk:
                                parts.append(chunk)
                                metrics["ttft_ms"] = ttft
                                await queue.put(chunk)
                        metrics["llm_ms"] = round((perf_counter() - llm_start) * 1000, 1)
                    except Exception as exc:
                        await queue.put(exc)
                    await queue.put(None)

                producer = asyncio.create_task(generate())
                chunk_index = 0
                while True:
                    chunk = await queue.get()
                    if chunk is None:
                        break
                    if isinstance(chunk, Exception):
                        raise chunk
                    tts_start = perf_counter()
                    wav = await backends.synthesize(client, chunk)
                    metrics["tts_ms"] = round(metrics["tts_ms"] + (perf_counter() - tts_start) * 1000, 1)
                    if chunk_index == 0:
                        metrics["first_audio_ready_ms"] = round((perf_counter() - started) * 1000 + vad_wait, 1)
                    await emit("audio", chunk_index=chunk_index, final=False,
                               wav=base64.b64encode(wav).decode(), metrics=dict(metrics))
                    chunk_index += 1
                await producer
                if not parts:
                    raise RuntimeError("LLM returned an empty answer")
                answer = "".join(parts)
                await emit("transcript", role="assistant", text=answer)
                metrics["server_ms"] = round((perf_counter() - started) * 1000 + vad_wait, 1)
                history.extend([{"role": "user", "content": text}, {"role": "assistant", "content": answer}])
                del history[:-40]
                awaiting_playback = True
                await emit("audio_end", metrics=metrics)
                logger.info("session=%s turn=%s device=%s compute=%s llm=%s metrics=%s", session_id, turn_id,
                            getattr(backends, "actual_stt_device", settings.stt_device),
                            getattr(backends, "actual_stt_compute", settings.stt_compute), settings.llm, metrics)
        except Exception as exc:
            active = awaiting_playback = False
            if isinstance(exc, httpx.HTTPStatusError):
                message = f"Local service HTTP {exc.response.status_code}. Check Ollama / VOICEVOX."
            elif isinstance(exc, (httpx.RequestError, TimeoutError)):
                message = "Local service unavailable or timed out. Check Ollama / VOICEVOX."
            else:
                message = str(exc) if isinstance(exc, RuntimeError) else "音声処理に失敗しました。モデルと実行デバイスを確認してください。"
            logger.warning("session=%s turn=%s error=%s", session_id, turn_id, type(exc).__name__)
            await emit("error", message=message)
            await emit("state", state="Listening")
        finally:
            if producer is not None:
                producer.cancel()
                await asyncio.gather(producer, return_exceptions=True)

    try:
        await emit("state", state="Idle")
        while True:
            packet = await ws.receive()
            if packet["type"] == "websocket.disconnect":
                break
            if packet.get("bytes") is not None:
                raw = packet["bytes"]
                if len(raw) != FRAME * 4:
                    await ws.close(code=1009, reason="Expected 512 float32 samples")
                    break
                if not listening or active or vad is None:
                    continue
                samples = np.frombuffer(raw, dtype="<f4")
                if not np.isfinite(samples).all() or np.max(np.abs(samples)) > 1.01:
                    await ws.close(code=1008, reason="Invalid PCM")
                    break
                import torch
                probability = float(vad(torch.from_numpy(samples.copy()), SAMPLE_RATE).item())
                result = segmenter.feed(raw, probability)
                if result is not None:
                    turn_id += 1
                    active = True
                    task = asyncio.create_task(process(result, settings.silence_ms))
            elif packet.get("text") is not None:
                if len(packet["text"]) > 2048:
                    await ws.close(code=1009)
                    break
                command = json.loads(packet["text"])
                kind = command.get("type")
                if kind == "start":
                    await cancel()
                    task = asyncio.create_task(start_session())
                elif kind in {"stop", "reset"}:
                    await cancel()
                    if kind == "reset":
                        history.clear()
                    await emit("state", state="Idle")
                elif kind == "played" and command.get("turn_id") == turn_id and active and awaiting_playback:
                    active = awaiting_playback = False
                    segmenter.reset()
                    vad.reset_states()
                    await emit("state", state="Listening")
                else:
                    await emit("error", message="Unknown command")
    except (WebSocketDisconnect, json.JSONDecodeError, AttributeError):
        pass
    finally:
        await cancel()
        await client.aclose()
