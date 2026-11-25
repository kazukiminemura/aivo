"""Measure Japanese character error rate on a JSONL manifest of reference text and 16 kHz mono WAVs."""
import argparse
import asyncio
import json
import unicodedata
import wave
from pathlib import Path
from time import perf_counter

import httpx
import numpy as np
from dotenv import load_dotenv

from aivo.backends import Backends
from aivo.core import FRAME, SAMPLE_RATE, Segmenter, Settings, local_url
from aivo.ovms import transcribe


def normalize(text):
    text = unicodedata.normalize("NFKC", text).casefold()
    return "".join(c for c in text if not c.isspace() and not unicodedata.category(c).startswith("P"))


def edit_distance(reference, actual):
    row = list(range(len(actual) + 1))
    for i, char in enumerate(reference, 1):
        next_row = [i]
        for j, other in enumerate(actual, 1):
            next_row.append(min(row[j] + 1, next_row[-1] + 1, row[j - 1] + (char != other)))
        row = next_row
    return row[-1]


def speech_segments(audio, vad, settings):
    import torch

    vad.reset_states()
    segmenter = Segmenter(settings.threshold, settings.silence_ms)
    # Reproduce a live input stream with silence before and after the recording.
    trailing = FRAME * (2 + int(settings.silence_ms * SAMPLE_RATE / 1000 / FRAME))
    padded = np.pad(audio, (8 * FRAME, trailing + (-len(audio)) % FRAME))
    for frame in padded.reshape(-1, FRAME):
        probability = vad(torch.from_numpy(frame), SAMPLE_RATE).item()
        segment = segmenter.feed(frame.tobytes(), probability)
        if segment is not None:
            yield np.frombuffer(segment, dtype="<f4")


async def evaluate(manifest, url, model, settings, use_vad):
    errors = characters = count = 0
    missed_speech = noise_triggers = 0
    vad = Backends(settings).vad() if use_vad else None
    latencies = []
    async with httpx.AsyncClient(trust_env=False, timeout=120) as client:
        for line in manifest.read_text(encoding="utf-8").splitlines():
            item = json.loads(line)
            with wave.open(str(manifest.parent / item["wav"]), "rb") as wav:
                if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) != (SAMPLE_RATE, 1, 2):
                    raise ValueError(f"Expected 16 kHz mono PCM16: {item['wav']}")
                audio = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2").astype(np.float32) / 32768
            started = perf_counter()
            segments = list(speech_segments(audio, vad, settings)) if use_vad else [audio]
            parts = []
            for segment in segments:
                parts.append(await transcribe(client, url, model, segment))
            actual = "".join(parts)
            seconds = perf_counter() - started
            reference = normalize(item["reference"])
            missed_speech += bool(reference) and not segments
            noise_triggers += not reference and bool(segments)
            distance = edit_distance(reference, normalize(actual))
            errors += distance
            characters += len(reference)
            count += 1
            latencies.append(seconds)
            print(json.dumps(dict(wav=item["wav"], transcript=actual, errors=distance, chars=len(reference),
                                  seconds=round(seconds, 3)), ensure_ascii=False), flush=True)
    if not count:
        raise ValueError("Empty manifest")
    print(json.dumps(dict(model=model, clips=count, errors=errors, chars=characters,
                          cer=errors / characters if characters else None, vad=use_vad,
                          missed_speech=missed_speech if use_vad else None,
                          noise_triggers=noise_triggers if use_vad else None,
                          median_seconds=float(np.median(latencies))), ensure_ascii=False))


def main():
    load_dotenv(encoding="utf-8-sig")
    settings = Settings()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--model", default=settings.stt_model)
    parser.add_argument("--url", type=local_url, default=settings.ovms)
    parser.add_argument("--vad", action="store_true", help="Include aivo's live speech segmentation")
    args = parser.parse_args()
    asyncio.run(evaluate(args.manifest, args.url, args.model, settings, args.vad))


if __name__ == "__main__":
    main()
