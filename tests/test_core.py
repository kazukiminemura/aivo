"""Dependency-free checks: python -m unittest discover -s tests -p test_core.py."""
import math
import unittest

from aivo.core import FRAME_MS, Segmenter, Settings, local_url


class CoreTests(unittest.TestCase):
    def test_silence_preroll_and_minimum_voice(self):
        vad = Segmenter(silence_ms=800)
        for _ in range(20):
            self.assertIsNone(vad.feed(b"p", 0))
        for _ in range(8):
            self.assertIsNone(vad.feed(b"v", .9))
        for _ in range(24):
            self.assertIsNone(vad.feed(b"s", 0))
        self.assertEqual(vad.feed(b"s", 0), b"p" * 7 + b"v" * 8 + b"s" * 25)
        self.assertFalse(vad.frames)
        for _ in range(7):
            self.assertIsNone(vad.feed(b"n", .9))
        for _ in range(25):
            self.assertIsNone(vad.feed(b"s", 0))
        self.assertFalse(vad.frames)

    def test_long_speech_is_bounded_and_reset_discards_buffers(self):
        vad = Segmenter()
        count = math.ceil(30000 / FRAME_MS)
        for _ in range(count - 1):
            self.assertIsNone(vad.feed(b"v", .8))
        self.assertEqual(vad.feed(b"v", .8), b"v" * count)
        vad.feed(b"v", .8)
        vad.reset()
        self.assertFalse(vad.frames)
        self.assertFalse(vad.pre)
        self.assertEqual(vad.voiced, 0)

    def test_only_local_http_origins_are_allowed(self):
        for url in ["https://127.0.0.1", "http://example.com", "http://localhost.evil", "http://user:pw@localhost", "http://localhost/path", "http://localhost?x=1", "http://localhost#x"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                local_url(url)
        self.assertEqual(local_url("http://127.0.0.1:11434/"), "http://127.0.0.1:11434")
        self.assertEqual(local_url("http://[::1]:50021"), "http://[::1]:50021")

    def test_settings_validate_threshold_and_silence(self):
        for arguments in [{"threshold": float("nan")}, {"threshold": 0}, {"threshold": 1}, {"silence_ms": 100}, {"silence_ms": 5000}]:
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                Settings(**arguments)


if __name__ == "__main__":
    unittest.main()
