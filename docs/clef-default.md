# Clef 27B Q8_0の実行例

既定の判定モデルはCloudflare Clefの27B版（ggml-org Q8_0）である。
`screen` と `fetch --strands` は `config/clef_flagger.jsonc` があれば読み込み、なければ `config/clef_flagger.example.jsonc` を使う。
旧 `config/strands_flagger.jsonc` は自動選択しない。
閾値と質問は比較時の固定条件を維持し、スコアキャッシュは無効にする。

## モデル取得と起動

Clef対応のCUDA版 `llama-server` をPATHに配置する。
検証環境はllama.cpp build 11510（`c35b66744`）、RTX 8000 48 GiBの物理GPU 0だった。
観測したGPU全体の使用量は約28.2 GiBで、モデルファイルは約28.7 GBある。

リポジトリのルートで一度だけモデルを取得する。

```bash
mkdir -p .cache/clef
curl -fL --retry 2 https://huggingface.co/ggml-org/Clef-GGUF/resolve/63840a1a68cb7084c88610cffc328509356b04cb/Clef-Q8_0.gguf -o .cache/clef/Clef-Q8_0.gguf
printf '%s  %s\n' 07c6410af7011e0e56873a3b0b3f4ad9e31fb176f0ca80d6fd1525fe6f036548 .cache/clef/Clef-Q8_0.gguf | sha256sum -c -
pixi run serve
```

`pixi run serve` は物理GPU 0を選び、port 8014で `clef-27b-q8` を起動する。
入力全体を物理バッチに収めるため `-c 8192 -b 8192 -ub 8192` を指定している。
別のGPUを使う場合は次のコマンドの `CUDA_VISIBLE_DEVICES` を変更する。

```bash
CUDA_VISIBLE_DEVICES=0 llama-server -m .cache/clef/Clef-Q8_0.gguf --alias clef-27b-q8 --host 127.0.0.1 --port 8014 -ngl 99 -c 8192 -b 8192 -ub 8192 -np 1
```

## CSVを判定する

別ターミナルで実行する。

```bash
pixi run clef-health
pixi run screen results/edna_multisource_2020plus.csv --out-csv results/edna_multisource_2020plus.clef.csv --limit 20
# 全件を判定する場合は --limit を外す
pixi run screen results/edna_multisource_2020plus.csv --out-csv results/edna_multisource_2020plus.clef.csv
```

出力先は毎回書き直す。
`--config` は不要で、既定のClef設定を読み込む。
取得と判定をまとめる例は次のとおり。

```bash
pixi run fetch --query 'environmental DNA' --email you@example.com --strands --out-dir results --out-prefix edna_latest
```

`--strands`、既定の出力接尾辞 `.strands.csv`、`strands_*` 列名は互換性のため維持している。
設定を変える場合は `config/clef_flagger.example.jsonc` を `config/clef_flagger.jsonc` にコピーして編集する。

## 旧Strandsを明示的に使う

```bash
pixi run serve-strands
# 別ターミナル
pixi run screen results/edna_multisource_2020plus.csv --config config/strands_flagger.example.jsonc --out-csv results/edna_multisource_2020plus.strands.csv
```

精度・速度・評価の限界は[27B比較資料](clef-27b-q8-comparison.md)を参照。
