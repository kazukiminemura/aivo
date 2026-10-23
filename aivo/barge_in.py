"""Sustained-speech confirmation for barge-in; probabilities come from Silero.

This is not acoustic echo cancellation. Playback echo must be reduced by the
browser's AEC and the microphone/speaker arrangement.
"""
import math
from collections import deque

from aivo.core import FRAME_MS


class BargeIn:
    def __init__(self, threshold=.65, min_speech_ms=160, preroll_frames=8):
        if not 0 < threshold <= 1 or not math.isfinite(min_speech_ms) or min_speech_ms <= 0:
            raise ValueError("Barge-in requires a positive speech duration and threshold in (0, 1]")
        if not isinstance(preroll_frames, int) or preroll_frames < 0:
            raise ValueError("preroll_frames must be a nonnegative integer")
        self.threshold = threshold
        self.min_speech_ms = min_speech_ms
        self.preroll_frames = preroll_frames
        self.required_frames = math.ceil(min_speech_ms / FRAME_MS)
        self.frames = deque(maxlen=preroll_frames + self.required_frames)
        self.voiced = 0

    def reset(self):
        self.frames.clear()
        self.voiced = 0

    def take_pending(self):
        audio = b"".join(self.frames) if self.voiced else b""
        self.reset()
        return audio

    def feed(self, frame, probability):
        self.frames.append(frame)
        self.voiced = self.voiced + 1 if probability >= self.threshold else 0
        if self.voiced < self.required_frames:
            return None
        audio = b"".join(self.frames)
        self.reset()
        return audio
