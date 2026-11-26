"""Explicit online setup; the voice server never downloads models."""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from aivo.ovms_install import install_ovms


def configure_models(runtime, whisper_name, whisper_dir, include_llm, *, small_device="NPU"):
    from huggingface_hub import snapshot_download

    whisper_models = [(whisper_name, whisper_dir)]
    if whisper_name != "whisper-small":
        whisper_models.append(("whisper-small", Path("models/ovms/whisper-small").resolve()))
    graphs = []
    for name, directory in whisper_models:
        small = name == "whisper-small"
        snapshot_download(repo_id=f"OpenVINO/{name}-int8-ov", local_dir=directory)
        subprocess.run([runtime, "--configure", "--model_path", directory, "--task", "speech2text",
                        "--target_device", small_device if small else "CPU",
                        "--num_streams", "1" if small else "2"], check=True)
        graphs.append({"name": name, "base_path": str(directory)})

    llm_dir = Path("models/Qwen3-4B").resolve()
    if include_llm:
        snapshot_download(repo_id="OpenVINO/Qwen3-4B-int4-ov", local_dir=llm_dir)
    if (llm_dir / "openvino_model.bin").is_file():
        subprocess.run([runtime, "--configure", "--model_path", llm_dir, "--task", "text_generation",
                        "--tool_parser", "hermes3", "--reasoning_parser", "qwen3"], check=True)
        graphs.append({"name": "Qwen3-4B", "base_path": str(llm_dir)})
    return graphs


def write_config(path, graphs):
    path.parent.mkdir(exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False, encoding="utf-8") as f:
        json.dump({"model_config_list": [], "mediapipe_config_list": graphs}, f, indent=2)
    os.replace(f.name, path)
    print(f"OVMS configuration ready: {path}")


def main():
    parser = argparse.ArgumentParser(description="Download local voice models (internet required)")
    parser.add_argument("--model", default="large-v3-turbo", help="Multilingual Whisper size (default: large-v3-turbo)")
    parser.add_argument("--small-device", choices=("NPU", "CPU"), default="NPU",
                        help="OVMS inference device for whisper-small (default: NPU)")
    parser.add_argument("--output", help="Model output directory (default: models/ovms/whisper-<model>)")
    parser.add_argument("--llm", action="store_true", help="Also download Qwen3-4B INT4 and install OVMS on x64 Windows")
    parser.add_argument("--ovms", action="store_true", help="Install only the Windows x64 OVMS runtime")
    args = parser.parse_args()
    if args.ovms or sys.platform == "win32":
        runtime = install_ovms()
        if args.ovms and not args.llm:
            return
    else:
        runtime = shutil.which("ovms")
        if not runtime:
            parser.error("Install OVMS and add ovms to PATH before setting up models.")

    name = f"whisper-{args.model}"
    directory = Path(args.output or Path("models/ovms") / name).resolve()
    graphs = configure_models(runtime, name, directory, args.llm, small_device=args.small_device)
    write_config(Path("models/aivo-ovms.json"), graphs)


if __name__ == "__main__":
    main()
