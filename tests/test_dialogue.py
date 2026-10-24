import json

import pytest

from aivo.backends import Backends
from aivo.core import Settings, build_messages
from aivo.dialogue import DialogueState, empty_state
from test_final_response import immediate, mock_client


def response(state, reply="場所はどこ？"):
    return json.dumps({"state": state, "reply": reply}, ensure_ascii=False)


def test_intent_location_correction_and_reset_cross_generation_boundary():
    dialogue = DialogueState()
    first = dict(intent="天気の相談", goal="明日花火を見に行きたい", facts=["明日"],
                 unresolved=["地域", "最新の天気予報"], question="場所はどこ？", status="clarifying")
    requests = []
    client = mock_client([{"response": response(first), "done": True}], requests)
    backend = Backends(Settings())
    stream = backend.chat_stream(client, build_messages([], "明日花火を見たい", 4096), dialogue)
    assert immediate(stream.__anext__())[0] == "場所はどこ？"
    assert dialogue.value == first
    first["facts"].append("outside mutation")
    assert "outside mutation" not in dialogue.value["facts"]
    _, payload = backend.generation_request(build_messages([], "東京", 4096), dialogue)
    assert "明日花火を見に行きたい" in payload["prompt"]
    assert "場所はどこ？" in payload["prompt"]
    assert payload["format"]["required"] == ["reply", "state"]
    corrected = dict(dialogue.value, facts=["日付:明日", "地域:横浜（東京から訂正）"],
                     unresolved=["最新の天気予報"], question="", status="answering")
    dialogue.accept_response(response(corrected, "横浜だね。最新の予報は確認できないから、開催情報も確認してね。"))
    assert dialogue.value["goal"] == "明日花火を見に行きたい"
    assert dialogue.value["unresolved"] == ["最新の天気予報"]
    dialogue.reset()
    assert dialogue.value == empty_state()
    assert DialogueState().value == empty_state()


@pytest.mark.parametrize("invalid", [
    {"reply": "hello"},
    {"reply": "<think>secret</think>", "state": empty_state()},
    {"reply": "hello", "state": dict(empty_state(), status="invented")},
    {"reply": "hello", "state": dict(empty_state(), facts=["x"] * 7)},
    {"reply": "hello", "state": dict(empty_state(), goal="x" * 161)},
    {"reply": "hello", "state": dict(empty_state(), extra="bad")},
])
def test_invalid_generation_never_commits_state_or_emits_audio_text(invalid):
    dialogue = DialogueState()
    before = json.dumps(dialogue.value)
    client = mock_client([{"response": json.dumps(invalid), "done": True}], [])
    stream = Backends(Settings()).chat_stream(client, [], dialogue)
    with pytest.raises(RuntimeError):
        immediate(stream.__anext__())
    assert json.dumps(dialogue.value) == before


def test_duplicate_fields_cannot_override_state():
    dialogue = DialogueState()
    with pytest.raises(RuntimeError):
        dialogue.accept_response('{"reply":"hi","state":{},"state":{}}')
    assert dialogue.value == empty_state()
