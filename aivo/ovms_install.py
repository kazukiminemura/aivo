"""Install the pinned Windows OVMS runtime in the project directory."""
import hashlib
import platform
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path
from urllib.request import urlopen


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
        return executable
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
    return executable
