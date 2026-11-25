# /// script
# requires-python = ">=3.11"
# dependencies = ["av", "numpy"]
# ///
"""Prepare the Japanese FLEURS development and test samples used in the ASR comparison."""
import argparse
import csv
import io
import json
import random
import tarfile
import wave
from pathlib import Path
from urllib.request import urlretrieve

import av
import numpy as np

REVISION = "70bb2e84b976b7e960aa89f1c648e09c59f894dd"
SOURCE = f"https://huggingface.co/datasets/google/fleurs/resolve/{REVISION}/data/ja_jp"


def download(url, path):
    if not path.exists():
        partial = path.with_suffix(path.suffix + ".part")
        urlretrieve(url, partial)
        partial.replace(path)


def prepare(root, split, count):
    metadata = root / f"{split}.tsv"
    archive = root / f"{split}.tar.gz"
    download(f"{SOURCE}/{split}.tsv", metadata)
    download(f"{SOURCE}/audio/{split}.tar.gz", archive)
    with metadata.open(encoding="utf-8", newline="") as source:
        rows = list(csv.reader(source, delimiter="\t", quoting=csv.QUOTE_NONE))
    random.Random(7113).shuffle(rows)
    prompts = set()
    clips = {}
    for prompt, filename, reference, _, _, samples, *_ in rows:
        if prompt in prompts or not 2 <= int(samples) / 16000 <= 25:
            continue
        prompts.add(prompt)
        clips[filename] = {"wav": f"{split}/{filename}", "reference": reference}
        if len(clips) == count:
            break

    (root / split).mkdir(exist_ok=True)
    written = set()
    with tarfile.open(archive, "r|gz") as recordings:
        for member in recordings:
            name = Path(member.name).name
            if name not in clips or not member.isfile():
                continue
            with av.open(io.BytesIO(recordings.extractfile(member).read())) as audio:
                frames = list(audio.decode(audio=0))
            if any(frame.sample_rate != 16000 or len(frame.layout.channels) != 1 for frame in frames):
                raise ValueError(f"Expected 16 kHz mono: {name}")
            pcm = np.concatenate([frame.to_ndarray().ravel() for frame in frames])
            if pcm.dtype.kind == "f":
                pcm = np.rint(np.clip(pcm, -1, 1) * 32767).astype("<i2")
            elif pcm.dtype != np.int16:
                raise ValueError(f"Unexpected audio format: {name}")
            with wave.open(str(root / split / name), "wb") as wav:
                wav.setparams((1, 2, 16000, 0, "NONE", ""))
                wav.writeframes(pcm.astype("<i2", copy=False).tobytes())
            written.add(name)
    if len(written) != count:
        raise ValueError(f"{split}: extracted {len(written)} of {count} clips")
    manifest = root / f"{split}.jsonl"
    manifest.write_text("".join(json.dumps(clip, ensure_ascii=False) + "\n" for clip in clips.values()),
                        encoding="utf-8")
    print(manifest)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    directory = parser.parse_args().output
    directory.mkdir(parents=True, exist_ok=True)
    prepare(directory, "dev", 32)
    prepare(directory, "test", 80)
