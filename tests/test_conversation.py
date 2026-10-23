"""Conversation regression checks require no models, microphone or event loop."""
import json

import httpx
import pytest

from aivo.backends import Backends
from aivo.check_conversation import TURNS, regression_failures, run_checks
from aivo.core import Settings, build_messages


def test_latest_correction_and_ordered_history_reach_chat_request():
    requests = []
    answers = ["こむぎちゃんですね。", "次に何をしたいですか？", "あずきちゃんに訂正します。", "猫じゃらしがおすすめです。", "あずき"]

    def handler(request):
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"details": {"quantization_level": "Q4_K_M"}})
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": [{"name": "qwen3:4b", "context_length": 4096, "size_vram": 2000}]})
        assert request.url.path == "/api/generate"
        payload = json.loads(request.content)
        requests.append(payload)
        answer = answers[len(requests) - 1]
        return httpx.Response(200, text=json.dumps({"response": json.dumps({"reply": answer}), "done": True}) + "\n")

    settings = Settings(llm="qwen3:4b", context=4096)
    output = []
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert run_checks(client, settings, output.append)
    assert len(requests) == 5
    final = requests[-1]
    prompt = final["prompt"]
    assert final["raw"] is True
    assert prompt.startswith("<|im_start|>system\n")
    assert prompt.endswith("<|im_start|>assistant\n<think>\n\n</think>\n\n")
    positions = [prompt.index(text) for text in TURNS]
    assert positions == sorted(positions)
    answer_positions = [prompt.index(text) for text in answers[:-1]]
    assert answer_positions == sorted(answer_positions)
    assert prompt.index(TURNS[-1]) > answer_positions[-1]
    assert final["think"] is False and final["stream"] is True
    assert final["options"] == Backends(settings).generation_request([])[1]["options"]
    assert final["options"]["num_ctx"] == 4096
    assert any('"quantization": "Q4_K_M"' in line for line in output)
    assert any('"context_length": 4096' in line for line in output)


def test_prompt_guides_conversation_and_does_not_mutate_history():
    history = [{"role": "user", "content": "猫の名前はこむぎです。"},
               {"role": "assistant", "content": "覚えました。"}]
    snapshot = [dict(message) for message in history]
    messages = build_messages(history, "次は、", 4096)
    assert history == snapshot
    assert messages[-1] == {"role": "user", "content": "次は、"}
    policy = messages[0]["content"]
    assert any(word in policy for word in ("曖昧", "不明", "確認", "聞き返"))
    assert any(word in policy for word in ("復唱", "繰り返", "オウム", "補完"))


@pytest.mark.parametrize("replies, expected", [
    (["次は、です。"] * 5, "Identical response"),
    ([TURNS[0], "何をしますか？", "訂正しました。", "猫じゃらし。", "あずき"], "literal input echo"),
    (["覚えました。", "何をしますか？", "訂正しました。", "猫じゃらし。", "こむぎ"], "not recalled"),
])
def test_regressions_are_reported_instead_of_hidden(replies, expected):
    assert any(expected in failure for failure in regression_failures(TURNS, replies))


def test_model_error_does_not_become_a_canned_answer():
    def handler(request):
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"details": {}})
        return httpx.Response(200, text='{"error":"model failed"}\n')
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError, match="model failed"):
            run_checks(client, Settings(), output=lambda line: None)

def test_context_budget_drops_oldest_complete_exchanges():
    history = [
        {"role": "user", "content": "old user" + "a" * 300},
        {"role": "assistant", "content": "old answer" + "b" * 300},
        {"role": "user", "content": "recent question"},
        {"role": "assistant", "content": "recent answer"},
    ]
    snapshot = [dict(message) for message in history]
    messages = build_messages(history, "latest correction", 1536)
    assert [message["role"] for message in messages] == ["system", "user", "assistant", "user"]
    assert messages[1:-1] == history[-2:]
    assert messages[-1]["content"] == "latest correction"
    messages[1]["content"] = "changed result"
    assert history == snapshot


def test_oversized_latest_input_is_preserved_without_partial_history():
    latest = "current utterance" * 200
    messages = build_messages([{"role": "user", "content": "before"},
                               {"role": "assistant", "content": "answer"}], latest, 1024)
    assert [message["role"] for message in messages] == ["system", "user"]
    assert messages[-1]["content"] == latest

def test_two_natural_acknowledgements_are_not_a_fixed_response_loop():
    replies = ["わかりました。", "次に何をしたいですか？", "わかりました。", "猫じゃらしがおすすめです。", "あずき"]
    assert regression_failures(TURNS, replies) == []
