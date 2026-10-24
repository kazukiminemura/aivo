"""Live acceptance checks for #4/#9; requires local Ollama and the internet.

Run: uv run python -m aivo.check_web
All prompts are synthetic. Prints actual results, never equates mocks with live PASS.
"""
import argparse
import asyncio
import json
import platform
from dataclasses import replace
from datetime import datetime, timedelta

import httpx
from dotenv import load_dotenv

from aivo.backends import Backends
from aivo.core import Settings
from aivo.dialogue import DialogueState
from aivo.web_search import JST, lookup, lookup_reply


async def run(settings, output=print):
    backend, state = Backends(settings), DialogueState()
    now = datetime.now(JST)
    today, tomorrow = now.date().isoformat(), (now.date() + timedelta(days=1)).isoformat()
    cases = [("今日の天気を教えて", "needs_location", today),
             ("東京", "ok", today),
             ("東京都の今日の天気を教えて", "ok", today),
             ("大阪市の明日の天気を教えて", "ok", tomorrow)]
    output(json.dumps({"environment": platform.platform(), "model": settings.llm,
                       "context": settings.context, "started_at": now.isoformat(),
                       "scope": "live weather/text; microphone/playback excluded"}, ensure_ascii=False))
    history, passed = [], True
    async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
        for text, expected, expected_date in cases:
            result, sources = await lookup(client, backend, history, text, state, now)
            reply = lookup_reply(result, state)
            ok = bool(result and result.get("status") == expected
                      and result.get("requested_date") == expected_date
                      and (expected != "ok" or (result.get("date") == expected_date and sources)))
            output(json.dumps({"input": text, "expected_status": expected,
                               "expected_date": expected_date, "result": result,
                               "reply": reply, "sources": sources, "state": state.value,
                               "weather_request": state.weather_request, "pass": ok}, ensure_ascii=False))
            passed = passed and ok
            if reply:
                history.extend([{"role": "user", "content": text}, {"role": "assistant", "content": reply}])
    output("PASS: limited live weather cases" if passed else "FAIL: live weather acceptance incomplete")
    return passed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", help="Local Ollama model override")
    args = parser.parse_args()
    load_dotenv(encoding="utf-8-sig")
    settings = Settings()
    if args.model:
        settings = replace(settings, llm=args.model)
    return 0 if asyncio.run(run(settings)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
