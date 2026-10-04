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
  - 既存の `flag_*` 列と互換で、`llama_flagger_overlap.py` にそのまま入力可能
  - 曖昧な論文は `unsure` に残すRecall重視のスクリーニング

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

## llama.cppの導入とビルド

llama.cppをローカルで使うための最小手順です。

### CPUビルド (最小)

```bash
git clone https://github.com/ggerganov/llama.cpp.git
cd llama.cpp
cmake -S . -B build
cmake --build build -j
```

### GPUビルド (CUDA例)

NVIDIA GPU + CUDA環境の例です。

```bash
git clone https://github.com/ggerganov/llama.cpp.git
cd llama.cpp
cmake -S . -B build -DGGML_CUDA=ON
cmake --build build -j
```

ビルド後、`build/bin/llama-cli` と `build/bin/llama-server` を利用します。
環境によってはGPUバックエンドが異なるため、公式READMEの該当手順も参照してください。

---

## llama.cppでの簡易RAG

csvにまとめた文献メタデータを使って、llama.cppでRAG風のQAを行う簡易スクリプトです。
以下はローカルのllama.cppサーバ (OpenAI互換API) を前提にしています。
`edna_literature_fetch.py` のCSVはデフォルト列でそのまま使えます。
`biorxiv_search.py` のCSVは列名が異なるため、`--text-cols`/`--context-cols` を指定してください。

### 1) llama.cppサーバを起動

```bash
./path/to/llama-server -m /path/to/your-model.gguf --port 8080 --embedding
```

※ 別の埋め込みモデルを使う場合は、別ポートで起動し `--embed-url` を分けて指定します。

### 2) CSVから埋め込みインデックス作成

PubMed (edna_literature_fetch.py) のCSV:

```bash
python script/llama_rag_csv.py index \
  results/edna_multisource_2020plus.csv \
  --out-index results/edna_multisource_2020plus.index.jsonl \
  --text-cols title,journal,year,authors,doi
```

bioRxiv (biorxiv_search.py) のCSV:

```bash
python script/llama_rag_csv.py index \
  results/biorxiv_results.csv \
  --out-index results/biorxiv_results.index.jsonl \
  --text-cols title,authors,doi,date,category,abstract,biorxiv_url
```

### 3) 質問する

```bash
python script/llama_rag_csv.py ask \
  results/edna_multisource_2020plus.index.jsonl \
  "Which papers mention CRISPR-Cas and what are the DOIs?" \
  --top-k 5 \
  --show-sources
```

bioRxivのindexに対する質問例:

```bash
python script/llama_rag_csv.py ask \
  results/biorxiv_results.index.jsonl \
  "Which preprints focus on metabarcoding and what are the DOIs?" \
  --context-cols title,authors,doi,date,category,biorxiv_url \
  --top-k 5 \
  --show-sources
```

### 追加例: 埋め込みとLLMを別URLで指定

```bash
python script/llama_rag_csv.py ask \
  results/edna_multisource_2020plus.index.jsonl \
  "Summarize trends in eDNA monitoring from 2020 onward." \
  --embed-url http://localhost:8081 \
  --embed-api openai \
  --llm-url http://localhost:8080 \
  --llm-api openai-chat \
  --top-k 8 \
  --show-sources
```

### 補足

- `--embed-api` / `--llm-api` で `openai` (OpenAI互換) と `legacy` を切り替えできます。
- CSVにabstractがある場合は `--text-cols` に含めると回答品質が上がります。

---

## ダウンロード済み論文のフラグ付け (llama.cpp CLI)

`edna_literature_fetch.py`で取得したcsvファイルを精査し、論文情報にフラグをつけます。`llama-cli`を使用します。

検討時は下記モデルを使用しました。

- gpt-oss-120b-Q4_K_M-00001-of-00002.gguf & gpt-oss-120b-Q4_K_M-00002-of-00002.gguf
- gemma-3-4b-it-abliterated.q5_k.gguf
- gpt-oss-20b-Q4_K_M.gguf

### 1) 設定ファイルを準備 (JSONC)

```bash
cp config/llama_flagger.example.jsonc config/llama_flagger.jsonc
```

`scope` と `model_path` を設定してください。

#### 設定項目 (llama_flagger.jsonc)

| 設定キー | 必須 | 説明 |
| --- | --- | --- |
| `scope` | yes | 対象論文の範囲を1〜3文で記述 |
| `model_path` | yes | 使用するGGUFモデルのパス |
| `out_csv` | no | 出力CSVのパス |
| `log_file` | no | ログファイルのパス |
| `log_level` | no | ログレベル (`DEBUG` / `INFO` / `WARNING` / `ERROR`) |
| `prompt_template` | no | プロンプト全体のテンプレート (指定時は内部テンプレを置換) |
| `llama_bin` | no | `llama-cli` のパス (省略時はPATH検索) |
| `llama_args` | no | `llama-cli` の追加引数 (例: `--no-conversation`) |
| `reuse_process` | no | `true` でモデルを1回ロードして使い回し |
| `batch_size` | no | バッチ件数 (例: `500`) |
| `batch_index` | no | バッチ番号 (0始まり) |
| `threads` | no | 使用スレッド数 |
| `limit` | no | 読み込み件数の上限 (`batch_size` とは同時指定不可) |
| `resume` | no | `true` で既存フラグ行をスキップ (`false` で再処理) |
| `dry_run` | no | `true` でプロンプトのみ表示して終了 |
| `max_tokens` | no | 生成トークン数 |
| `sampling_temperature` | no | サンプリング温度 |
| `ctx_size` | no | コンテキスト長 |
| `timeout` | no | 1件あたりのタイムアウト秒 |
| `include_hint` | no | in-scopeの補助ヒント (文字列 or 配列) |
| `exclude_hint` | no | out-of-scopeの補助ヒント (文字列 or 配列) |

※ `llama_args` の `--no-conversation` は会話テンプレートを無効化し、単純なテキスト生成として扱う指定です。

設定のポイント:

- `ctx_size` を `llama_args` に指定した場合は、その値が優先されます (configの `ctx_size` は無視されます)。
- `resume=true` は入力CSVに `flag_*` が埋まっている行、または出力CSVに `flag_*` が埋まっている行をスキップします (`flag_record_id` だけの行は再処理されます)。
- CSVに `abstract` 列がある場合は、文頭+末尾の抜粋を自動的に使います。
- `prompt_template` を指定すると内部のプロンプト生成を上書きします (JSON出力の指示も含めて記述してください)。

設定例 (gpt-oss):

```jsonc
{
  "scope": "environmental DNA/RNA papers for ecology and monitoring",
  "model_path": "/path/to/gpt-oss-20b-Q4_K_M.gguf",
  "llama_bin": "/path/to/llama-cli",
  "llama_args": "--threads 16 --no-conversation",
  "log_file": "logs/llama_flagger.log",
  "log_level": "INFO",
  "prompt_template": null,
  "ctx_size": null,
  "sampling_temperature": null,
  "reuse_process": true
}
```

設定値の具体例:

| 設定キー | 型 | 例 | 補足 |
| --- | --- | --- | --- |
| `scope` | string | `Environmental DNA/RNA papers for ecology and monitoring.` | 1〜3文推奨 |
| `model_path` | string | `/models/gpt-oss-20b-Q4_K_M.gguf` | GGUFファイル |
| `out_csv` | string | `results/flagged.csv` | 出力CSV |
| `log_file` | string/null | `logs/llama_flagger.log` | ログファイル |
| `log_level` | string | `INFO` | ログレベル |
| `prompt_template` | string/null | `null` | プロンプトテンプレ |
| `llama_bin` | string | `/path/to/llama-cli` | PATH上のコマンド名でも可 |
| `llama_args` | string/null | `--threads 16 --no-conversation` | 追加CLI引数 |
| `ctx_size` | int/null | `8192` | `llama_args` の指定が優先 |
| `threads` | int | `16` | CPUスレッド数 |
| `reuse_process` | bool | `true` | モデルを使い回す |
| `batch_size` | int | `500` | |
| `batch_index` | int | `0` | 0始まり |
| `limit` | int/null | `100` | `batch_size` と併用不可 |
| `resume` | bool | `true` | 既存フラグ行をスキップ |
| `dry_run` | bool | `false` | プロンプトのみ表示 |
| `max_tokens` | int | `256` | 生成トークン数 |
| `sampling_temperature` | float/null | `0.05` | 低いほど安定 |
| `timeout` | float | `300` | 秒 |
| `include_hint` | string/array | `eDNA, eRNA, metabarcoding` | ヒント |
| `exclude_hint` | string/array | `microbiome, metagenomics` | ヒント |

### 2) 実行

```bash
python script/llama_flagger.py \
  results/edna_multisource_2020plus.csv \
  --config config/llama_flagger.jsonc \
  --out-csv results/edna_multisource_2020plus.flagged.csv
```

### 実行例 (コマンド指定)

```bash
python3 /path/to/eDNA-paper-downloader/script/llama_flagger.py \
  results/edna_multisource_20260118.csv \
  --out-csv results/edna_multisource_20260118plus.flagged.csv \
  --model /path/to/model.gguf \
  --llama-bin /path/to/llama.cpp/build/bin/llama-cli \
  --scope "You are screening papers using only the title and abstract. Classify a paper as OUT-OF-SCOPE if the primary focus is: - Microbial or algal community profiling (e.g., 16S rRNA, ITS, 18S rRNA, rbcL used to characterize microbial/algal communities or microbiome composition), - Host-associated microbiomes or microbiota (gut, skin, oral, rumen, dysbiosis, probiotics), - Shotgun metagenomics or related approaches (shotgun sequencing, metagenome, MAGs, genome assembly, binning), - Functional or applied microbial themes such as resistome, antimicrobial resistance (AMR), virome, wastewater-based epidemiology, or microbial biogeochemistry. Classify a paper as IN-SCOPE when the study uses environmental DNA or RNA (eDNA/eRNA) from environmental samples (water, soil, sediment, air, etc.) to detect, monitor, or assess the presence, distribution, abundance, or biodiversity of: - Animals (vertebrates or invertebrates), - Plants or macrophytes, - Or microorganisms when the study focuses on detecting specific microbial taxa from environmental DNA (NOT microbiome/community profiling). Marker gene guidance: - Do NOT classify a paper as OUT-OF-SCOPE solely because markers such as "16S", "18S", "28S", or "COI" appear. - Treat these markers as OUT-OF-SCOPE only when they are used primarily for microbial/algal community profiling. - Treat these markers as IN-SCOPE when used to detect non-microbial organisms (e.g., vertebrates, invertebrates, macrofauna, macroflora), or to detect specific microbial taxa via eDNA rather than profiling whole communities. If the focus is ambiguous or cannot be clearly determined from the title and abstract alone, prefer OUT-OF-SCOPE to minimize false positives." \
  --exclude-hint "microbiome; microbiota; gut microbiome; gut microbiota; skin microbiome; oral microbiome; rumen microbiome; dysbiosis; probiotic; metagenomics; shotgun metagenomics; metagenome; metagenome-assembled genome; MAG; genome assembly; binning; resistome; antimicrobial resistance; AMR; virome; viral metagenomics; bacteriome; mycobiome; 16S profiling; 16S community profiling; ITS community profiling; wastewater epidemiology" \
  --reuse-process --model-profile gemma
```

`--scope`に指定するプロンプト例

```English
You are screening papers using only the title and abstract. Classify a paper as OUT-OF-SCOPE if the primary focus is: - Microbial or algal community profiling (e.g., 16S rRNA, ITS, 18S rRNA, rbcL used to characterize microbial/algal communities or microbiome composition), - Host-associated microbiomes or microbiota (gut, skin, oral, rumen, dysbiosis, probiotics), - Shotgun metagenomics or related approaches (shotgun sequencing, metagenome, MAGs, genome assembly, binning), - Functional or applied microbial themes such as resistome, antimicrobial resistance (AMR), virome, wastewater-based epidemiology, or microbial biogeochemistry. Classify a paper as IN-SCOPE when the study uses environmental DNA or RNA (eDNA/eRNA) from environmental samples (water, soil, sediment, air, etc.) to detect, monitor, or assess the presence, distribution, abundance, or biodiversity of: - Animals (vertebrates or invertebrates), - Plants or macrophytes, - Or microorganisms when the study focuses on detecting specific microbial taxa from environmental DNA (NOT microbiome/community profiling). Marker gene guidance: - Do NOT classify a paper as OUT-OF-SCOPE solely because markers such as "16S", "18S", "28S", or "COI" appear. - Treat these markers as OUT-OF-SCOPE only when they are used primarily for microbial/algal community profiling. - Treat these markers as IN-SCOPE when used to detect non-microbial organisms (e.g., vertebrates, invertebrates, macrofauna, macroflora), or to detect specific microbial taxa via eDNA rather than profiling whole communities. If the focus is ambiguous or cannot be clearly determined from the title and abstract alone, prefer OUT-OF-SCOPE to minimize false positives.
```

`--exclude-hint`に指定するプロンプト例

```English
microbiome; microbiota; gut microbiome; gut microbiota; skin microbiome; oral microbiome; rumen microbiome; dysbiosis; probiotic; metagenomics; shotgun metagenomics; metagenome; metagenome-assembled genome; MAG; genome assembly; binning; resistome; antimicrobial resistance; AMR; virome; viral metagenomics; bacteriome; mycobiome; 16S profiling; 16S community profiling; ITS community profiling; wastewater epidemiology
```

### 出力

元CSVの列に加えて、以下の列が追加されます。

| 列名                  | 説明                                                                               |
| --------------------- | ---------------------------------------------------------------------------------- |
| `flag_record_id`      | 識別子 (DOI優先、無ければタイトル+年)                                              |
| `flag_label`          | `in_scope` / `out_of_scope` / `unsure` / `parse_error` / `process_error`           |
| `flag_confidence`     | モデルが返した信頼度 (0〜1)                                                        |
| `flag_reason`         | 判定理由 (短文)                                                                    |
| `flag_model_path`     | 使用モデルパス                                                                     |
| `flag_prompt_version` | プロンプトのバージョン                                                             |

### バッチ実行例

```bash
# 0番目のバッチ (0始まり) を実行
python script/llama_flagger.py \
  results/edna_multisource_2020plus.csv \
  --config config/llama_flagger.jsonc \
  --batch-size 500 \
  --batch-index 0 \
  --out-csv results/edna_multisource_2020plus.flagged.csv
```

### 補足

- 行ごとのモデル再ロードを避けるには `--reuse-process` (またはJSONCで `reuse_process: true`) を指定します。
- `--reuse-process` 使用時は `llama_args` に `--single-turn` を渡さないでください。
- `llama-cli` が対話待ちになる場合は `--reuse-process` を維持し、`--llama-args "--no-conversation"` を追加してください。
- `--resume` は入力CSVで `flag_*` が埋まっている行と、出力CSVで `flag_*` が埋まっている行をスキップします。`flag_record_id` だけがある行は再処理されます。再実行する場合は `--no-resume` を使ってください。

---

## Strands DeciderでのAbstract判定 (strands_flagger.py)

`script/strands_flagger.py` は、Strands DeciderのHTTP APIを使ってCSVの `abstract` 列を判定します。
生成LLM版の `llama_flagger.py` と同じ `flag_record_id` / `flag_label` / `flag_confidence` などを出力するため、
既存の `llama_flagger_overlap.py` でgpt-oss等と直接比較できます。

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
| `exclude_threshold` | `0.90` | `P(out_of_scope)` の自動除外閾値 |
| `exclude_actual_use_max` | `0.15` | 自動除外で許容する `actual_use` の上限 |
| `exclude_method_relevance_max` | `0.30` | 自動除外で許容するMethod関連性の上限 |

初期値はPrecisionよりRecallを重視しています。まず手動判定済みデータでFalse Negativeを確認してから調整してください。

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

既存のllama.cpp版と互換の列:

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

### gpt-oss等との比較

Strands出力は `llama_flagger_overlap.py` にそのまま入力できます。

```bash
python script/llama_flagger_overlap.py \
  results/papers.flagged.gpt-oss-20b.csv \
  results/papers.strands.csv \
  --out results/gptoss_strands_overlap.csv \
  --plots-dir results \
  --plots-prefix gptoss_strands
```

特に `in_scope` のFalse Negative、`unsure` 率、1 Abstractあたりの処理時間を比較すると、
生成LLMとStrands Deciderの役割分担を判断しやすくなります。

---

## Strands Deciderベンチマーク

実運用データに対する性能と、境界例への強さを分けて確認するため、
`script/make_strands_benchmark.py` でblind benchmarkを作成できます。

デフォルトは200件です。

- 120件: 元CSVからランダム抽出した `natural` subset
- 80件: 微生物群集、環境試料だがeDNA/eRNA明記なし、組織/ゲノム、wastewater、Method系などを厚めにした `challenge` subset
- 約40%: `calibration` partition
- 約60%: `test` partition

人手判定時のバイアスを避けるため、2ファイルに分けて出力します。

- `*.review.csv`: 論文情報 + 空のgold label。人手判定に使用
- `*.manifest.csv`: natural/challenge、stratum、calibration/test。人手判定中は見ない

ラベル基準は `benchmark/LABELING_GUIDE.md` を参照してください。

### 1) ベンチマークセット作成

```bash
pixi run benchmark-make \
  test/results/edna_multisource20260228.csv \
  --out-prefix benchmark/edna_strands_200 \
  --n-total 200 \
  --n-natural 120 \
  --seed 3407
```

生成物:

```text
benchmark/edna_strands_200.review.csv
benchmark/edna_strands_200.manifest.csv
```

`review.csv` の以下を人手で入力します。

```text
gold_label
gold_actual_use
gold_microbial_only
gold_method_relevance
gold_note
```

最重要の `gold_label` は次の3値です。

- `in_scope`: 環境試料由来DNA/RNAを研究で実際に使用
- `out_of_scope`: 実際には環境試料由来DNA/RNAを使用していない
- `unsure`: Abstractだけでは判断不能

### 2) 同じ200件をStrandsで判定

```bash
pixi run strands-screen \
  benchmark/edna_strands_200.review.csv \
  --config config/strands_flagger.jsonc \
  --out-csv benchmark/edna_strands_200.strands.csv
```

人手ラベルを先に付け、Strandsの確率を見ない状態でgoldを確定させることを推奨します。

### 3) calibration partitionで除外閾値を調整

現在の初期閾値はRecall重視のため、陰性を `unsure` に残しすぎる可能性があります。
`benchmark-tune` は `calibration` partitionだけを使い、inclusion側の閾値を固定したまま、
以下の除外条件を探索します。

- `exclude_threshold`
- `exclude_actual_use_max`
- `exclude_method_relevance_max`

デフォルトでは `hard_false_negative=0` を必須条件とし、その範囲で正しく自動除外できる
`out_of_scope` 件数を最大化します。

```bash
pixi run benchmark-tune \
  benchmark/strands_benchmark_80_gold.csv \
  benchmark/strands_benchmark_80.strands.csv \
  --manifest benchmark/strands_benchmark_80_manifest.csv \
  --max-hard-fn 0 \
  --out-json benchmark/strands_benchmark_80.thresholds.json
```

表示された推奨値を `config/strands_flagger.jsonc` に反映してから再判定します。
最終的な性能確認には `test` partitionを使用します。

### 3) test partitionで最終評価

```bash
pixi run benchmark-eval \
  benchmark/edna_strands_200.review.csv \
  benchmark/edna_strands_200.strands.csv \
  --manifest benchmark/edna_strands_200.manifest.csv \
  --partition test \
  --out-errors benchmark/edna_strands_200.errors.csv \
  --out-json benchmark/edna_strands_200.metrics.json
```

主な指標:

- `hard_false_negative_rate`: gold=`in_scope` を自動で `out_of_scope` に落とした割合
- `operational_recall_if_unsure_is_reviewed`: `unsure` を人間が確認する運用を前提にしたRecall
- `manual_review_rate`: 人手確認へ回る割合
- `auto_coverage`: 自動でin/out判定できた割合
- `auto_accuracy`: 自動判定したものだけのaccuracy
- `in_scope_precision`: 自動採用した論文のprecision
- `brier_p_in_scope`: `P(in_scope)` の確率品質

`natural` と `challenge`、challenge stratumごとの結果も別々に表示されます。

閾値調整はまず `--partition calibration` で行い、最終的な性能確認には `test` partitionだけを使ってください。

---

## 複数モデルの一致度チェック (llama_flagger_overlap.py)

複数モデルのフラグ結果CSVから、ラベルの一致度合いを集計・可視化します。

### 依存関係

```bash
pip install matplotlib
# Venn図も使う場合
pip install matplotlib-venn
# UpSetプロットも使う場合
pip install upsetplot
```

### 基本例

```bash
python script/llama_flagger_overlap.py \
  results/modelA.flagged.csv \
  results/modelB.flagged.csv \
  results/modelC.flagged.csv \
  --out results/flag_overlap.csv \
  --plots-dir results \
  --plots-prefix flag_overlap
```

### Venn図の対象ラベル/モデルを指定

```bash
python script/llama_flagger_overlap.py \
  results/modelA.flagged.csv \
  results/modelB.flagged.csv \
  results/modelC.flagged.csv \
  --plots-dir results \
  --venn-label out_of_scope \
  --venn-models modelA.gguf modelB.gguf modelC.gguf
```

出力される図:

- `flag_overlap.agreement.png` (合意ステータス)
- `flag_overlap.pairwise.png` (ペア一致率)
- `flag_overlap.labels.png` (モデル別ラベル分布)
- `flag_overlap.venn_<label>.png` (ラベル別Venn)
- `flag_overlap.upset_in_scope.png` (in_scope UpSet)
- `flag_overlap.upset_out_of_scope.png` (out_of_scope UpSet)
