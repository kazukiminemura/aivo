import json
import shutil
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from aivo.core import Settings
from aivo.preferences import public_settings, save_preferences


@pytest.fixture
def prefs_dir():
    root = (Path.cwd() / ".pytest_cache" / "preferences-tests").resolve()
    directory = root / uuid.uuid4().hex
    directory.mkdir(parents=True)
    yield directory
    assert directory.resolve().parent == root
    shutil.rmtree(directory)


def test_settings_save_preserves_unrelated_values_and_does_not_change_runtime(prefs_dir):
    path = prefs_dir / ".env"
    path.write_text("# private settings\nSECRET=keep-this\nAIVO_LLM_MODEL=old\n", encoding="utf-8")
    current = Settings(llm="qwen3:4b", save_history=False)
    values = dict(public_settings(current), llm="qwen3.5:9b", speed=1.2, save_history=True)
    pending = save_preferences(path, values, current)
    assert current.llm == "qwen3:4b" and current.save_history is False
    assert pending.llm == "qwen3.5:9b" and pending.save_history is True
    assert "SECRET=keep-this" in path.read_text(encoding="utf-8")
    assert "AIVO_TTS_SPEED=1.2" in path.read_text(encoding="utf-8")
    assert "AIVO_SAVE_HISTORY=true" in path.read_text(encoding="utf-8")
    assert not list(prefs_dir.glob("*.tmp"))


@pytest.mark.parametrize("change", [
    {"llm": "bad\nAIVO_WEB_SEARCH=true"}, {"speed": float("nan")}, {"speed": 0},
    {"speaker": True}, {"speaker": -1}, {"save_history": "true"}, {"web_search": 1},
    {"stt_device": []}, {"stt_device": "invalid"}, {"unknown": "bad"},
])
def test_invalid_settings_do_not_write_files(prefs_dir, change):
    current = Settings()
    with pytest.raises(ValueError):
        save_preferences(prefs_dir / ".env", dict(public_settings(current), **change), current)
    assert not (prefs_dir / ".env").exists()


def request(method="POST", origin="http://localhost:8000", site=None):
    headers = [(b"host", b"localhost:8000")]
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    if site:
        headers.append((b"sec-fetch-site", site.encode()))
    return Request({"type": "http", "method": method, "path": "/api/settings", "headers": headers})


def test_settings_route_applies_only_history_immediately_and_rejects_cross_origin(prefs_dir, monkeypatch):
    from aivo import server
    monkeypatch.setattr(server, "ROOT", prefs_dir)
    monkeypatch.setattr(server, "settings", Settings(llm="qwen3:4b", save_history=False))
    values = dict(public_settings(server.settings), llm="qwen3.5:9b", save_history=True)
    with pytest.raises(HTTPException) as exc:
        server.update_settings(request(origin="https://evil.example"), values)
    assert exc.value.status_code == 403 and not (prefs_dir / ".env").exists()
    result = server.update_settings(request(), values)
    assert result["restart_required"] is True
    assert server.settings.save_history is True
    assert server.settings.llm == "qwen3:4b"
    server.require_same_origin(request("GET", origin=None, site="same-origin"))
    with pytest.raises(HTTPException):
        server.require_same_origin(request("GET", origin=None, site="cross-site"))


def test_history_routes_list_read_delete_and_clear(prefs_dir, monkeypatch):
    from aivo import server
    from aivo.history import HistoryStore
    store = HistoryStore(prefs_dir / "history.sqlite3")
    monkeypatch.setattr(server, "history_store", store)
    monkeypatch.setattr(server, "settings", Settings(save_history=False))
    req = request("GET")
    assert server.list_history(req) == {"enabled": False, "conversations": []}
    assert not store.path.exists()
    store.append_turn("one", "こんにちは", "こんにちは！")
    store.append_turn("two", "質問", "回答")
    assert len(server.list_history(req)["conversations"]) == 2
    assert server.get_history("one", req)["messages"][0]["content"] == "こんにちは"
    server.delete_history("one", request("DELETE"))
    assert store.get_conversation("one") == []
    server.clear_history(request("DELETE"))
    assert store.list_conversations() == []


def test_tts_speed_reaches_voicevox_without_extra_fields():
    from types import SimpleNamespace
    from aivo.backends import Backends
    from test_final_response import immediate
    requests = []
    async def post(url, **kwargs):
        requests.append((url, kwargs))
        if url.endswith("/audio_query"):
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"speedScale": 1.0})
        return SimpleNamespace(raise_for_status=lambda: None, content=b"RIFFtest")
    backend = Backends(Settings(speed=1.3))
    assert immediate(backend.synthesize(SimpleNamespace(post=post), "こんにちは")) == b"RIFFtest"
    assert requests[-1][1]["json"] == {"speedScale": 1.3}


def test_corrupt_history_returns_recoverable_service_error(prefs_dir, monkeypatch):
    from aivo import server
    from aivo.history import HistoryStore
    path = prefs_dir / "broken.sqlite3"
    path.write_bytes(b"not a sqlite database")
    monkeypatch.setattr(server, "history_store", HistoryStore(path))
    with pytest.raises(HTTPException) as exc:
        server.list_history(request("GET"))
    assert exc.value.status_code == 503
