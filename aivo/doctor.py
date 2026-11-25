"""Read-only prerequisite diagnostics. No downloads, model loading, or configuration writes."""
import argparse
import json
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

from aivo.core import Settings, local_url


def git_state(root):
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
                              text=True, timeout=5, check=True).stdout.strip()
        names = subprocess.run(["git", "status", "--porcelain=v1", "-z"], cwd=root,
                               capture_output=True, text=True, timeout=5, check=True).stdout
        records = names.split("\0")
        paths, index = [], 0
        while index < len(records):
            record = records[index]
            if record:
                paths.append(record[3:])
                if "R" in record[:2] or "C" in record[:2]:
                    index += 1
                    if index < len(records):
                        paths.append(records[index])
            index += 1
        return {"status": "passed", "head": head, "dirty": bool(paths),
                "changed_paths": sorted(set(paths))}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "failed", "error": type(exc).__name__}


def diagnose(settings, client, *, root=None, app_url="http://127.0.0.1:8000"):
    """Return explicit outcomes; passed prerequisites do not mean accepted voice quality."""
    app_url = local_url(app_url)
    root = Path(root or Path.cwd())
    checks = {"python_runtime": {"status": "passed" if (3, 11) <= sys.version_info[:2] < (3, 14) else "failed"}}

    def request(name, method, url, **kwargs):
        try:
            response = client.request(method, url, **kwargs)
            response.raise_for_status()
            value = response.json()
            checks[name] = {"status": "passed"}
            return value
        except (httpx.HTTPError, ValueError) as exc:
            outcome = {"status": "failed", "error": type(exc).__name__}
            if isinstance(exc, httpx.HTTPStatusError):
                outcome["http_status"] = exc.response.status_code
            checks[name] = outcome
            return None

    catalog = request("ovms_model", "GET", settings.ovms + "/v3/models")
    observed = []
    models = catalog.get("data") if isinstance(catalog, dict) else None
    if isinstance(models, list):
        observed = [m for m in models if isinstance(m, dict) and m.get("id") in {settings.llm, settings.stt_model}]
    model_ids = {m["id"] for m in observed}
    checks["ovms_stt"] = dict(checks["ovms_model"])
    for key, name in (("ovms_model", settings.llm), ("ovms_stt", settings.stt_model)):
        if catalog is not None and name not in model_ids:
            checks[key] = {"status": "failed", "error": "ConfiguredModelMissing", "model": name}
    speakers = request("voicevox_speaker", "GET", settings.voicevox + "/speakers")
    if speakers is not None:
        valid = isinstance(speakers, list) and any(
            style.get("id") == settings.speaker for speaker in speakers if isinstance(speaker, dict)
            for style in (speaker.get("styles") if isinstance(speaker.get("styles"), list) else []) if isinstance(style, dict))
        if not valid:
            checks["voicevox_speaker"] = {"status": "failed", "error": "ConfiguredSpeakerMissing"}

    if settings.tts_backend == "browser" or (settings.tts_backend == "auto" and checks["voicevox_speaker"]["status"] != "passed"):
        checks["voicevox_speaker"] = {**checks["voicevox_speaker"], "status": "passed", "mode": "browser_fallback",
                                      "note": "VOICEVOX is optional. Japanese local speech must be checked in the actual browser."}

    running = request("aivo_server", "GET", app_url + "/api/health")
    effective = None
    if running is not None:
        if not isinstance(running, dict):
            checks["aivo_server"] = {"status": "failed", "error": "InvalidHealthResponse"}
        else:
            keys = ("llm_model", "stt_model", "speaker", "web_search",
                    "response_protocol", "lookup_revision", "runtime_id", "stt_backend")
            effective = {key: running.get(key) for key in keys}
            expected = {"llm_model": settings.llm, "stt_model": settings.stt_model,
                        "speaker": settings.speaker, "web_search": settings.web_search}
            mismatches = [key for key, value in expected.items() if running.get(key) != value]
            if mismatches or running.get("response_protocol") != "final-json-v1" or not running.get("ready"):
                checks["aivo_server"] = {"status": "failed", "error": "ServerNotReadyOrConfigurationMismatch",
                                         "mismatched_fields": mismatches, "recovery": "Restart aivo, reload the browser, and start a new conversation."}
    checks["git"] = git_state(root)
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(), "python": sys.version.split()[0],
        "platform": platform.platform(), "status": "passed" if all(c["status"] == "passed" for c in checks.values()) else "failed",
        "scope": "Read-only prerequisites; not model inference, microphone/playback, quality, offline, or latency acceptance.",
        "configuration": {"llm_model": settings.llm, "stt_model": settings.stt_model,
                          "speaker": settings.speaker, "web_search": settings.web_search,
                          "context": settings.context, "max_tokens": settings.max_tokens},
        "checks": checks, "quantization": None, "loaded_models": observed,
        "running_configuration": effective,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-url", default="http://127.0.0.1:8000", type=local_url)
    args = parser.parse_args(argv)
    load_dotenv(".env", encoding="utf-8-sig")
    try:
        settings = Settings()
        with httpx.Client(timeout=3, trust_env=False) as client:
            report = diagnose(settings, client, app_url=args.app_url)
    except (ValueError, OSError) as exc:
        report = {"status": "failed", "checks": {"configuration": {"status": "failed", "error": type(exc).__name__}},
                  "scope": "No acceptance result; diagnostic could not complete."}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
