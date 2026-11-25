# 日本語の音声認識を評価する

aivoはOpenAI Python SDKの `audio.transcriptions.create` でローカルOVMSにWAVを送り、言語を `ja`、温度を `0` に指定する。文字誤り率（CER）は参照文から認識文への挿入・削除・置換の合計を、全参照文の文字数で割る。Unicode NFKC正規化と大文字小文字の統一を行い、句読点と空白を除く。「七時」と「7時」、漢字と読み仮名の違いは誤りに含める。

## 実測結果

WindowsのIntel Core Ultra 7 258Vで、多言語版Whisper Large V3 Turboを検証した。FLEURSは人が読んだ日本語音声で、短い応答の検出は別に評価した。

| 評価条件 | 結果 |
| --- | --- |
| FLEURS開発用32件、INT8・CPU、貪欲デコード | 44 / 1,428文字（CER 3.08%） |
| 同じ32件、INT8・CPU、ビーム幅5 | 40 / 1,428文字（2.80%） |
| 同じ32件、FP16・GPU、貪欲デコード | 51 / 1,428文字（3.57%） |
| 同じ32件、FP16・CPU、ビーム幅5 | 45 / 1,428文字（3.15%） |
| 同じ32件、INT8・ビーム幅5にローカルQwenの校正を追加 | 45 / 1,428文字（3.15%） |

設定選びに使わなかった80件では、INT8・貪欲デコードが162 / 3,795文字（4.27%）、ビーム幅5が156 / 3,795文字（4.11%）だった。ビーム幅5で改善した録音は5件、悪化した録音は2件、同数は73件。CERの差は−0.16ポイントで、録音単位の対応付きブートストラップ95%区間は−0.50～+0.13ポイントだった。さらに短い「はい」をビーム幅5で888文字に引き延ばす反復誤りが再現したため、対話ではINT8・貪欲デコードを使う。

Microsoft Haruka DesktopとIchiroで「はい」「いいえ」「うん」など各11発話を速度設定+5で合成し、VADを含めて比較した。INT8の貪欲デコードでは、短い発話の検出修正によって取りこぼしは4件から0件、CERは18 / 42文字（42.86%）から10 / 42文字（23.81%）になった。無音、ファン様雑音、打鍵音など合成対照30件の誤検出は0件。「東京」と「特許」の取り違えや、「七時」と「7時」の表記差は残る。

評価用の人声の先頭12件に信号対雑音比10 dBの合成雑音を重ね、VADとINT8の貪欲デコードを通すと、取りこぼしは0件、CERは36 / 559文字（6.44%）だった。実マイクでの距離、残響、複数人の会話はこの数値の対象外。

## 同じ日本語音声で測定する

OVMSの起動後、リポジトリのルートで次を実行する。1行目は日本語音声のアーカイブを取得し、開発用32件と評価用80件を16 kHzモノラルPCM16に変換する。同じ発話文は各分割で1回までとし、長さ2～25秒から固定シードで抽出する。データは任意の作業用ディレクトリに保存できる。音源は [Google FLEURS 日本語（CC BY 4.0）](https://huggingface.co/datasets/google/fleurs/tree/70bb2e84b976b7e960aa89f1c648e09c59f894dd/data/ja_jp)。

```powershell
uv run --script scripts/prepare_fleurs_ja.py "$env:TEMP/aivo-fleurs"
uv run python -m aivo.check_asr "$env:TEMP/aivo-fleurs/dev.jsonl"
uv run python -m aivo.check_asr "$env:TEMP/aivo-fleurs/test.jsonl"
```

## マイクで録った発話を測定する

録音ごとに16 kHz・モノラル・16ビットPCMのWAVと正解文を用意し、次の形式でUTF-8の `clips.jsonl` に1件ずつ書く。ファイル名はJSONLからの相対パス。無音や生活音の正解文には空文字列を指定する。

```jsonl
{"wav":"hai.wav","reference":"はい"}
{"wav":"keyboard.wav","reference":""}
```

```console
uv run python -m aivo.check_asr clips.jsonl --vad
```

`--vad` は会話と同じSilero VADと発話区間判定を通す。各行の `transcript` を確認し、最終行の `missed_speech` で発話が丸ごと検出されなかった件数、`noise_triggers` で生活音を発話として送った件数を確認できる。発話をすべて取りこぼした場合も、その正解文の文字数がCERの削除誤りに入る。
