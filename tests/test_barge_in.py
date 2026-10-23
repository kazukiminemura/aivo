"""No models, sockets or timing sleeps are needed to verify speech confirmation."""
import unittest

from aivo.barge_in import BargeIn


class BargeInTests(unittest.TestCase):
    def test_requires_five_consecutive_voiced_frames_and_keeps_eight_preroll_frames(self):
        detector = BargeIn()
        for index in range(12):
            self.assertIsNone(detector.feed(bytes([index]), .1))
        for index in range(4):
            self.assertIsNone(detector.feed(bytes([20 + index]), .9))
        self.assertEqual(detector.feed(bytes([24]), .9), bytes(range(4, 12)) + bytes(range(20, 25)))
        self.assertEqual(detector.voiced, 0)
        self.assertFalse(detector.frames)

    def test_single_clap_never_confirms_speech(self):
        detector = BargeIn()
        self.assertIsNone(detector.feed(b"clap", 1))
        for _ in range(20):
            self.assertIsNone(detector.feed(b"silence", .01))

    def test_below_threshold_frame_resets_confirmation(self):
        detector = BargeIn()
        for _ in range(5):
            for _ in range(4):
                self.assertIsNone(detector.feed(b"v", .9))
            self.assertIsNone(detector.feed(b"s", .64))
        self.assertEqual(detector.voiced, 0)

    def test_custom_threshold_duration_and_reset(self):
        detector = BargeIn(threshold=.8, min_speech_ms=64, preroll_frames=0)
        self.assertIsNone(detector.feed(b"ignored", .79))
        self.assertIsNone(detector.feed(b"first", .8))
        self.assertEqual(detector.feed(b"second", .8), b"firstsecond")
        self.assertIsNone(detector.feed(b"old", .9))
        detector.reset()
        self.assertIsNone(detector.feed(b"new", .9))
        self.assertEqual(detector.feed(b"next", .9), b"newnext")

    def test_duration_is_rounded_up_to_complete_frames(self):
        detector = BargeIn(min_speech_ms=160.1)
        for _ in range(5):
            self.assertIsNone(detector.feed(b"v", .9))
        self.assertEqual(detector.feed(b"v", .9), b"v" * 6)

    def test_pending_speech_is_preserved_when_playback_ends_before_confirmation(self):
        detector = BargeIn()
        self.assertEqual(detector.take_pending(), b"")
        detector.feed(b"s", .1)
        self.assertEqual(detector.take_pending(), b"")
        detector.feed(b"pre", .1)
        detector.feed(b"word-start", .9)
        detector.feed(b"word-next", .9)
        self.assertEqual(detector.take_pending(), b"preword-startword-next")
        self.assertEqual(detector.take_pending(), b"")
        self.assertEqual(detector.voiced, 0)
        self.assertFalse(detector.frames)

    def test_invalid_configuration_is_rejected(self):
        for kwargs in ({"threshold": 0}, {"threshold": float("nan")}, {"min_speech_ms": 0},
                       {"min_speech_ms": float("inf")}, {"preroll_frames": -1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                BargeIn(**kwargs)


if __name__ == "__main__":
    unittest.main()
