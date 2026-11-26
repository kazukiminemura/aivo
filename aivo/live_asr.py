"""Bounded provisional and final Whisper requests for one microphone connection."""
import asyncio
import logging

import numpy as np


class LiveASR:
    def __init__(self, transcribe, emit, *, preview_request=None):
        self.transcribe = transcribe
        self.preview_request = preview_request or transcribe
        self.emit = emit
        self.segment = 0
        self.last_preview = 0
        self.preview_task = None
        self.final_task = None
        self.finals = asyncio.Queue(maxsize=4)

    def preview(self, segmenter):
        # A frame is 32 ms. Snapshot only when the previous preview has returned.
        count = segmenter.voiced
        if segmenter.silent or count - self.last_preview < 16:
            return
        if self.preview_task and not self.preview_task.done():
            return
        self.last_preview = count
        self.preview_task = asyncio.create_task(self._preview(self.segment, b"".join(segmenter.frames)))

    async def _preview(self, segment, raw):
        try:
            text = await self.preview_request(np.frombuffer(raw, dtype="<f4"))
            if segment == self.segment and text:
                await self.emit("transcription_preview", segment=segment, text=text)
        except Exception as exc:
            logging.getLogger("aivo").warning("STT preview failed: %s", type(exc).__name__)

    def advance(self):
        self.segment += 1
        self.last_preview = 0

    async def finalize(self, raw):
        segment = self.segment
        self.advance()
        await self.emit("transcription_pending", segment=segment)
        if self.final_task is None:
            self.final_task = asyncio.create_task(self._finalize())
        await self.finals.put((segment, raw))

    async def _finalize(self):
        while True:
            segment, raw = await self.finals.get()
            try:
                text = await self.transcribe(np.frombuffer(raw, dtype="<f4"))
            except Exception as exc:
                logging.getLogger("aivo").warning("STT final failed: %s", type(exc).__name__)
                await self.emit("transcription_failed", segment=segment)
            else:
                await self.emit("transcription", segment=segment, text=text)

    async def reset(self):
        self.advance()
        tasks = [task for task in (self.preview_task, self.final_task) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.preview_task = self.final_task = None
        self.finals = asyncio.Queue(maxsize=4)
