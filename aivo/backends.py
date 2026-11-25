import asyncio
import json
import logging
import threading
from pathlib import Path
from time import perf_counter

import httpx

from aivo.core import speech_chunks
from aivo.answer import parse_final_answer
from aivo.dialogue import STATE_SCHEMA

logger = logging.getLogger("aivo")


def completion_delta(line):
    if not line.startswith("data:"):
        return "", False
    data = line[5:].strip()
    if data == "[DONE]":
        return "", True
    event = json.loads(data)
    if event.get("error"):
        raise RuntimeError(f"OVMS: {event['error']}")
    choices = event.get("choices", [])
    if not choices:
        return "", False
    return choices[0]["delta"].get("content") or "", False


class Backends:
    def __init__(self, settings):
        self.settings = settings
        self.model = None
        self.actual_stt_device = None
        self.actual_stt_compute = None
        # One STT worker: use a process if hard cancellation is required.
        self.lock = threading.Lock()

    def _load_stt(self, force_cpu=False):
        from faster_whisper import WhisperModel
        import ctranslate2
        path = Path(self.settings.stt_model)
        if not path.is_dir():
            raise RuntimeError("STT model missing. Run: uv run python -m aivo.setup")
        device = self.settings.stt_device
        if force_cpu:
            device = "cpu"
        elif device == "auto":
            device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
        compute = self.settings.stt_compute
        if compute == "auto" or force_cpu:
            compute = "int8_float16" if device == "cuda" else "int8"
        self.model = WhisperModel(str(path), device=device, compute_type=compute, local_files_only=True)
        self.actual_stt_device, self.actual_stt_compute = device, compute
        logger.info("stt_loaded device=%s compute=%s model=%s", device, compute, path)

    def transcribe(self, audio):
        with self.lock:
            def run():
                if self.model is None:
                    self._load_stt()
                segments, _ = self.model.transcribe(audio, language="ja", beam_size=3, best_of=1,
                    vad_filter=False, condition_on_previous_text=False, temperature=0,
                    without_timestamps=True)
                return "".join(segment.text for segment in segments).strip()
            try:
                return run()
            except RuntimeError:
                # CUDA libraries can fail only on first inference, after successful model load.
                if self.settings.stt_device != "auto" or self.actual_stt_device == "cpu":
                    raise
                self.model = None
                logger.warning("STT auto CUDA unavailable; falling back to CPU int8")
                self._load_stt(force_cpu=True)
                return run()

    def generation_request(self, messages, dialogue=None, *, response_schema=None, response_instruction=None):
        # Enforce an answer envelope at decoding time, then validate it before
        # anything is handed to playback. Do not stream raw model prose to TTS.
        schema = {"type": "object", "properties": {"reply": {"type": "string", "minLength": 1}},
                  "required": ["reply"], "additionalProperties": False}
        if dialogue is not None:
            schema["properties"]["state"] = STATE_SCHEMA
            schema["required"] = ["reply", "state"]
        wire_messages = [dict(message) for message in messages]
        instruction = ('日本語の会話の返答本文だけをreplyに入れてください。'
                       '出力は{"reply":"返答本文"}というJSONオブジェクト一つだけです。'
                       '思考過程、発言の分析、回答方針の説明をreplyに含めないでください。')
        if dialogue is not None:
            instruction = ('出力はreplyとstateを持つJSON。思考過程を含めない。' + dialogue.instruction())
        if response_schema is not None:
            schema = response_schema
        if response_instruction is not None:
            instruction = response_instruction
        if wire_messages and wire_messages[0]["role"] == "system":
            wire_messages[0]["content"] += "\n" + instruction
        else:
            wire_messages.insert(0, {"role": "system", "content": instruction})
        payload = {"model": self.settings.llm, "messages": wire_messages, "stream": True,
                   "temperature": 0.4, "top_p": 0.9, "chat_template_kwargs": {"enable_thinking": False},
                   "max_tokens": min(2048, self.settings.max_tokens + (384 if dialogue else 0)),
                   "response_format": {"type": "json_schema", "json_schema": {"name": "answer", "schema": schema}}}
        return "/v3/chat/completions", payload

    async def prepare(self, client, tts_backend="voicevox"):
        import numpy as np
        async def preload_llm():
            response = await client.post(self.settings.ovms + "/v3/chat/completions", json={
                "model": self.settings.llm, "messages": [{"role": "user", "content": "はい"}], "max_tokens": 1})
            response.raise_for_status()
        # Load kernels before Listening, including CUDA DLLs used only by STT inference.
        tasks = [asyncio.create_task(asyncio.to_thread(self.transcribe, np.zeros(8000, dtype=np.float32))),
                 asyncio.create_task(preload_llm())]
        if tts_backend == "voicevox":
            tasks.append(asyncio.create_task(self.synthesize(client, "はい。")))
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def chat_stream(self, client, messages, dialogue=None):
        started, first, size, done, raw = perf_counter(), None, 0, False, []
        path, payload = self.generation_request(messages, dialogue)
        async with client.stream("POST", self.settings.ovms + path, json=payload) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                token, finished = completion_delta(line)
                if finished:
                    done = True
                    break
                if token and first is None:
                    first = (perf_counter() - started) * 1000
                size += len(token)
                if size > 12000:
                    raise RuntimeError("LLM output exceeded the response limit")
                raw.append(token)
        if not done:
            raise RuntimeError("OVMS stream ended before completion")
        answer = dialogue.accept_response("".join(raw), user_messages=[m["content"] for m in messages if m["role"] == "user"]) if dialogue is not None else parse_final_answer("".join(raw))
        # Validation must finish before the first chunk can reach UI/TTS/history.
        parts, _ = speech_chunks(answer, final=True)
        for part in parts:
            yield part, round(first or 0, 1)

    async def chat(self, client, messages):
        parts, ttft = [], 0
        async for part, ttft in self.chat_stream(client, messages):
            parts.append(part)
        return "".join(parts), ttft

    async def synthesize(self, client, text):
        params = {"speaker": self.settings.speaker}
        query = await client.post(self.settings.voicevox + "/audio_query", params={**params, "text": text})
        query.raise_for_status()
        audio_query = query.json()
        audio_query["speedScale"] = self.settings.speed
        result = await client.post(self.settings.voicevox + "/synthesis", params=params, json=audio_query)
        result.raise_for_status()
        if len(result.content) > 20_000_000 or not result.content.startswith(b"RIFF"):
            raise RuntimeError("VOICEVOX returned invalid or oversized WAV audio")
        return result.content

    def vad(self):
        import torch
        from silero_vad import load_silero_vad
        torch.set_num_threads(1)
        return load_silero_vad()


async def readiness(settings):
    model = Path(settings.stt_model)
    result = {"stt": all((model / name).is_file() for name in ("model.bin", "config.json", "tokenizer.json")),
              "ovms": False, "voicevox": False, "llm_model": settings.llm,
              "speaker": settings.speaker, "stt_model": settings.stt_model,
              "stt_device": settings.stt_device, "stt_compute": settings.stt_compute, "quantization": None,
              "gpu_loaded": None, "size_vram": None}
    async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
        async def check_ovms():
            response = await client.get(settings.ovms + "/v3/models")
            response.raise_for_status()
            result["ovms"] = any(m["id"] == settings.llm for m in response.json()["data"])
        async def check_voicevox():
            response = await client.get(settings.voicevox + "/speakers")
            response.raise_for_status()
            result["voicevox"] = any(style["id"] == settings.speaker
                                      for speaker in response.json() for style in speaker["styles"])
        checks = [check_ovms()]
        if settings.tts_backend != "browser":
            checks.append(check_voicevox())
        await asyncio.gather(*checks, return_exceptions=True)
    result["tts_backend"] = ("voicevox" if settings.tts_backend == "voicevox" or
                             (settings.tts_backend == "auto" and result["voicevox"]) else "browser")
    result["ready"] = result["stt"] and result["ovms"] and (result["voicevox"] or result["tts_backend"] == "browser")
    return result
