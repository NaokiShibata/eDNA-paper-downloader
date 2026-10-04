# eDNA-paper-downloader

[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Python: 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![Platform](https://img.shields.io/badge/platform-linux%20%7C%20macOS%20%7C%20windows-lightgrey.svg)](#動作環境)

eDNA関連の文献・プレプリント情報を収集し、CSV/JSONとして保存するPython CLIツールです。

主なスクリプト:

- `script/edna_literature_fetch.py`
  - デフォルトで `PubMed + Crossref + OpenAlex` から統合取得
  - DOI優先、タイトル+年フォールバックで重複統合
  - `Crossref/OpenAlex` は `type` による preprint 除外を実施
  - `--abstract` 有効時は abstract 空レコードを最終出力から除外
  - `--run-biorxiv` で `script/biorxiv_search.py` を連続実行可能
- `script/biorxiv_search.py`
  - bioRxiv/medRxiv APIで期間取得 (`YYYY/MM/DD`)
  - DOIごとに最新版のみ採用（デフォルト）
  - 差分出力 (`*.delta.csv/json`) 対応
- `script/crossref_semantic_fetch.py`
  - Crossref + Semantic Scholar を収集し DOI/タイトル+年で重複統合
  - PubMed CSVを除外リストとして利用可能
- `script/strands_flagger.py`
  - Strands Deciderを使って `abstract` 全文からeDNA/eRNA関連性を高速判定
  - `in_scope` / `out_of_scope` / `unsure` の3段階でスクリーニング
  - 曖昧な論文は `unsure` に残すRecall重視の運用

---

## 動作環境

- Python 3.12
- 文献取得・CSV処理: Linux
- Strands Decider: Ubuntu/Linux + NVIDIA GPU + CUDA
- 環境管理: Pixi
- Pixi環境はリポジトリ直下の `.pixi/` に作成

現在の `pixi.toml` は、Strands DeciderをCUDAで動かす用途に合わせて `linux-64` を対象にしています。

---

## Pixiでの環境構築

依存関係は `pixi.toml` で管理します。Python環境を手動で `venv` / `uv` から作成する必要はありません。

### 1) Pixiをプロジェクト配下にインストール

システム全体にPixiを入れず、このリポジトリ配下の `tools/pixi` に配置する例です。

```bash
export PIXI_VERSION="latest"

export PIXI_HOME="${PWD}/tools/pixi"
export PIXI_BIN_DIR="${PIXI_HOME}/bin"
export PIXI_CACHE_DIR="${PWD}/.cache/pixi"
export PIXI_NO_PATH_UPDATE=1
export TMPDIR="${PWD}/tmp"

mkdir -p "${PIXI_HOME}" "${PIXI_BIN_DIR}" "${PIXI_CACHE_DIR}" "${TMPDIR}"

curl -fsSL https://pixi.sh/install.sh | sh

export PATH="${PIXI_BIN_DIR}:${PATH}"

pixi --version
```

新しいシェルを開く場合は、再度以下を設定してください。

```bash
export PIXI_HOME="${PWD}/tools/pixi"
export PIXI_BIN_DIR="${PIXI_HOME}/bin"
export PIXI_CACHE_DIR="${PWD}/.cache/pixi"
export PIXI_NO_PATH_UPDATE=1
export PATH="${PIXI_BIN_DIR}:${PATH}"
```

### 2) 環境を構築

```bash
pixi install
```

`.pixi/` に環境が作成され、初回solve時に `pixi.lock` が生成されます。
`pixi.lock` は再現性のためGit管理対象としてください。

### 3) Pixi環境を有効化（任意）

既存のREADMEにある `python3 script/...` 形式のコマンドをそのまま使う場合は、先にPixi環境へ入ります。

```bash
pixi shell
```

以降は通常の `python` / `python3` コマンドが `.pixi` 環境を使用します。シェルに入らず実行する場合は `pixi run python ...` でも構いません。

### 4) CUDAを確認

```bash
pixi run cuda-check
```

Tritonは実行時にCUDA Driver API用の小さなC拡張をJITコンパイルするため、`cuda.h` も必要です。
conda-forgeではこのヘッダは `cuda-cudart-dev` から `targets/x86_64-linux/include/` に配置されるため、`pixi.toml` に `cuda-cudart-dev` を含めています。

```bash
pixi run cuda-header-check
pixi run triton-driver-check
```

`cuda-header-check` が `exists: True`、`triton-driver-check` が `libcuda: ok` とGPU targetを表示すれば、
TritonのCUDA初期化まで通っています。

例えば以下のように表示され、`CUDA available: True` になればStrands DeciderをGPUで実行できます。

```text
torch: ...
CUDA available: True
CUDA runtime: ...
GPU: NVIDIA ...
```

### Pixiタスク

`pixi.toml` には以下のタスクを定義しています。

| タスク | 内容 |
| --- | --- |
| `pixi run cuda-check` | PyTorchから見えるCUDA GPUを一覧表示 |
| `pixi run cuda1-check` | `cuda:1` のGPU名と空きVRAMを確認 |
| `pixi run strands-serve` | Strands Deciderを `cuda:1` でport 8012に起動 |
| `pixi run strands-health` | 起動中のStrands Decider APIを確認 |
| `pixi run strands-screen ...` | `script/strands_flagger.py` を実行 |
| `pixi run lint` | Ruff |
| `pixi run typecheck` | mypy |

---

## クイックスタート

```bash
# 1) 文献統合取得 (PubMed + Crossref + OpenAlex)
python3 script/edna_literature_fetch.py \
  --email you@example.com \
  --since 2020/01/01 \
  --out-dir results \
  --out-prefix edna_multisource_2020plus

# 2) bioRxiv 単体取得
python3 script/biorxiv_search.py \
  --from-date 2024/01/01 \
  --to-date 2024/12/31 \
  --query "eDNA" \
  --out-dir results \
  --out-prefix biorxiv_edna_2024
```

---

## 文献統合取得: edna_literature_fetch.py

### ヘルプ

```bash
python3 script/edna_literature_fetch.py --help
```

### 基本例 (デフォルトで全ソース有効)

```bash
python3 script/edna_literature_fetch.py \
  --email you@example.com \
  --query '("environmental DNA"[Title/Abstract] OR eDNA[Title/Abstract])' \
  --since 2020/01/01 \
  --out-dir results \
  --out-prefix edna_multisource_2020plus
```

### 除外語

```bash
python3 script/edna_literature_fetch.py \
  --email you@example.com \
  --query '("environmental DNA"[Title/Abstract] OR eDNA[Title/Abstract])' \
  --exclude 'metagenome OR probiotic OR microbiome' \
  --since 2020/01/01 \
  --out-dir results \
  --out-prefix edna_multisource_no_microbiome
```

`--exclude` は複数指定でき、各値内の `OR` も解釈されます。

### ソース切り替え

```bash
# デフォルト: 3ソースすべて有効
python3 script/edna_literature_fetch.py --email you@example.com

# 例: PubMedのみ
python3 script/edna_literature_fetch.py \
  --email you@example.com \
  --no-source-crossref \
  --no-source-openalex

# 例: Crossref/OpenAlex件数上限を指定
python3 script/edna_literature_fetch.py \
  --email you@example.com \
  --crossref-max-items 2000 \
  --openalex-max-items 2000
```

### `biorxiv_search.py` を連続実行

```bash
python3 script/edna_literature_fetch.py \
  --email you@example.com \
  --since 2020/01/01 \
  --run-biorxiv \
  --biorxiv-from-date 2024/01/01 \
  --biorxiv-to-date 2024/12/31 \
  --biorxiv-query "eDNA"
```

### 要旨を含める (`--abstract`)

```bash
python3 script/edna_literature_fetch.py \
  --email you@example.com \
  --since 2020/01/01 \
  --abstract \
  --out-dir results \
  --out-prefix edna_multisource_with_abstract
```

### CrossrefでDOI補完 (任意)

```bash
python3 script/edna_literature_fetch.py \
  --email you@example.com \
  --since 2020/01/01 \
  --crossref \
  --user-agent "edna-literature-fetch/1.0 (mailto:you@example.com)" \
  --out-dir results \
  --out-prefix edna_multisource_crossref_fill
```

### 色々総まとめ

```bash
python3 script/edna_literature_fetch.py \
  --email you@example.com \
  --query '("environmental DNA"[Title/Abstract] OR eDNA[Title/Abstract])' \
  --exclude 'metagenome OR probiotic OR microbiome' \
  --since 2008/01/01 \
  --until 2025/12/31 \
  --abstract \
  --crossref \
  --crossref-max-items 2000 \
  --openalex-max-items 2000 \
  --run-biorxiv \
  --biorxiv-from-date 2024/01/01 \
  --biorxiv-to-date 2024/12/31 \
  --out-dir results \
  --out-prefix edna_multisource_$(date +%Y%m%d) \
  --log-file logs/edna_multisource_$(date +%Y%m%d).log
```

### 出力列

統合出力には以下の列が入ります。

- `pmid`, `title`, `journal`, `year`, `authors`, `doi`, `abstract`, `pubmed_url`

`pubmed_url` 列には、PubMed以外のソースでは Crossref/OpenAlex 側のURLが入る場合があります。

### 実装上の挙動

- OpenAlex取得は `pyalex` を使用します（要 `pip install pyalex`）。
- OpenAlex取得は `Works().search(...).filter(...).paginate(...)` で実行しています。
  - `per_page=200`
  - `n_max` は `--openalex-max-items` に対応
- `pyalex.config.email` は `--email` の値を設定しています（OpenAlexの polite pool 利用を意図）。
- OpenAlex APIキーは `--openalex-api-key` で設定できます（ログにはマスク表示）。
- `Crossref/OpenAlex` では `type` ベースで preprint を除外します。
- `--abstract` が有効な場合のみ、abstract 空レコードを最終出力から除外します。
- ログは `rich` を使って標準出力に表示されます（RUN HEADER含む）。

### OpenAlex APIメモ (pyalex)

- `pyalex` ではページングは `paginate()` が推奨です（cursor paging がデフォルト）。
- OpenAlexのレスポンス安定化には polite pool が有効です（`pyalex.config.email` 設定）。
- `pyalex` READMEでは、OpenAlex APIは **2026年2月13日** から APIキー必須と案内されています。運用時は `--openalex-api-key` を指定してください。
- 必要なら `pyalex.config.max_retries` / `retry_backoff_factor` / `retry_http_codes` で再試行挙動を調整できます。

### ログ出力 (RUN HEADER)

```bash
python3 script/edna_literature_fetch.py \
  --email you@example.com \
  --since 2020/01/01 \
  --out-dir results \
  --out-prefix edna_multisource \
  --log-file logs/edna_multisource.log \
  --log-level INFO
```

---

## Crossref + Semantic Scholar: crossref_semantic_fetch.py

### 基本例

```bash
python3 script/crossref_semantic_fetch.py \
  --query "environmental DNA eDNA" \
  --max-items 1000 \
  --out-dir results \
  --out-prefix crossref_semantic_edna
```

### PubMed結果を除外

```bash
python3 script/crossref_semantic_fetch.py \
  --query "environmental DNA eDNA" \
  --exclude-pubmed-csv results/edna_multisource_2020plus.csv \
  --out-dir results \
  --out-prefix crossref_semantic_edna_no_pubmed
```

---

## bioRxiv/medRxiv: biorxiv_search.py

`--query`に指定する内容の書き方は、Biorxivの[`Search-Tips`](https://www.biorxiv.org/content/search-tips)を参照下さい。

### ヘルプ

```bash
python3 script/biorxiv_search.py --help
```

### 基本例 (bioRxiv 2024)

```bash
python3 script/biorxiv_search.py \
  --server biorxiv \
  --from-date 2024/01/01 \
  --to-date 2024/12/31 \
  --query "environmental DNA eDNA" \
  --out-dir results \
  --out-prefix biorxiv_edna_2024
```

### 最新版のみ

同一DOIで複数バージョン (v1, v2, …) がある場合、**versionが最大のものだけ**を残します (デフォルト)。

`--all-versions` を付けると全バージョンを残します。

```bash
# 最新版のみ (デフォルト)
python3 script/biorxiv_search.py \
  --from-date 2024/01/01 --to-date 2024/12/31 \
  --query "eDNA" \
  --latest-only

# 全バージョンを保持
python3 script/biorxiv_search.py \
  --from-date 2024/01/01 --to-date 2024/12/31 \
  --query "eDNA" \
  --all-versions
```

### 逐次上書き

途中で停止しても最新出力が残るようになっています。

```bash
python3 script/biorxiv_search.py \
  --server biorxiv \
  --from-date 2024/01/01 \
  --to-date 2024/12/31 \
  --query "environmental DNA eDNA" \
  --exclude microbiome \
  --incremental \
  --out-dir results \
  --out-prefix biorxiv_edna_2024
```

### バッチ処理

週や月単位でのバッチ処理例です。

```bash
python3 script/biorxiv_search.py \
  --server biorxiv \
  --from-date 2024/01/01 \
  --to-date 2024/12/31 \
  --batch-unit monthly \
  --query "environmental DNA eDNA" \
  --out-dir results \
  --out-prefix biorxiv_edna_2024
```

### 差分更新 (既存出力からの更新)

既存出力からの更新処理です。
既存の `out_prefix.csv/json` がある場合、**新規/更新DOIのみ**を `out_prefix.delta.csv/json` に出力します（デフォルト）。

```bash
python3 script/biorxiv_search.py \
  --server biorxiv \
  --from-date 2024/01/01 \
  --to-date 2024/12/31 \
  --query "environmental DNA eDNA" \
  --update \
  --write-delta \
  --out-dir results \
  --out-prefix biorxiv_edna_2024
```

### デバッグログ

- doi/title/date/version を詳細表示する DEBUG 実行モードです。

```bash
python3 script/biorxiv_search.py \
  --server biorxiv \
  --from-date 2024/01/01 \
  --to-date 2024/12/31 \
  --query "environmental DNA eDNA" \
  --log-level DEBUG \
  --debug-max-items 20 \
  --log-file logs/biorxiv_debug.log \
  --out-dir results \
  --out-prefix biorxiv_edna_2024
```

### 出力列 (日付)

bioRxivの出力には以下が入ります。

- `date` / `posted_date` : 投稿/掲載日 (APIのdate)
- `retrieved_at` : 取得時刻 (スクリプト実行時)

---

## 補足 / トラブルシューティング

### PubMed (NCBI Entrez)

- `--email` は必須です
- 大量取得時は `--sleep` を増やすと安定します (例: 0.5〜1.0)
- `python3 script/...` で依存が足りない場合、`.venv/bin/python` が存在すれば自動で再実行します。

### bioRxiv

- APIは期間指定取得が基本で、検索 (boolean) はローカルフィルタです
- `--incremental` は安全性 (途中停止) と引き換えにI/Oが増えるため遅くなります

---

## Strands DeciderでのAbstract判定 (strands_flagger.py)

`script/strands_flagger.py` は、Strands DeciderのHTTP APIを使ってCSVの `abstract` 列を判定します。
現在の論文スクリーニング実装はStrands Deciderに統一しています。

判定は単純なキーワード一致ではなく、1つのAbstractに対して以下を評価します。RTX 5060 TiなどVRAMが限られるGPUでも安定させるため、現在は4質問を1 HTTP requestにまとめず、1質問ずつ順番に送信します。

- `scope`: `in_scope` / `out_of_scope` / `unsure`
- `actual_use`: 環境試料由来DNA/RNAをMethods/Resultsで実際に扱っているか
- `microbial_only`: 一般的な微生物群集・microbiome・metagenomics解析に留まるか
- `method_relevance`: eDNA/eRNAの採取・保存・抽出・検出・定量・解析・モデリング等にMethodとして有用か

タイトルやAbstract中に `eDNA` / `eRNA` の語がなくても、研究内容そのものから判定します。
取りこぼしを減らすため、判断が曖昧な場合は `out_of_scope` に落とさず `unsure` に残します。

### 1) Strands Deciderを起動

Pixi環境を構築後、まずGPU 1を確認します。

```bash
pixi run cuda1-check
```

現在の構成では物理GPU 1のRTX 5060 Tiを使用します。Pixiタスクは `CUDA_VISIBLE_DEVICES=1` を設定してからStrands Deciderを起動するため、Strandsプロセス内ではRTX 5060 Tiが論理 `cuda:0` として見えます。

```bash
pixi run strands-serve
```

`strands-serve` タスクは `CUDA_VISIBLE_DEVICES=1` と `--device cuda` を使用します。古いStrands Decider CLIとの互換性を保つため、`--max-batch` など新しいサーバオプションには依存しません。
prefix cacheを無効にしても判定内容は同じで、主な違いは複数質問時の速度です。

別ターミナルからhealth checkします。

```bash
pixi run strands-health
```

`"status":"ok"` と `"device":"cuda"` が返れば実行可能です。物理GPUの選択は `CUDA_VISIBLE_DEVICES=1` で行っているため、health responseだけでは物理GPU番号は表示されません。

`causal_conv1d` や `flash-linear-attention` が未導入というwarningが出る場合でも、
最適化カーネルを使わないPyTorch実装へフォールバックします。まず判定精度の検証を優先してください。

### 2) 設定ファイルを準備

```bash
cp config/strands_flagger.example.jsonc config/strands_flagger.jsonc
```

デフォルトではCSVの `abstract` 列を使用します。
`max_abstract_chars=null` の場合はAbstract全文を渡します。長い入力を意図的に切り詰めたい場合だけ文字数を指定してください。

主な閾値:

| 設定キー | デフォルト | 意味 |
| --- | ---: | --- |
| `include_threshold` | `0.70` | `P(in_scope)` の自動採用閾値 |
| `actual_use_threshold` | `0.60` | 自動採用時に必要な `actual_use` |
| `exclude_threshold` | `0.50` | `P(out_of_scope)` の自動除外閾値 |
| `exclude_actual_use_max` | `0.50` | 自動除外で許容する `actual_use` の上限 |
| `exclude_method_relevance_max` | `0.60` | 自動除外で許容するMethod関連性の上限 |

現在のデフォルトは、Method系をやや安全側に残す方針で合成benchmarkを用いて調整した値です。曖昧な論文は引き続き `unsure` として人手確認に回します。

### 3) 少数件でテスト

```bash
pixi run strands-screen \
  results/edna_multisource_2020plus.csv \
  --config config/strands_flagger.jsonc \
  --out-csv results/edna_multisource_2020plus.strands.csv \
  --limit 20
```

### 4) 全件実行

```bash
pixi run strands-screen \
  results/edna_multisource_2020plus.csv \
  --config config/strands_flagger.jsonc \
  --out-csv results/edna_multisource_2020plus.strands.csv
```

`resume=true` がデフォルトです。正常に完了した `flag_record_id` はスキップし、
`process_error` は再実行時に再試行します。

### 出力

主な出力列:

| 列名 | 説明 |
| --- | --- |
| `flag_record_id` | DOI優先、無ければタイトル+年による識別子 |
| `flag_label` | `in_scope` / `out_of_scope` / `unsure` / `process_error` |
| `flag_confidence` | `scope` Choiceの分布から計算されるStrands Deciderのconfidence |
| `flag_reason` | 各確率をまとめた機械可読な判定根拠 |
| `flag_model_path` | Strands Deciderサーバが返したモデル名 |
| `flag_prompt_version` | 判定ルールのバージョン |

Strands固有の診断列:

- `strands_scope_choice`
- `strands_p_in_scope`
- `strands_p_out_of_scope`
- `strands_p_unsure`
- `strands_p_actual_use`
- `strands_p_microbial_only`
- `strands_p_method_relevance`
- `strands_latency_ms`（4質問の合計）
- `strands_input_tokens`（4質問の合計）

`flag_confidence` は `P(in_scope)` そのものではありません。採否の検証や閾値調整では
`strands_p_in_scope` などの確率列も確認してください。

## Strands Deciderベンチマーク

ベンチマークの作成、gold labelの基準、calibration/test分割、閾値調整、検証結果は
[docs/strands-decider-benchmark.md](docs/strands-decider-benchmark.md) にまとめています。
