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

    def llm_options(self):
        return {"num_predict": self.settings.max_tokens, "num_ctx": self.settings.context,
                "temperature": 0.4, "top_p": 0.9, "repeat_penalty": 1.1}

    def generation_request(self, messages, dialogue=None):
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
        if wire_messages and wire_messages[0]["role"] == "system":
            wire_messages[0]["content"] += "\n" + instruction
        else:
            wire_messages.insert(0, {"role": "system", "content": instruction})
        payload = {"model": self.settings.llm, "stream": True, "think": False,
                   "keep_alive": self.settings.keep_alive, "options": self.llm_options(), "format": schema}
        if dialogue is not None:
            payload["options"]["num_predict"] = min(2048, self.settings.max_tokens + 384)
        if self.settings.llm.rsplit("/", 1)[-1].split(":", 1)[0].lower() == "qwen3":
            # Bypass the installed legacy template entirely: its trailing <think>
            # survives native /api/chat even when think:false is requested.
            prompt = ""
            for message in wire_messages:
                if message["role"] not in {"system", "user", "assistant"}:
                    raise ValueError("Unsupported conversation role")
                content = message["content"].replace("<|", "＜|")
                prompt += f'<|im_start|>{message["role"]}\n{content}<|im_end|>\n'
            prompt += "<|im_start|>assistant\n<think>\n\n</think>\n\n"
            payload.update(raw=True, prompt=prompt)
            payload["options"]["stop"] = ["<|im_end|>", "<|im_start|>"]
            return "/api/generate", payload
        payload["messages"] = wire_messages
        return "/api/chat", payload

    async def prepare(self, client):
        import numpy as np
        async def preload_llm():
            response = await client.post(self.settings.ollama + "/api/generate", json={
                "model": self.settings.llm, "prompt": "", "stream": False,
                "keep_alive": self.settings.keep_alive, "options": self.llm_options(),
            })
            response.raise_for_status()
            if response.json().get("error"):
                raise RuntimeError("Ollama model preload failed")
        # Load kernels before Listening, including CUDA DLLs used only by STT inference.
        tasks = [asyncio.create_task(asyncio.to_thread(self.transcribe, np.zeros(8000, dtype=np.float32))),
                 asyncio.create_task(preload_llm()), asyncio.create_task(self.synthesize(client, "はい。"))]
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
        async with client.stream("POST", self.settings.ollama + path, json=payload) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line:
                    continue
                item = json.loads(line)
                if item.get("error"):
                    raise RuntimeError("Ollama returned an error. Check the model and server.")
                # The separate thinking field is never an answer.
                token = item.get("response", "") if path == "/api/generate" else item.get("message", {}).get("content", "")
                if token and first is None:
                    first = (perf_counter() - started) * 1000
                size += len(token)
                if size > 12000:
                    raise RuntimeError("LLM output exceeded the response limit")
                raw.append(token)
                if item.get("done"):
                    done = True
                    break
        if not done:
            raise RuntimeError("Ollama stream ended before completion")
        answer = dialogue.accept_response("".join(raw)) if dialogue is not None else parse_final_answer("".join(raw))
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
        result = await client.post(self.settings.voicevox + "/synthesis", params=params, json=query.json())
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
              "ollama": False, "voicevox": False, "llm_model": settings.llm,
              "speaker": settings.speaker, "stt_model": settings.stt_model,
              "stt_device": settings.stt_device, "stt_compute": settings.stt_compute, "quantization": None,
              "gpu_loaded": False, "size_vram": 0}
    async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
        async def check_ollama():
            response = await client.post(settings.ollama + "/api/show", json={"model": settings.llm})
            response.raise_for_status()
            result["quantization"] = response.json().get("details", {}).get("quantization_level")
            # A successful show confirms this exact model exists locally, without pulling it.
            result["ollama"] = True
            response = await client.get(settings.ollama + "/api/ps")
            response.raise_for_status()
            for loaded in response.json().get("models", []):
                if loaded.get("name") in {settings.llm, settings.llm + ":latest"}:
                    result["size_vram"] = loaded.get("size_vram", 0)
                    result["gpu_loaded"] = result["size_vram"] > 0
        async def check_voicevox():
            response = await client.get(settings.voicevox + "/speakers")
            response.raise_for_status()
            result["voicevox"] = any(style["id"] == settings.speaker
                                      for speaker in response.json() for style in speaker["styles"])
        await asyncio.gather(check_ollama(), check_voicevox(), return_exceptions=True)
    result["ready"] = all(result[k] for k in ("stt", "ollama", "voicevox"))
    return result
