"""Explicit online setup; the voice server never downloads models."""
import argparse
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="Download local voice models (internet required)")
    parser.add_argument("--model", default="small", help="faster-whisper size; small is the Japanese conversation preset")
    parser.add_argument("--output", help="Model output directory (default: models/whisper-<model>)")
    parser.add_argument("--llm", action="store_true", help="Also download the configured Ollama Q4 model")
    args = parser.parse_args()
    from faster_whisper.utils import download_model
    output = args.output or f"models/whisper-{args.model}"
    print(f"Downloading STT {args.model} -> {output}", flush=True)
    download_model(args.model, output_dir=str(Path(output)))
    print(f"STT model ready: {output}", flush=True)
    if args.llm:
        from dotenv import load_dotenv
        from aivo.core import Settings
        load_dotenv(encoding="utf-8-sig")
        settings = Settings()
        print(f"Downloading quantized LLM: {settings.llm}", flush=True)
        subprocess.run(["ollama", "pull", settings.llm], check=True)


if __name__ == "__main__":
    main()
