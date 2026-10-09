# Clef 27B Q8_0の実行例

既定の判定モデルはCloudflare Clefの27B版（ggml-org Q8_0）である。
`screen` と `fetch --strands` は `config/clef_flagger.jsonc` があれば読み込み、なければ `config/clef_flagger.example.jsonc` を使う。
旧 `config/strands_flagger.jsonc` は自動選択しない。
質問は `edna-macrofauna-v6` を使用し、同一入力・質問・モデル名のスコアキャッシュを有効にする。
速度改善の二段階判定とキャッシュの詳細は[速度改善の手順](clef-speed.md)を参照。
微生物・microbiomeだけの研究は対象外とする。
真菌（Fungi）自体の検出・同定・監視・分布・多様性を目的とする研究も対象外とし、酵母・カビ・キノコ・真菌病原体を含める。
eDNA/eRNAや真菌検出手法の開発であっても対象外であり、真菌を大型無脊椎動物とは扱わない。例外は、大型脊椎動物・無脊椎動物の検出・監視に直接関わる手法の検討である。
微生物のみの検出、群集解析、活性測定はeDNAを使っていてもこの例外に含めない。
microbial-only確率が `exclude_microbial_only_min`（既定0.80）以上で、対象動物に関わる手法関連性が `exclude_method_relevance_max`（既定0.60）以下なら除外する。
0.80は保守的な初期値で、独立した評価データで調整した値ではない。抄録が300文字未満なら自動除外せず要確認にする。

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

`--strands` と既定の出力接尾辞 `.strands.csv` は互換性のため維持している。
CSVの確率列は `p_in_scope`、`p_out_of_scope`、`p_unsure`、`p_actual_use`、`p_microbial_only`、`p_method_relevance`、`p_review` を使う。
モデルのscope回答は `scope_choice`、最終判定は `flag_label`、モデル名は `flag_model_path` に記録する。
`latency_ms` と `input_tokens` もモデル名に依存しない。既存CSVは上書きせず、再実行して新しい質問・列名の結果を生成する。
benchmarkのeval/tuneは旧 `strands_*` 列と新列の両方を読み込める。
以前の比較資料は旧質問・旧判定ルールでの結果であり、今回の精度を示すものではない。
設定を変える場合は `config/clef_flagger.example.jsonc` を `config/clef_flagger.jsonc` にコピーして編集する。

## 旧Strandsを明示的に使う

```bash
pixi run serve-strands
# 別ターミナル
pixi run screen results/edna_multisource_2020plus.csv --config config/strands_flagger.example.jsonc --out-csv results/edna_multisource_2020plus.strands.csv
```

精度・速度・評価の限界は[27B比較資料](clef-27b-q8-comparison.md)を参照。

## 変更後の動作確認

ローカルで取得済みの先頭20件を27Bで再判定し、処理エラー0件、採用8件、除外11件、要確認1件だった。
南極の微生物群集、空中細菌、果樹園土壌群集、空中菌類の4研究は除外し、サケのeDNA検出研究は採用を維持した。
微生物のDNA同位体標識手法は要確認だった。
確定ラベルに基づく精度評価ではなく、独立データでの再評価は未実施である。
評価CSVはローカルに保持し、コミット・pushしない。

真菌除外を明記したv6では、実データ5件を二段階で確認した。
空中菌類、真菌検出手法、土壌真菌の3件を除外し、サケと大型無脊椎動物のeDNA研究2件は採用した。
処理エラーは0件で、これは少数件の動作確認であり全体の精度評価ではない。
