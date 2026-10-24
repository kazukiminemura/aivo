"""Known semantic failures from GitHub issues #4 and #9.

These deterministic boundary checks do not claim live-model acceptance.
"""
import json
from datetime import datetime, timezone

import pytest

from aivo.dialogue import DialogueState, empty_state
from aivo.web_search import JST, lookup_reply, resolve_weather_plan

NOW = datetime(2026, 10, 24, 12, tzinfo=JST)


def plan(location="東京", day="2026-12-06"):
    return {"tool": "weather", "query": "", "location": location, "date": day}


@pytest.mark.parametrize("utterance, expected", [
    ("東京の今日の天気", "2026-10-24"),
    ("東京の明日の天気", "2026-10-25"),
    ("東京の明後日の天気", "2026-10-26"),
    ("東京の2026年10月25日の天気", "2026-10-25"),
    ("東京の2026-10-25の天気", "2026-10-25"),
])
def test_user_date_overrides_hallucinated_model_date(utterance, expected):
    original = plan()
    resolved = resolve_weather_plan(original, utterance, [], DialogueState(), NOW)
    assert resolved["date"] == expected
    assert original["date"] == "2026-12-06", "Resolver must not mutate the planner output"


def test_relative_date_uses_japan_day_and_handles_year_rollover():
    utc = datetime(2026, 12, 31, 16, tzinfo=timezone.utc)
    resolved = resolve_weather_plan(plan(), "明日の東京の天気", [], DialogueState(), utc)
    assert resolved["date"] == "2027-01-02"


def test_date_not_in_user_request_cannot_be_invented_by_model():
    resolved = resolve_weather_plan(plan(), "東京の天気", [], DialogueState(), NOW)
    assert resolved["date"] == ""


def test_short_location_answer_uses_pending_request_date():
    state = DialogueState()
    state.weather_request = {"location": "", "date": "2026-10-25"}
    resolved = resolve_weather_plan(plan(day=""), "東京", [], state, NOW)
    assert resolved["date"] == "2026-10-25"
    assert resolved["location"] == "東京"
    state.reset()
    assert state.weather_request == {}


@pytest.mark.parametrize("utterance, shortened, original", [
    ("東京都の今日の天気", "東京", "東京都"),
    ("大阪市の明日の天気", "大阪", "大阪市"),
    ("東京の今日の天気", "東京", "東京"),
])
def test_location_keeps_the_spoken_administrative_name(utterance, shortened, original):
    resolved = resolve_weather_plan(plan(location=shortened), utterance, [], DialogueState(), NOW)
    assert resolved["location"] == original


def test_non_weather_search_is_not_rewritten():
    original = {"tool": "search", "query": "明日の花火大会", "location": "", "date": ""}
    assert resolve_weather_plan(original, "明日の花火大会を検索", [], DialogueState(), NOW) == original


def test_search_interval_and_units_are_quoted_without_reinterpretation_or_facts_change():
    state = DialogueState()
    state.commit(dict(empty_state(), facts=["猫の名前はあずきです。"], status="answering"))
    before = json.dumps(state.value, ensure_ascii=False)
    snippet = "予報データは1〜3時間ごとに更新され、風速は秒速5メートルです。"
    result = {"status": "ok", "kind": "search", "query": "予報更新間隔", "results": [
        {"title": "予報について", "url": "https://example.com/weather", "snippet": snippet}],
        "retrieved_at": NOW.isoformat()}
    answer = lookup_reply(result, state)
    assert isinstance(answer, str) and snippet in answer
    assert "1時間に1〜3回" not in answer
    assert "抜粋" in answer, "Unverified search snippets must be identified as excerpts"
    assert json.dumps(state.value, ensure_ascii=False) == before


def test_model_facts_are_grounded_in_user_quotes_and_not_search_evidence():
    state = DialogueState()
    user = "猫の名前はあずきです。"
    proposed = dict(empty_state(), facts=[user, "予報データは1〜3時間ごとに更新される"], status="answering")
    raw = json.dumps({"reply": "あずきちゃんですね。", "state": proposed}, ensure_ascii=False)
    state.accept_response(raw, user_messages=[user])
    assert state.value["facts"] == [user]


def test_model_cannot_relabel_cat_name_as_user_attribute_in_facts():
    state = DialogueState()
    user = "私は猫を飼っています。猫の名前はこむぎです。"
    proposed = dict(empty_state(), facts=["ユーザーの名前はこむぎ", "猫の名前はこむぎです。"], status="answering")
    raw = json.dumps({"reply": "こむぎちゃんですね。", "state": proposed}, ensure_ascii=False)
    state.accept_response(raw, user_messages=[user])
    assert state.value["facts"] == ["猫の名前はこむぎです。"]
