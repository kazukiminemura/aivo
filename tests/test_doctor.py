import json
from unittest.mock import patch

import httpx

from aivo.core import Settings
from aivo.doctor import diagnose, main


def settings():
    return Settings(stt_model="models/test", llm="qwen3.5:9b")


def healthy_handler(request):
    if request.url.path == "/api/show":
        assert json.loads(request.content) == {"model": "qwen3.5:9b"}
        return httpx.Response(200, json={"details": {"quantization_level": "Q4_K_M"}})
    if request.url.path == "/api/ps":
        return httpx.Response(200, json={"models": [{"name": "other"}, {"name": "qwen3.5:9b", "context_length": 4096, "size_vram": 5000}]})
    if request.url.path == "/speakers":
        return httpx.Response(200, json=[{"styles": [{"id": 3}]}])
    assert request.url.path == "/api/health"
    s = settings()
    return httpx.Response(200, json={"ready": True, "llm_model": s.llm, "stt_model": s.stt_model,
        "stt_device": s.stt_device, "stt_compute": s.stt_compute, "speaker": s.speaker,
        "web_search": s.web_search, "response_protocol": "final-json-v1", "runtime_id": "test"})


def test_doctor_reports_live_configuration_and_provenance_without_loading_models():
    with httpx.Client(transport=httpx.MockTransport(healthy_handler)) as client, \
         patch("aivo.doctor.Path.is_file", return_value=True), \
         patch("aivo.doctor.Path.stat", return_value=type("Stat", (), {"st_size": 1})()), \
         patch("aivo.doctor.git_state", return_value={"status": "passed", "head": "abc", "dirty": True, "changed_paths": ["aivo/core.py"]}):
        report = diagnose(settings(), client)
    assert report["status"] == "passed"
    assert report["quantization"] == "Q4_K_M"
    assert report["loaded_models"][0]["name"] == "qwen3.5:9b"
    assert report["checks"]["git"]["changed_paths"] == ["aivo/core.py"]
    assert "not model inference" in report["scope"]


def test_blocked_services_and_missing_stt_are_failed_with_explicit_recovery():
    def blocked(request):
        raise httpx.ConnectError("sensitive path should not appear in report")
    with httpx.Client(transport=httpx.MockTransport(blocked)) as client, \
         patch("aivo.doctor.Path.is_file", return_value=False), \
         patch("aivo.doctor.git_state", return_value={"status": "passed", "head": "abc"}):
        report = diagnose(settings(), client)
    assert report["status"] == "failed"
    assert report["checks"]["stt_files"]["missing"] == ["model.bin", "config.json", "tokenizer.json"]
    assert report["checks"]["stt_files"]["recovery"]["command"][-1] == "models/test"
    assert report["checks"]["ollama_model"]["error"] == "ConnectError"
    assert "sensitive path" not in json.dumps(report)
    assert report["running_configuration"] is None


def test_running_server_model_mismatch_is_not_reported_as_ready():
    def stale(request):
        response = healthy_handler(request)
        if request.url.path == "/api/health":
            data = response.json(); data["llm_model"] = "qwen3:4b"
            return httpx.Response(200, json=data)
        return response
    with httpx.Client(transport=httpx.MockTransport(stale)) as client, \
         patch("aivo.doctor.Path.is_file", return_value=True), \
         patch("aivo.doctor.Path.stat", return_value=type("Stat", (), {"st_size": 1})()), \
         patch("aivo.doctor.git_state", return_value={"status": "passed"}):
        report = diagnose(settings(), client)
    assert report["status"] == "failed"
    assert report["checks"]["aivo_server"]["mismatched_fields"] == ["llm_model"]


def test_cli_exit_code_is_nonzero_for_diagnostic_failure(capsys):
    with patch("aivo.doctor.diagnose", return_value={"status": "failed"}):
        assert main([]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "failed"


def test_malformed_provider_data_is_a_failure_instead_of_a_crash():
    def malformed(request):
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"details": []})
        if request.url.path == "/speakers":
            return httpx.Response(200, json=[{"styles": None}])
        return healthy_handler(request)
    with httpx.Client(transport=httpx.MockTransport(malformed)) as client, \
         patch("aivo.doctor.Path.is_file", return_value=True), \
         patch("aivo.doctor.Path.stat", return_value=type("Stat", (), {"st_size": 1})()), \
         patch("aivo.doctor.git_state", return_value={"status": "passed"}):
        report = diagnose(settings(), client)
    assert report["status"] == "failed"
    assert report["checks"]["ollama_model"]["error"] == "InvalidModelDetails"
    assert report["checks"]["voicevox_speaker"]["error"] == "ConfiguredSpeakerMissing"
