"""Streaming and precision checks without sockets, external services, or models."""
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from aivo.backends import Backends
from aivo.core import Settings, speech_chunks


def immediate(coroutine):
    # All awaits in these fakes complete synchronously; unexpected real I/O fails.
    iterator = coroutine.__await__()
    try:
        iterator.send(None)
    except StopIteration as done:
        return done.value
    raise AssertionError("Test unexpectedly suspended on I/O")


class StreamingTests(unittest.TestCase):
    def test_phrase_boundaries_and_bounded_punctuation_wait(self):
        chunks, tail = speech_chunks("\u306f\u3044\u3002next")
        self.assertEqual(chunks, ["\u306f\u3044\u3002"])
        self.assertEqual(tail, "next")
        self.assertEqual(speech_chunks("x" * 49), (["x" * 48], "x"))
        self.assertEqual(speech_chunks("last", final=True), (["last"], ""))
        source = "word " * 30
        parts, tail = speech_chunks(source, final=True)
        self.assertEqual("".join(parts) + tail, source)
        self.assertEqual(speech_chunks("\n\n ", final=True), ([], ""))

    def test_q4_request_buffers_complete_validated_reply_before_playback(self):
        settings = Settings(llm="qwen3:0.6b-q4_K_M", max_tokens=128, context=2048)
        consumed, requests = [], []
        class Stream:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            def raise_for_status(self): pass
            async def aiter_lines(self):
                for line in ['data: ' + json.dumps({"choices": [{"delta": {"content": part}}]})
                             for part in ['{"reply":"First!', 'Second!"}']] + ['data: [DONE]']:
                    consumed.append(line)
                    yield line
        class Client:
            def stream(self, method, url, **kwargs):
                requests.append(kwargs["json"])
                return Stream()
        stream = Backends(settings).chat_stream(Client(), [])
        first = immediate(stream.__anext__())
        self.assertEqual(first[0], "First!")
        self.assertEqual(len(consumed), 3, "no output before complete JSON validation")
        self.assertEqual(immediate(stream.__anext__())[0], "Second!")
        with self.assertRaises(StopAsyncIteration): immediate(stream.__anext__())
        request = requests[0]
        self.assertEqual(request["model"], "qwen3:0.6b-q4_K_M")
        self.assertEqual(request["max_tokens"], 128)

    def test_truncated_stream_raises_instead_of_committing_a_partial_answer(self):
        class Stream:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            def raise_for_status(self): pass
            async def aiter_lines(self):
                yield '{"message":{"content":"partial"}}'
        client = SimpleNamespace(stream=lambda *a, **kw: Stream())
        stream = Backends(Settings()).chat_stream(client, [])
        with self.assertRaisesRegex(RuntimeError, "before completion"):
            immediate(stream.__anext__())

    def test_transcribe_sends_japanese_mono_pcm_wav_to_ovms(self):
        import io
        import wave
        import httpx
        import numpy as np

        import asyncio
        from email.parser import BytesParser
        from email.policy import default

        def handler(request):
            assert request.url.path == "/v3/audio/transcriptions" and request.url.host == "127.0.0.1"
            mime = BytesParser(policy=default).parsebytes(
                f"Content-Type: {request.headers['content-type']}\r\n\r\n".encode() + request.content)
            parts = {part.get_param("name", header="content-disposition"): part for part in mime.iter_parts()}
            assert parts["model"].get_payload(decode=True) == b"whisper-large-v3-turbo"
            assert parts["language"].get_payload(decode=True) == b"ja"
            assert float(parts["temperature"].get_payload(decode=True)) == 0
            with wave.open(io.BytesIO(parts["file"].get_payload(decode=True))) as wav:
                assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (16000, 1, 2)
                assert wav.getnframes() == 512
            return httpx.Response(200, json={"text": " こんにちは "})

        async def invoke():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                settings = Settings(ovms="http://127.0.0.1:8001", stt_model="whisper-large-v3-turbo")
                return await Backends(settings).transcribe(client, np.zeros(512))

        text = asyncio.run(invoke())
        self.assertEqual(text, "こんにちは")


if __name__ == "__main__":
    unittest.main()
