import os
from collections import deque
from dataclasses import dataclass, field
from urllib.parse import urlsplit

SAMPLE_RATE = 16000
FRAME = 512
FRAME_MS = FRAME / SAMPLE_RATE * 1000


def local_url(value):
    url = urlsplit(value)
    if (url.scheme != "http" or url.hostname not in {"localhost", "127.0.0.1", "::1"}
            or url.username or url.password or url.path not in {"", "/"}
            or url.query or url.fragment):
        raise ValueError("Service URLs must be loopback HTTP origins")
    return value.rstrip("/")


@dataclass
class Settings:
    stt_model: str = field(default_factory=lambda: os.getenv("AIVO_STT_MODEL", "models/whisper-small"))
    stt_device: str = field(default_factory=lambda: os.getenv("AIVO_STT_DEVICE", "auto"))
    stt_compute: str = field(default_factory=lambda: os.getenv("AIVO_STT_COMPUTE", "auto"))
    ollama: str = field(default_factory=lambda: local_url(os.getenv("AIVO_OLLAMA_URL", "http://127.0.0.1:11434")))
    llm: str = field(default_factory=lambda: os.getenv("AIVO_LLM_MODEL", "qwen3:4b"))
    voicevox: str = field(default_factory=lambda: local_url(os.getenv("AIVO_VOICEVOX_URL", "http://127.0.0.1:50021")))
    speaker: int = field(default_factory=lambda: int(os.getenv("AIVO_SPEAKER", "3")))
    threshold: float = field(default_factory=lambda: float(os.getenv("AIVO_VAD_THRESHOLD", ".5")))
    silence_ms: int = field(default_factory=lambda: int(os.getenv("AIVO_SILENCE_MS", "800")))

    max_tokens: int = field(default_factory=lambda: int(os.getenv("AIVO_MAX_TOKENS", "192")))
    context: int = field(default_factory=lambda: int(os.getenv("AIVO_CONTEXT", "4096")))
    keep_alive: str = field(default_factory=lambda: os.getenv("AIVO_KEEP_ALIVE", "30m"))

    barge_threshold: float = field(default_factory=lambda: float(os.getenv("AIVO_BARGE_THRESHOLD", ".65")))
    barge_min_ms: int = field(default_factory=lambda: int(os.getenv("AIVO_BARGE_MIN_MS", "160")))

    def __post_init__(self):
        if not .1 <= self.barge_threshold <= .99 or not 64 <= self.barge_min_ms <= 1000:
            raise ValueError("Barge-in threshold: 0.1-0.99; speech duration: 64-1000 ms")
        self.ollama = local_url(self.ollama)
        self.voicevox = local_url(self.voicevox)
        if not 32 <= self.max_tokens <= 2048 or not 1024 <= self.context <= 32768:
            raise ValueError("max_tokens: 32-2048; context: 1024-32768")
        if self.stt_device not in {"auto", "cpu", "cuda"}:
            raise ValueError("STT device must be auto, cpu, or cuda")
        if not .1 <= self.threshold <= .95 or not 200 <= self.silence_ms <= 3000:
            raise ValueError("VAD threshold: 0.1-0.95; silence: 200-3000 ms")


class Segmenter:
    def __init__(self, threshold=.5, silence_ms=800):
        self.threshold, self.silence_ms = threshold, silence_ms
        self.reset()

    def reset(self):
        self.pre = deque(maxlen=8)
        self.frames = []
        self.voiced = self.silent = 0

    def feed(self, frame, probability):
        speech = probability >= self.threshold
        if not self.frames:
            self.pre.append(frame)
            if not speech:
                return None
            self.frames = list(self.pre)
            self.pre.clear()
        else:
            self.frames.append(frame)
        self.voiced += int(speech)
        self.silent = 0 if speech else self.silent + 1
        if self.silent * FRAME_MS >= self.silence_ms or len(self.frames) * FRAME_MS >= 30000:
            result = b"".join(self.frames) if self.voiced * FRAME_MS >= 250 else None
            self.reset()
            return result
        return None


def speech_chunks(text, final=False):
    """Split complete phrases, with a bounded wait for punctuation."""
    chunks = []
    while text:
        boundary = next((i + 1 for i, c in enumerate(text[:48])
                         if c in "。！？!?\n" or (c in "、,，" and i >= 15)), None)
        if boundary is None:
            if len(text) < 48 and not final:
                break
            boundary = min(48, len(text))
        part, text = text[:boundary], text[boundary:]
        if part.strip():
            chunks.append(part)
    return chunks, text


CONVERSATION_PROMPT = """あなたはaivo。日本語で自然に会話し、通常1〜3文で返答する。Markdownや思考過程は出さない。
直近の質問・依頼にまず答える。復唱だけ、定型の「お手伝いできます」、質問を相手に返すだけで終わらない。挨拶から勝手に天気など別の話題へ進めない。
履歴の話題と目的を保つ。否定・訂正されたら自分の誤解を撤回し、直前の依頼に戻る。音声認識らしい不明語に勝手な意味を付けず、履歴で分かる部分を使う。不足情報だけ一つ確認する。
外部検索機能はない。現在・明日の天気など未取得の情報を作らず、確認できないと伝えて必要な地域を尋ねる。
例：
ユーザー「こんにちは」→「こんにちは！」
ユーザー「寒いのよ」→「温かい飲み物でも飲んで、暖かくしてね。」
ユーザー「明日の天気どう？花火を見たい」→「明日の予報はここでは確認できないけど、花火なら雨と風が気になるね。どこの花火？」
ユーザー「花火が見える天気とは言ってない」→「ごめん、明日、花火を見に行けそうか知りたいんだね。場所はどこ？」"""


def build_messages(history, text, context):
    """Keep recent complete exchanges within a conservative character budget.

    Two tokens per character is an estimate, not a tokenizer guarantee.
    Always preserve the system instruction and the current utterance.
    """
    if len(history) % 2:
        raise ValueError("History must contain complete user/assistant exchanges")
    budget = max(0, (context - 512) // 2 - len(CONVERSATION_PROMPT) - len(text))
    exchanges = []
    for index in range(len(history) - 2, -1, -2):
        pair = history[index:index + 2]
        if [m.get("role") for m in pair] != ["user", "assistant"]:
            raise ValueError("History must contain complete user/assistant exchanges")
        size = sum(len(m["content"]) for m in pair)
        if size > budget:
            break
        exchanges.append(pair)
        budget -= size
    messages = [{"role": "system", "content": CONVERSATION_PROMPT}]
    for pair in reversed(exchanges):
        messages.extend({"role": m["role"], "content": m["content"]} for m in pair)
    return [*messages, {"role": "user", "content": text}]
