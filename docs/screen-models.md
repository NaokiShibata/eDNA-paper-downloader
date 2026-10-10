# 一次判定・unsure再判定モデルの指定

`screen`の`--model1`で一次判定モデル、`--model2`で`unsure`の再判定モデルを指定する。
`--model2`を省略すると`--model1`だけで判定する。
両方を省略した場合は既存設定を維持し、既定設定はStrands→Clef 27Bである。
入力にはタイトル＋抄録を使い、採否閾値・真菌/微生物の除外方針は設定ファイルから引き継ぐ。

| 指定名 | モデル | 既定URL | 実行方式 |
|---|---|---|---|
| `strands` | Strands Decider 2B v19 | `http://127.0.0.1:8012` | 5問順次 |
| `clef` | Clef 27B Q8_0 | `http://127.0.0.1:8014` | GGUF、5問一括 |
| `clef_flash` | Clef-Flash Q8_0 | `http://127.0.0.1:8085` | GGUF、5問一括 |
| `clef_omni` | Clef-Omni FP16 | `http://127.0.0.1:8016` | 非量子化、2GPU、5問一括 |

指定したモデルが設定ファイルのモデルと一致すると、そのURLを使う。
別のモデルなら表のURLを使い、ポートが占有されていれば自分で起動するサーバー用に空きポートを選ぶ。
Strandsの質問送信方式もモデルが一致する設定から引き継ぐ。
一次と再判定に同じモデルは指定できず、`--model2`には`--model1`が必要である。

## 実行例

取得済みCSVのパスを指定する。
次の`results/edna_latest.csv`は、自分の入力ファイルのパスへ置き換える。

```bash
# Strands単独
pixi run screen results/edna_latest.csv --model1 strands --out-csv results/edna_latest.strands.csv

# Strandsでunsureになったものだけ27Bで再判定
pixi run screen results/edna_latest.csv --model1 strands --model2 clef --out-csv results/edna_latest.clef.csv

# StrandsでunsureになったものだけOmni FP16で再判定
pixi run screen results/edna_latest.csv --model1 strands --model2 clef_omni --out-csv results/edna_latest.omni.csv

# Omni単独、またはFlash単独
pixi run screen results/edna_latest.csv --model1 clef_omni --out-csv results/edna_latest.omni-only.csv
pixi run screen results/edna_latest.csv --model1 clef_flash --out-csv results/edna_latest.flash.csv
```

旧引数も互換性のため使える。
`--strands`/`--strandes`は`--model1 strands`、`--clef`は`--model1 clef`、`--both`は`--model1 strands --model2 clef`に相当する。
旧引数と`--model1`/`--model2`は同時に指定できない。

## Omniの準備

この環境では公式重みと隔離した実行環境を取得済みで、再ダウンロードは不要である。
新しい環境では、CUDA対応PyTorch、Accelerate、safetensorsを備えたPythonと、次の固定版を用意する。
Strandsと異なるTransformers版を使うため、依存関係は`.cache/omni-runtime`へ隔離する。
この環境では既存の`.venv/bin/python`を使い、そこになければ`screen`を実行したPythonを使う。

```bash
uv pip install --target .cache/omni-runtime --no-deps -r config/clef_omni_requirements.txt
hf download Cloudflare/clef-omni --revision 0db1cd2607d76a7bdb2a382f659e7b313079f84b --local-dir .cache/clef-omni
```

Omniはローカル重みだけで実行し、有料APIやクラウドGPUは利用しない。
モデルIDに固定revisionとFP16を含め、27Bや8bit版のキャッシュを流用しない。
起動時には重みの読み込み整合性、固定revision、FP16・非量子化・GPU配置を確認する。
画像・音声・動画の入力は受け付けず、テキスト分類だけに対応する。

## Omniを含む二段階処理

FP16のOmniは2枚のGPUを使うので、両モデルを同時に新規起動しない。
一次判定を全件完了して、この実行が起動した一次サーバーだけを停止する。
その後、`unsure`と一次の推論エラー・不正確率だけを再判定モデルへ渡す。
短い抄録の自動除外を保留にする既存ガードも適用し、抄録が空の行は両モデルで推論しない。
再判定対象が0件なら二段目は起動しない。
Omni以外の組み合わせは従来どおり、同じGPUにモデルを配置して行ごとに振り分ける。

一次の確定判定は保持し、出力CSVは入力順に戻す。
CSVの`evaluation_stage`は`first`/`second`で判定した段を示し、`first_stage_model`や`first_stage_label`で一次判定を確認できる。
二段目を待っている途中結果は`evaluation_stage=pending`として保存する。
一次・二次のスコアを別々にキャッシュし、モデルや入力・質問・URLが異なるスコアは再利用しない。
二段目の起動失敗や中断でも一次結果と完了したスコアキャッシュは残り、同じコマンドで再実行できる。
キャッシュヒット時もサーバーの確認・起動は行うため、モデル読み込み時間は必要になる。

既に起動しているサーバーは停止しない。
Omniに必要な容量が空かなければエラーにするので、不要な常駐サーバーは事前に手動で停止する。
今回の配置は先頭37層・入力/出力埋め込み・判定ヘッド・音声/画像エンコーダーを大きいGPUへ、後半11層・最終normをもう一方へ配置する。
表示GPUの余裕を増やすため、判定ヘッドは先日の予備評価と異なり大きいGPUへ置く。
自動起動では概算で48,200MiBと13,700MiB以上の空きがある2枚を選ぶ。
一次サーバー停止後のGPUメモリ解放が反映されるまで、最大5秒間再確認する。
長い入力や他プロセスのメモリ使用によってはOOMになり得る。

モデル・URL・各GPUの名称、計算使用率とVRAM使用量は既存の進捗表示で確認できる。
終了・Ctrl+C・SIGTERM・起動失敗時は、この実行が起動したサーバーだけを停止する。
`--no-auto-server`では指定したモデルのサーバーを事前に起動する。
Omniを手動起動する例は次のとおり。

```bash
CUDA_VISIBLE_DEVICES=0,1 .venv/bin/python script/serve_omni.py --port 8016
```

この引数追加はOmniを既定モデルへ変更するものではない。
分類精度の予備比較と限界は[FP16の2GPU実測](clef-omni-fp16-multigpu.md)を参照。

## 実行ログ

既定の`logs/screen.log`（`--log-file`で変更可能）へ、開始日時・実行Pythonと引数をシェルで引用したコマンド・作業ディレクトリ・入出力の絶対パスを記録する。
`pixi run`など外側のラッパーはPythonから取得できないため、コマンド欄は実際のPythonプロセスの起動内容である。
選択モデル、適用後のURL・閾値・質問送信方式・タイムアウト・キャッシュ設定、プロンプト版、入力/処理対象件数も記録する。
サーバー起動・再利用・停止、GPU情報、キャッシュのヒット数、処理エラーと完了件数・出力先を同じログで確認できる。
ログの詳細度は`--log-level`に従い、これらの通常情報は既定の`INFO`で出力する。

## 動作確認

RTX 8000＋RTX 5060 Tiで、Strands→OmniとOmni→Strandsをそれぞれ3件で実行した。
両方向とも一次で2件を確定し、残り1件だけを再判定して、処理エラーは0件だった。
自動起動したサーバーの終了、2GPUの進捗表示、CSVの入力順保持も確認した。
これはモデル指定と処理経路の動作確認であり、分類精度を再評価した結果ではない。
評価用の入力・出力CSV、ログ、モデル重みはGitへ含めていない。
