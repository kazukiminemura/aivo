# aivo

ローカルPCで動かす日本語音声会話アシスタントです。マイクの発話を認識し、Ollamaの最終回答を検証した後、VOICEVOXで文ごとに読み上げます。

**状態: v0.1 MVP実装・実機受け入れ検証待ち。** 日本語で会話が成立することを優先した構成です。ChatGPT Voice Modeと同等の自然さや応答速度を保証するものではありません。

## 初期設定

| 役割 | 実装・設定 |
| --- | --- |
| UI / 音声取得 | ブラウザ標準API、AudioWorklet |
| 音声転送 / API | FastAPI、WebSocket |
| 発話区間検出 | Silero VAD、無音待機800ms |
| 日本語音声認識 | faster-whisper `small`。CUDA `int8_float16`を自動選択、利用不可ならCPU `int8` |
| 回答生成 | ローカルOllama、`qwen3:4b` |
| 応答長 / コンテキスト | 最大192出力トークン / 4096トークン |
| 音声合成 | ローカルVOICEVOX、最終回答の検証後に文単位で合成・順次再生 |
| 会話履歴 | 接続内で保持。直近20往復を上限に、会話の組とコンテキスト予算を考慮 |

この環境の `qwen3:4b` はQ4_K_M量子化モデルです。量子化情報は起動時・確認コマンドで検査します。Q4_K_Mは4bit中心の混合量子化であり、全テンソル、STT、TTSがINT4で動く意味ではありません。

先に速度を優先した0.6Bモデル・Whisper baseでは、同じ回答の繰り返しや認識品質の問題が報告されました。現在は4B / smallへ戻し、断片的な入力には確認する、訂正を受け付ける、単純に入力を繰り返さない対話方針を追加しています。固定回答に置き換えるフィルターではありません。改善の程度は実会話で確認してください。

## セットアップ

Python 3.11〜3.13、[uv](https://docs.astral.sh/uv/)、[Ollama](https://ollama.com/)、[VOICEVOX](https://voicevox.hiroshiba.jp/)を用意してください。Chromium系の最新ブラウザを推奨します。初回の依存パッケージ・モデル取得にはインターネット接続が必要です。

リポジトリルートで実行します。

```console
uv sync
uv run python -m aivo.setup
ollama list
```

STTのsmallモデルを `models/whisper-small` に取得します。Ollamaに `qwen3:4b` が既にあれば、LLMの再取得は不要です。未取得の場合、Ollamaを起動してから実行します。

```console
ollama pull qwen3:4b
```

`uv run python -m aivo.setup --llm` でもSTTと設定済みLLMを取得できます。

初めて設定ファイルを作る場合:

```powershell
# Windows PowerShell
Copy-Item .env.example .env
```

```bash
# Ubuntu
cp .env.example .env
```

既存の `.env` は必要な値だけ編集してください。旧設定の0.6B / baseは自動変更されません。[設定一覧](docs/implementation.md#設定)を確認してください。

## 起動と設定の反映

1. Ollamaを起動します。未起動の場合は別ターミナルで `ollama serve` を実行します。
2. VOICEVOXアプリまたはEngineを `http://127.0.0.1:50021` で利用できる状態にします。
3. aivoを起動します。

```console
uv run uvicorn aivo.server:app --host 127.0.0.1 --port 8000 --ws-max-size 8192
```

[http://127.0.0.1:8000](http://127.0.0.1:8000)を開き、準備状況を確認して会話を開始します。マイク利用を許可してください。AIの生成・再生終了後に次の発話を受け付けます。停止でマイクと再生を止め、新規会話で履歴を消去します。

**`.env` を変更したら、実行中のaivoを停止して起動し直し、ブラウザを再読み込みして新規会話を開始してください。** ファイルを書き換えるだけでは、起動中のモデル設定や既存の会話履歴には反映されません。UIは応答プロトコルと実行IDで旧サーバーを検出し、不一致時には開始を無効にして再起動を案内します。

初期音声はspeaker 3で、クレジットは **VOICEVOX:ずんだもん**。公開・商用利用前に各キャラクター・モデルの規約を確認してください。

## 会話と音声の検証

```console
uv run pytest
uv run python -m aivo.check_conversation
```

後者はローカルOllamaに5ターンを送り、回答の重複、入力の丸写し、名前の訂正・記憶を確認する狭いテキスト回帰チェックです。STT・TTS・実際のマイク音声や、会話品質全般を検証するものではありません。[実機の10ターンチェック](docs/implementation.md#実機受け入れチェック)も行ってください。

画面の認識文が実際の発話と違う場合は、まずSTT・マイクを確認します。スピーカーのAI音声がマイクへ回り込む疑いがある場合は、ヘッドホンで比較してください。

構文上正しいJSONでも、`reply` 内の文章が自然な回答かは別の問題です。最終回答の検証は一般的な意味・会話品質まで保証しません。

## 現在の範囲

- 実行時はローカルモデルのみを読み込み、未取得モデルを自動取得しません。接続先はローカルOllama / VOICEVOXです。
- 録音・会話本文は永続保存しません。アプリのログは処理時間とエラー種別を記録します。
- `final-json-v1` 形式で最終回答全体を検証し、`reply` 本文だけを文単位で読み上げます。生成完了前の読み上げは行いません。音声による割り込み、履歴保存、LAN / スマートフォン接続は未実装です。
- GPU配置はVRAM空き容量などに依存します。他アプリは自動停止しません。`ollama ps` と実行ログを確認してください。
- 最速・目標応答時間の達成、Windows / Ubuntu双方の実音声・オフライン受け入れは未確認です。

## 設計文書

- [要件定義](docs/requirements.md)
- [当初のアーキテクチャ案](docs/architecture.md)
- [開発計画](docs/development-plan.md)
- [現在の実装・検証手順](docs/implementation.md)
- [ADR-001: ローカルMVPの構成](docs/adr-001-local-mvp.md)