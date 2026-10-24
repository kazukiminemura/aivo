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
from aivo.answer import parse_final_answer
from aivo.barge_in import BargeIn
from aivo.dialogue import DialogueState
from aivo.web_search import lookup, evidence_message, lookup_reply
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
    status["web_search"] = settings.web_search
    status["lookup_revision"] = 2
    status["voice_interruption"] = True
    status["barge_threshold"] = settings.barge_threshold
    status["barge_min_ms"] = settings.barge_min_ms
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
    active = listening = awaiting_playback = playback_pending = False
    history = []
    dialogue = DialogueState()
    pending_user = None
    pending_answer = []
    committed_turn = None
    segmenter = Segmenter(settings.threshold, settings.silence_ms)
    vad = None
    barge = BargeIn(settings.barge_threshold, settings.barge_min_ms)
    client = httpx.AsyncClient(timeout=httpx.Timeout(120, connect=5), trust_env=False)

    async def emit(kind, **data):
        await ws.send_json({"type": kind, "session_id": session_id, "turn_id": turn_id,
                            "timestamp": perf_counter(), **data})

    async def cancel(interrupted=False):
        nonlocal task, active, listening, awaiting_playback, playback_pending, turn_id
        nonlocal pending_user, pending_answer, committed_turn
        old_turn = turn_id
        turn_id += 1
        active = listening = awaiting_playback = playback_pending = False
        if task:
            task.cancel()
        if interrupted:
            # Stop sound before awaiting cancellation of the generation pipeline.
            await emit("interrupted", interrupted_turn_id=old_turn)
        if task:
            with contextlib.suppress(asyncio.CancelledError):
                await task
            task = None
        if interrupted:
            marker = "\n（この応答の読み上げ中にユーザーが割り込みました。最後まで伝わっていません。）"
            if pending_user is not None:
                history.extend([{"role": "user", "content": pending_user},
                                {"role": "assistant", "content": "".join(pending_answer) + marker}])
                del history[:-40]
            elif committed_turn == old_turn and history:
                history[-1]["content"] += marker
        pending_user, pending_answer, committed_turn = None, [], None
        segmenter.reset()
        barge.reset()
        if vad is not None:
            vad.reset_states()

    def seed_capture(raw):
        # Replay detector pre-roll into normal VAD so the first syllables survive.
        import torch
        for offset in range(0, len(raw), FRAME * 4):
            frame = raw[offset:offset + FRAME * 4]
            samples = np.frombuffer(frame, dtype="<f4").copy()
            probability = float(vad(torch.from_numpy(samples), SAMPLE_RATE).item())
            segmenter.feed(frame, probability)

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
        nonlocal active, awaiting_playback, playback_pending
        nonlocal pending_user, pending_answer, committed_turn
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
                pending_user, pending_answer, committed_turn = text, [], None
                await emit("transcript", role="user", text=text)
                evidence, sources = None, []
                if settings.web_search:
                    await emit("lookup_status", text="外部情報の必要性を確認しています。")
                    evidence, sources = await lookup(client, backends, history, text, dialogue)
                    if evidence is not None:
                        count = len(evidence.get("network_requests", []))
                        labels = {"ok": "取得完了", "needs_location": "地域の指定待ち", "needs_date": "日付の指定待ち",
                                  "location_not_found": "地名検索で該当なし", "ambiguous_location": "地域の確認待ち",
                                  "unsupported_date": "予報の対象期間外", "failed": "取得失敗"}
                        await emit("lookup_status", text=f"外部リクエスト {count}件：{labels.get(evidence['status'], '確認完了')}")
                    else:
                        await emit("lookup_status", text="今回の返答では外部検索を利用していません。")
                else:
                    evidence = {"status": "disabled", "message": "外部検索は設定で無効。最新情報は確認できない。"}
                direct_reply = lookup_reply(evidence, dialogue)
                if direct_reply is not None:
                    direct_reply = parse_final_answer(json.dumps({"reply": direct_reply}))
                extra = evidence_message(evidence)["content"] if evidence is not None else ""
                messages = build_messages(history, text,
                    settings.context - 2 * (len(dialogue.instruction()) + len(extra)) - 384)
                if extra:
                    messages[0]["content"] += "\n" + extra
                queue = asyncio.Queue(maxsize=2)
                parts = []
                llm_start = perf_counter()

                async def generate():
                    try:
                        if direct_reply is not None:
                            parts.append(direct_reply)
                            await queue.put(direct_reply)
                            metrics["llm_ms"] = 0
                            await queue.put(None)
                            return
                        async for chunk, ttft in backends.chat_stream(client, messages, dialogue=dialogue):
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
                    if not playback_pending:
                        barge.reset()
                        vad.reset_states()
                        playback_pending = True
                    pending_answer.append(chunk)
                    await emit("audio", chunk_index=chunk_index, final=False,
                               wav=base64.b64encode(wav).decode(), metrics=dict(metrics))
                    chunk_index += 1
                await producer
                if not parts:
                    raise RuntimeError("LLM returned an empty answer")
                answer = "".join(parts)
                await emit("transcript", role="assistant", text=answer)
                if sources:
                    await emit("sources", sources=sources)
                metrics["server_ms"] = round((perf_counter() - started) * 1000 + vad_wait, 1)
                history.extend([{"role": "user", "content": text}, {"role": "assistant", "content": answer}])
                del history[:-40]
                committed_turn = turn_id
                pending_user, pending_answer = None, []
                awaiting_playback = True
                await emit("audio_end", metrics=metrics)
                logger.info("session=%s turn=%s device=%s compute=%s llm=%s metrics=%s", session_id, turn_id,
                            getattr(backends, "actual_stt_device", settings.stt_device),
                            getattr(backends, "actual_stt_compute", settings.stt_compute), settings.llm, metrics)
        except Exception as exc:
            active = awaiting_playback = playback_pending = False
            pending_user, pending_answer = None, []
            barge.reset()
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
                if not listening or vad is None:
                    continue
                if active and not playback_pending:
                    continue
                samples = np.frombuffer(raw, dtype="<f4")
                if not np.isfinite(samples).all() or np.max(np.abs(samples)) > 1.01:
                    await ws.close(code=1008, reason="Invalid PCM")
                    break
                import torch
                probability = float(vad(torch.from_numpy(samples.copy()), SAMPLE_RATE).item())
                if active:
                    interrupted_audio = barge.feed(raw, probability)
                    if interrupted_audio is not None:
                        await cancel(interrupted=True)
                        listening = True
                        seed_capture(interrupted_audio)
                        await emit("state", state="Listening")
                    continue
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
                        dialogue.reset()
                    await emit("state", state="Idle")
                elif kind == "played" and command.get("turn_id") != turn_id:
                    # A source callback may already be queued when interruption stops it.
                    continue
                elif kind == "played" and command.get("turn_id") == turn_id and active and awaiting_playback:
                    pending_audio = barge.take_pending()
                    active = awaiting_playback = playback_pending = False
                    segmenter.reset()
                    vad.reset_states()
                    if pending_audio:
                        seed_capture(pending_audio)
                    await emit("state", state="Listening")
                else:
                    await emit("error", message="Unknown command")
    except (WebSocketDisconnect, json.JSONDecodeError, AttributeError):
        pass
    finally:
        await cancel()
        await client.aclose()
