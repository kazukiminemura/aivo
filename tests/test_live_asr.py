import asyncio
from types import SimpleNamespace

from aivo.live_asr import LiveASR


def test_final_does_not_wait_for_preview_and_stale_preview_is_discarded():
    async def run():
        release_preview = asyncio.Event()
        started_preview = asyncio.Event()
        completed = asyncio.Queue()

        async def transcribe(audio):
            if len(audio) == 12:
                started_preview.set()
                await release_preview.wait()
                return "old preview"
            return "final"

        async def emit(kind, **data):
            completed.put_nowait((kind, data))

        stream = LiveASR(transcribe, emit)
        segment = SimpleNamespace(voiced=11, silent=0, frames=[bytes(4)] * 11)
        stream.preview(segment)
        assert stream.preview_task is None
        segment.voiced += 1
        segment.frames.append(bytes(4))
        stream.preview(segment)
        await started_preview.wait()
        segment.voiced = 24
        stream.preview(segment)
        await stream.finalize(bytes(256))
        await stream.finalize(bytes(128))
        events = [await completed.get() for _ in range(4)]
        finals = [data for kind, data in events if kind == "transcription"]
        assert finals == [dict(segment=0, text="final"), dict(segment=1, text="final")]
        release_preview.set()
        await stream.preview_task
        assert completed.empty()
        await stream.reset()

    asyncio.run(asyncio.wait_for(run(), 3))
