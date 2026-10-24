"""Editable local preferences; preserve unrelated .env values."""
import os
import re
import uuid
from dataclasses import replace
from pathlib import Path
from threading import Lock

KEYS = {"llm": "AIVO_LLM_MODEL", "speaker": "AIVO_SPEAKER", "speed": "AIVO_TTS_SPEED",
        "stt_device": "AIVO_STT_DEVICE", "web_search": "AIVO_WEB_SEARCH", "save_history": "AIVO_SAVE_HISTORY"}
LOCK = Lock()


def public_settings(settings):
    return {key: getattr(settings, key) for key in KEYS}


def save_preferences(path, values, current):
    if not isinstance(values, dict) or set(values) != set(KEYS):
        raise ValueError("設定項目が不足、または未対応の設定が含まれています。")
    if not isinstance(values["llm"], str) or not re.fullmatch(r"[a-zA-Z0-9_.:/-]{1,120}", values["llm"]):
        raise ValueError("モデル名が不正です。")
    if type(values["speaker"]) is not int or type(values["speed"]) not in (int, float):
        raise ValueError("声は整数、速度は数値で指定してください。")
    if type(values["save_history"]) is not bool or type(values["web_search"]) is not bool:
        raise ValueError("保存・検索の設定は真偽値で指定してください。")
    if not isinstance(values["stt_device"], str) or values["stt_device"] not in {"auto", "cpu", "cuda"}:
        raise ValueError("音声認識デバイスはauto / cpu / cudaで指定してください。")
    pending = replace(current, **values)
    path = Path(path)
    with LOCK:
        content = path.read_text(encoding="utf-8-sig") if path.exists() else ""
        lines = content.splitlines()
        for key, env in KEYS.items():
            value = values[key]
            rendered = str(value).lower() if type(value) is bool else str(value)
            line = f"{env}={rendered}"
            indexes = [i for i, text in enumerate(lines) if re.match(r"^\s*(?:export\s+)?" + env + r"\s*=", text)]
            if indexes:
                for index in indexes:
                    lines[index] = line
            else:
                lines.append(line)
        temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return pending
