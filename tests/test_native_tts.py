import httpx
import pytest

from aivo.backends import readiness
from aivo.core import Settings
from test_final_response import immediate


@pytest.mark.parametrize("mode, engine_ready, expected, ready", [
    ("auto", False, "browser", True), ("auto", True, "voicevox", True),
    ("browser", False, "browser", True), ("voicevox", False, "voicevox", False),
])
def test_readiness_can_use_browser_without_voicevox(monkeypatch, mode, engine_ready, expected, ready):
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, **kwargs):
            return httpx.Response(200, json={"details": {"quantization_level": "Q4_K_M"}}, request=httpx.Request("POST", url))
        async def get(self, url):
            if url.endswith("/speakers"):
                if not engine_ready: raise httpx.ConnectError("not running")
                return httpx.Response(200, json=[{"styles": [{"id": 3}]}], request=httpx.Request("GET", url))
            return httpx.Response(200, json={"models": []}, request=httpx.Request("GET", url))
    async def gather(*coroutines, return_exceptions=False):
        values = []
        for coroutine in coroutines:
            try: values.append(await coroutine)
            except Exception as exc:
                if not return_exceptions: raise
                values.append(exc)
        return values
    monkeypatch.setattr("aivo.backends.httpx.AsyncClient", lambda **kwargs: Client())
    monkeypatch.setattr("aivo.backends.asyncio.gather", gather)
    monkeypatch.setattr("aivo.backends.Path.is_file", lambda self: True)
    status = immediate(readiness(Settings(tts_backend=mode)))
    assert status["voicevox"] == engine_ready
    assert status["tts_backend"] == expected and status["ready"] == ready


def test_invalid_tts_backend_is_rejected():
    with pytest.raises(ValueError): Settings(tts_backend="cloud")
