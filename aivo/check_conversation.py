"""Text-only regression check against the configured local Ollama (no STT/TTS).

Run: uv run python -m aivo.check_conversation
This is a narrow smoke check, not a general model-quality benchmark.
"""
import argparse
import json
import sys
from collections import Counter
from dataclasses import replace
from time import perf_counter

import httpx
from dotenv import load_dotenv

from aivo.backends import Backends
from aivo.answer import parse_final_answer
from aivo.dialogue import DialogueState
from aivo.core import Settings, build_messages

TURNS = (
    "私は猫を飼っています。名前はこむぎです。覚えてください。",
    "次は、",
    "訂正します。猫の名前は「あずき」です。新しい名前で覚えてください。",
    "その猫におすすめのおもちゃを一つ提案してください。",
    "私の猫の今の名前は何ですか。名前だけ答えてください。",
)


WEATHER_TURNS = (
    "こんにちは",
    "寒いのよ",
    "明日の天気ってどうなんかね、花火見たいんだけど",
    "花火が見える天気と言ってませんよ",
    "フォガビじゃなくて花火。明日見に行きたいの。場所は東京。",
)


def weather_failures(inputs, replies):
    """Narrow failure signatures from a real conversation, not a quality score."""
    failures = []
    if len(replies) != len(inputs):
        failures.append("Missing conversation turns")
    for i, (text, reply) in enumerate(zip(inputs, replies)):
        if i > 0 and normalized(text) == normalized(reply):
            failures.append(f"Turn {i + 1}: literal input echo")
        if "お手伝いできること" in reply:
            failures.append(f"Turn {i + 1}: canned response instead of continuing the topic")
    if replies and "天気" in replies[0]:
        failures.append("Greeting introduced an unsolicited weather topic")
    if len(replies) > 2:
        answer = replies[2]
        if "明日はどんな" in answer or "花火が見える天気なんですね" in answer:
            failures.append("Weather question was reversed or misinterpreted")
        if not any(w in answer for w in ("確認でき", "わから", "分から", "取得でき", "調べられ", "調べる機能", "予報は見", "アクセスでき")):
            failures.append("Unavailable forecast was not acknowledged")
    if len(replies) > 3 and any(w in replies[3] for w in ("薄い煙", "霧という", "フォガという")):
        failures.append("Invented a meaning for a recognition error")
    if len(replies) > 4 and any(w in replies[4] for w in ("場所はどこ", "どこの花火", "どの地域")):
        failures.append("Asked again for the location already supplied")
    return failures


def normalized(text):
    return "".join(c for c in text if c.isalnum()).casefold()


def regression_failures(inputs, replies):
    failures = []
    clean = [normalized(reply) for reply in replies]
    if any(not reply for reply in clean):
        failures.append("Empty answer")
    if any(count >= 3 for count in Counter(clean).values()):
        failures.append("Identical response repeated across different turns")
    for index, (text, reply) in enumerate(zip(inputs, replies), 1):
        if normalized(text) == normalized(reply):
            failures.append(f"Turn {index}: literal input echo")
    if replies and ("あずき" not in replies[-1] or "こむぎ" in replies[-1]):
        failures.append("Latest corrected cat name was not recalled (expected あずき)")
    return failures


def stream_reply(client, settings, messages, dialogue=None):
    backend = Backends(settings)
    path, payload = backend.generation_request(messages, dialogue)
    started, first, parts, done = perf_counter(), None, [], False
    with client.stream("POST", settings.ollama + path, json=payload) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if not line:
                continue
            event = json.loads(line)
            if event.get("error"):
                raise RuntimeError(f"Ollama: {event['error']}")
            token = event.get("response", "") if path == "/api/generate" else event.get("message", {}).get("content", "")
            if token and first is None:
                first = (perf_counter() - started) * 1000
            parts.append(token)
            if event.get("done"):
                done = True
                break
    if not done:
        raise RuntimeError("Ollama stream ended before completion")
    answer = dialogue.accept_response("".join(parts)) if dialogue is not None else parse_final_answer("".join(parts))
    return answer, {"ttft_ms": round(first or 0, 1),
                                    "total_ms": round((perf_counter() - started) * 1000, 1)}


def run_checks(client, settings, output=print, scenario="memory", stateful=False):
    dialogue = DialogueState() if stateful else None
    turns = WEATHER_TURNS if scenario == "weather" else TURNS
    details = client.post(settings.ollama + "/api/show", json={"model": settings.llm})
    details.raise_for_status()
    output(json.dumps({"requested_model": settings.llm,
                       "quantization": details.json().get("details", {}).get("quantization_level"),
                       "requested_context": settings.context,
                       "generation_options": Backends(settings).llm_options()}, ensure_ascii=False))
    history, replies = [], []
    for index, text in enumerate(turns, 1):
        budget = settings.context - 2 * len(dialogue.instruction()) - 384 if dialogue is not None else settings.context
        answer, timing = stream_reply(client, settings, build_messages(history, text, budget), dialogue)
        if dialogue is not None:
            output(json.dumps({"state": dialogue.value}, ensure_ascii=False))
        replies.append(answer)
        output(json.dumps({"turn": index, "user": text, "assistant": answer, **timing}, ensure_ascii=False))
        history.extend([{"role": "user", "content": text}, {"role": "assistant", "content": answer}])
    loaded = client.get(settings.ollama + "/api/ps")
    loaded.raise_for_status()
    observed = [model for model in loaded.json().get("models", [])
                if model.get("name") in {settings.llm, settings.llm + ":latest"}]
    output(json.dumps({"observed_loaded_models": [
        {key: model.get(key) for key in ("name", "context_length", "size_vram")}
        for model in observed], "note": "null means the engine did not report this field"}, ensure_ascii=False))
    failures = weather_failures(turns, replies) if scenario == "weather" else regression_failures(turns, replies)
    for failure in failures:
        output("FAIL: " + failure)
    output("FAIL" if failures else "PASS: five-turn text smoke check (limited failure signatures)")
    return not failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", help="Override AIVO_LLM_MODEL for comparison")
    parser.add_argument("--scenario", choices=("memory", "weather"), default="memory")
    args = parser.parse_args()
    load_dotenv(encoding="utf-8-sig")
    settings = Settings()
    if args.model:
        settings = replace(settings, llm=args.model)
    try:
        with httpx.Client(timeout=httpx.Timeout(120, connect=5), trust_env=False) as client:
            return 0 if run_checks(client, settings, scenario=args.scenario, stateful=True) else 1
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        print(f"CHECK NOT COMPLETED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
