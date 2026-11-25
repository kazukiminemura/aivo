"""HTTP operations for the OpenVINO Model Server."""
import io
import json
import wave
from time import perf_counter

import numpy as np
from openai import AsyncOpenAI

from aivo.core import SAMPLE_RATE

CHAT_PATH = "/v3/chat/completions"


async def model_ids(client, url):
    response = await client.get(url + "/v3/models")
    response.raise_for_status()
    return [entry["id"] for entry in response.json()["data"]]


async def transcribe(client, url, model, audio):
    samples = np.rint(np.clip(audio, -1, 1) * 32767).astype("<i2")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setparams((1, 2, SAMPLE_RATE, 0, "NONE", ""))
        wav.writeframes(samples.tobytes())
    sdk = AsyncOpenAI(base_url=url + "/v3", api_key="local", http_client=client,
                      timeout=client.timeout, max_retries=0)
    response = await sdk.audio.transcriptions.create(
        model=model, language="ja", temperature=0,
        file=("audio.wav", buffer.getvalue(), "audio/wav"),
    )
    return response.text.strip()


async def preload_llm(client, url, model):
    response = await client.post(url + CHAT_PATH, json={
        "model": model, "messages": [{"role": "user", "content": "はい"}], "max_tokens": 1})
    response.raise_for_status()


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


async def read_completion(client, url, payload, started):
    first, size, raw = None, 0, []
    async with client.stream("POST", url, json=payload) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            token, finished = completion_delta(line)
            if finished:
                return "".join(raw), round(first or 0, 1)
            if token and first is None:
                first = (perf_counter() - started) * 1000
            size += len(token)
            if size > 12000:
                raise RuntimeError("LLM output exceeded the response limit")
            raw.append(token)
    raise RuntimeError("OVMS stream ended before completion")
