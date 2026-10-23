"""Streaming and precision checks without sockets, external services, or models."""
import json
import sys
import tempfile
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
        settings = Settings(llm="qwen3:0.6b-q4_K_M", max_tokens=128, context=2048, keep_alive="30m")
        consumed, requests = [], []
        class Stream:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            def raise_for_status(self): pass
            async def aiter_lines(self):
                for line in [json.dumps({"response": '{"reply":"First!'}),
                             json.dumps({"response": 'Second!"}', "done": True})]:
                    consumed.append(line)
                    yield line
        class Client:
            def stream(self, method, url, **kwargs):
                requests.append(kwargs["json"])
                return Stream()
        stream = Backends(settings).chat_stream(Client(), [])
        first = immediate(stream.__anext__())
        self.assertEqual(first[0], "First!")
        self.assertEqual(len(consumed), 2, "no output before complete JSON validation")
        self.assertEqual(immediate(stream.__anext__())[0], "Second!")
        with self.assertRaises(StopAsyncIteration): immediate(stream.__anext__())
        request = requests[0]
        self.assertEqual(request["model"], "qwen3:0.6b-q4_K_M")
        self.assertFalse(request["think"])
        self.assertEqual(request["options"], {"num_predict": 128, "num_ctx": 2048, "temperature": 0.4, "top_p": 0.9, "repeat_penalty": 1.1, "stop": ["<|im_end|>", "<|im_start|>"]})
        self.assertEqual(request["keep_alive"], "30m")

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

    def test_auto_cuda_failure_retries_cpu_int8(self):
        devices = []
        class Whisper:
            def __init__(self, path, **kwargs):
                devices.append((kwargs["device"], kwargs["compute_type"]))
                self.cuda = kwargs["device"] == "cuda"
                self.assert_local = kwargs["local_files_only"]
            def transcribe(self, *args, **kwargs):
                if self.cuda: raise RuntimeError("CUDA library unavailable")
                return [SimpleNamespace(text="ok")], None
        modules = {"faster_whisper": SimpleNamespace(WhisperModel=Whisper),
                   "ctranslate2": SimpleNamespace(get_cuda_device_count=lambda: 1)}
        with tempfile.TemporaryDirectory(dir=".uv-cache/tmp") as path, patch.dict(sys.modules, modules):
            backend = Backends(Settings(stt_model=path, stt_device="auto", stt_compute="auto"))
            self.assertEqual(backend.transcribe([]), "ok")
            self.assertEqual(devices, [("cuda", "int8_float16"), ("cpu", "int8")])
            self.assertEqual((backend.actual_stt_device, backend.actual_stt_compute), ("cpu", "int8"))


if __name__ == "__main__":
    unittest.main()
