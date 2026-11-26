import asyncio
from time import perf_counter

import httpx
import numpy as np

from aivo import ovms
from aivo.answer import parse_final_answer
from aivo.core import speech_chunks
from aivo.dialogue import STATE_SCHEMA
from aivo.ovms import completion_delta  # Existing diagnostic import.


class Backends:
    def __init__(self, settings):
        self.settings = settings

    async def transcribe(self, client, audio):
        return await ovms.transcribe(client, self.settings.ovms, self.settings.stt_model, audio)

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
        return ovms.CHAT_PATH, payload

    async def prepare(self, client, tts_backend="voicevox"):
        tasks = [asyncio.create_task(self.transcribe(client, np.zeros(8000, dtype=np.float32))),
                 asyncio.create_task(ovms.preload_llm(client, self.settings.ovms, self.settings.llm))]
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
        started = perf_counter()
        path, payload = self.generation_request(messages, dialogue)
        raw, first_token_ms = await ovms.read_completion(client, self.settings.ovms + path, payload, started)
        if dialogue is None:
            answer = parse_final_answer(raw)
        else:
            user_messages = [m["content"] for m in messages if m["role"] == "user"]
            answer = dialogue.accept_response(raw, user_messages=user_messages)
        # Validation must finish before the first chunk can reach UI/TTS/history.
        parts, _ = speech_chunks(answer, final=True)
        for part in parts:
            yield part, first_token_ms

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
    result = {"stt": False, "stt_backend": "ovms",
              "ovms": False, "voicevox": False, "llm_model": settings.llm,
              "speaker": settings.speaker, "stt_model": settings.stt_model,
              "quantization": None,
              "gpu_loaded": None, "size_vram": None}
    async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
        async def check_ovms():
            ids = await ovms.model_ids(client, settings.ovms)
            result["ovms"] = settings.llm in ids
            result["stt"] = settings.stt_model in ids
            result["stt_preview_model"] = "whisper-small" if "whisper-small" in ids else settings.stt_model
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
