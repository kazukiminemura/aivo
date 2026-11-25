"""Explicit online setup; the voice server never downloads models."""
import argparse
import hashlib
import platform
import shutil
import sys
import tempfile
import zipfile
from urllib.request import urlopen
from pathlib import Path


OVMS_VERSION = "2026.4.1"
OVMS_SHA256 = "1bc2372d81bb4767829a7bf513745bd481ccbfec9550e278804ebe2051e3b678"


def install_ovms():
    if sys.platform != "win32" or platform.machine().lower() not in {"amd64", "x86_64"}:
        raise SystemExit("The bundled OVMS setup requires x64 Windows.")
    parent = Path(__file__).resolve().parents[1] / ".runtime"
    target = parent / f"ovms-{OVMS_VERSION}"
    executable = target / "ovms.exe"
    if executable.is_file():
        print(f"OVMS already installed: {executable}")
        return
    parent.mkdir(exist_ok=True)
    url = (f"https://github.com/openvinotoolkit/model_server/releases/download/v{OVMS_VERSION}/"
           f"ovms_windows_{OVMS_VERSION}_python_off.zip")
    print(f"Installing Windows OVMS {OVMS_VERSION} -> {target}", flush=True)
    with tempfile.TemporaryDirectory(dir=parent) as temp:
        archive = Path(temp) / "ovms.zip"
        with urlopen(url, timeout=120) as response, archive.open("wb") as output:
            shutil.copyfileobj(response, output)
        with archive.open("rb") as source:
            if hashlib.file_digest(source, "sha256").hexdigest() != OVMS_SHA256:
                raise RuntimeError("OVMS download checksum mismatch")
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(temp)
        try:
            (Path(temp) / "ovms").rename(target)
        except FileExistsError:
            if not executable.is_file():
                raise
    print(f"OVMS ready: {executable}", flush=True)


def main():
    parser = argparse.ArgumentParser(description="Download local voice models (internet required)")
    parser.add_argument("--model", default="small", help="faster-whisper size; small is the Japanese conversation preset")
    parser.add_argument("--output", help="Model output directory (default: models/whisper-<model>)")
    parser.add_argument("--llm", action="store_true", help="Also download Qwen3-4B INT4 and install OVMS on x64 Windows")
    parser.add_argument("--ovms", action="store_true", help="Install only the Windows x64 OVMS runtime")
    args = parser.parse_args()
    if args.ovms or (args.llm and sys.platform == "win32"):
        install_ovms()
        if args.ovms and not args.llm:
            return
    from faster_whisper.utils import download_model
    output = args.output or f"models/whisper-{args.model}"
    print(f"Downloading STT {args.model} -> {output}", flush=True)
    download_model(args.model, output_dir=str(Path(output)))
    print(f"STT model ready: {output}", flush=True)
    if args.llm:
        from huggingface_hub import snapshot_download

        repo = "OpenVINO/Qwen3-4B-int4-ov"
        destination = Path("models/Qwen3-4B")
        print(f"Downloading {repo} -> {destination}", flush=True)
        snapshot_download(repo_id=repo, local_dir=destination)
        print(f"OVMS model ready: {destination}", flush=True)


if __name__ == "__main__":
    main()
