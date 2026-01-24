# eDNA-paper-downloader

[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Python: 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![Platform](https://img.shields.io/badge/platform-linux%20%7C%20macOS%20%7C%20windows-lightgrey.svg)](#動作環境)

eDNA関連の文献・プレプリント情報を収集し、CSV/JSONとして保存するPython CLIツールです。

- **PubMed**: `script/edna_literature_fetch.py`

  - NCBI Entrez (E-utilities) を利用
  - クエリ検索 + 期間指定 + ページング (全PMID回収)
  - DOI重複が出た場合は **より新しい年/PMID** を採用
  - RUN HEADER付きログ出力 (実行者・コマンド・パラメータなど)

- **bioRxiv/medRxiv**: `script/biorxiv_search.py`

  - 公式APIで期間指定取得
  - ローカルでキーワード/除外語フィルタ
  - DOIごとに **最新版のみ (version最大)** を採用 (デフォルト)
  - 逐次上書き出力 (途中で落ちても最新CSV/JSONが残る)
  - 既存出力から **差分 (delta)** を検出し `*.delta.csv/json` を保存 (デフォルト)
  - DEBUGログでヒット内容 (doi/title/date/versionなど) を出力可能

- **Crossref + Semantic Scholar**: `script/crossref_semantic_fetch.py`
  - PubMed外の論文を拾う用途に便利
  - DOI/タイトル+年で重複排除し、両ソースをマージ
  - PubMed CSVを渡すと既存論文を除外可能

---

## 動作環境

- Python 3.10+ (推奨: 3.11/3.12)
- OS: Linux/macOS/Windows (WSL可)

---

## インストール

```bash
python -m venv .venv
source .venv/bin/activate

pip install -U pip
pip install "typer[all]" pandas requests tqdm biopython
```

---

## uvでの環境構築

uvの導入から環境作成までの手順です。

### 1) uvをインストール

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

インストール後、シェルを再起動するか `~/.profile` 等を読み直してください。

### 2) 仮想環境の作成と依存導入

```bash
uv venv .venv
source .venv/bin/activate
uv pip install "typer[all]" pandas requests tqdm biopython
```

PubMed (Entrez) を含むダウンロード系スクリプトに必要な主なパッケージは以下です。

- `biopython` (Entrez)
- `requests`
- `pandas`
- `tqdm`
- `typer[all]`

YAML設定ファイルを使う場合は `pyyaml` も追加してください。

```bash
uv pip install pyyaml
```

---

## クイックスタート

```bash
# PubMed (最小例)
python script/edna_literature_fetch.py \
  --email you@example.com \
  --out-dir results

# bioRxiv (最小例)
python script/biorxiv_search.py \
  --from-date 2024-01-01 \
  --to-date 2024-12-31 \
  --query "eDNA" \
  --out-dir results
```

---

## PubMed: edna_literature_fetch.py

### ヘルプ

```bash
python script/edna_literature_fetch.py --help
```

### 基本例

```bash
python script/edna_literature_fetch.py \
  --email you@example.com \
  --query '("environmental DNA"[Title/Abstract] OR eDNA[Title/Abstract])' \
  --since 2020/01/01 \
  --out-dir results \
  --out-prefix pubmed_edna_2020plus
```

### 除外語

```bash
python script/edna_literature_fetch.py \
  --email you@example.com \
  --query '("environmental DNA"[Title/Abstract] OR eDNA[Title/Abstract])' \
  --exclude microbiome \
  --since 2020/01/01 \
  --out-dir results \
  --out-prefix pubmed_edna_no_microbiome
```

### 要旨を含める

```bash
python script/edna_literature_fetch.py \
  --email you@example.com \
  --since 2020/01/01 \
  --abstract \
  --out-dir results \
  --out-prefix pubmed_edna_with_abstract
```

### CrossrefでDOI補完 (任意)

```bash
python script/edna_literature_fetch.py \
  --email you@example.com \
  --since 2020/01/01 \
  --crossref \
  --user-agent "edna-literature-fetch/1.0 (mailto:you@example.com)" \
  --out-dir results \
  --out-prefix pubmed_edna_crossref
```

### 色々総まとめ

```bash
python3 script/edna_literature_fetch.py \
--email you@example.com \
--since 2008/01/01 \
--out-dir results \
--out-prefix pubme_edna_$(date +%Y%m%d) \
--log-file logs/pubmed_edna_$(date +%Y%m%d).log \
--crossref \
--abstract \
--exclude 'metagenome OR probiotic OR "skin microbiome" OR "oral microbiome"  OR "rumen microbiome" OR dysbiosis OR probiotic OR "gut microbiome" OR metagenome OR "shotgun metagenomics" OR microbiome OR resistome OR "antimicrobial resistance" OR AMR OR virome OR "viral metagenome" OR "ITS community profiling" OR "wastewater epidemiology" OR mycobiome' \
--query '("environmental DNA"[Title/Abstract] OR eDNA[Title] OR "environmental RNA[Title/Abstract]" OR eRNA[Title])'
```

### 出力列

PubMed出力には以下の列が入ります。

- `pmid`, `title`, `journal`, `year`, `authors`, `doi`, `abstract`, `pubmed_url`

### ログ出力 (RUN HEADER)

```bash
python script/edna_literature_fetch.py \
  --email you@example.com \
  --since 2020/01/01 \
  --out-dir results \
  --out-prefix pubmed_edna \
  --log-file logs/pubmed_edna.log \
  --log-level INFO
```

---

## Crossref + Semantic Scholar: crossref_semantic_fetch.py

### 基本例

```bash
python script/crossref_semantic_fetch.py \
  --query "environmental DNA eDNA" \
  --max-items 1000 \
  --out-dir results \
  --out-prefix crossref_semantic_edna
```

### PubMed結果を除外

```bash
python script/crossref_semantic_fetch.py \
  --query "environmental DNA eDNA" \
  --exclude-pubmed-csv results/pubmed_edna_2020plus.csv \
  --out-dir results \
  --out-prefix crossref_semantic_edna_no_pubmed
```

---

## bioRxiv/medRxiv: biorxiv_search.py

### ヘルプ

```bash
python script/biorxiv_search.py --help
python script/biorxiv_search.py search --help
```

### 基本例 (bioRxiv 2024)

```bash
python script/biorxiv_search.py \
  --server biorxiv \
  --from-date 2024-01-01 \
  --to-date 2024-12-31 \
  --query "environmental DNA eDNA" \
  --out-dir results \
  --out-prefix biorxiv_edna_2024
```

### 最新版のみ

同一DOIで複数バージョン (v1, v2, …) がある場合、**versionが最大のものだけ**を残します (デフォルト)。

`--all-versions` を付けると全バージョンを残します。

```bash
# 最新版のみ (デフォルト)
python script/biorxiv_search.py \
  --from-date 2024-01-01 --to-date 2024-12-31 \
  --query "eDNA" \
  --latest-only

# 全バージョンを保持
python script/biorxiv_search.py \
  --from-date 2024-01-01 --to-date 2024-12-31 \
  --query "eDNA" \
  --all-versions
```

### 逐次上書き

途中で停止しても最新出力が残るようになっています。

```bash
python script/biorxiv_search.py \
  --server biorxiv \
  --from-date 2024-01-01 \
  --to-date 2024-12-31 \
  --query "environmental DNA eDNA" \
  --exclude microbiome \
  --incremental \
  --out-dir results \
  --out-prefix biorxiv_edna_2024
```

### バッチ処理

週や月単位でのバッチ処理例です。

```bash
python script/biorxiv_search.py \
  --server biorxiv \
  --from-date 2024-01-01 \
  --to-date 2024-12-31 \
  --batch-unit monthly \
  --query "environmental DNA eDNA" \
  --out-dir results \
  --out-prefix biorxiv_edna_2024
```

### 差分更新 (既存出力からの更新)

既存出力からの更新処理です。
既存の `out_prefix.csv/json` がある場合、**新規/更新DOIのみ**を`out_prefix.delta.csv/json` に出力します ()デフォルト)。

```bash
python script/biorxiv_search.py \
  --server biorxiv \
  --from-date 2024-01-01 \
  --to-date 2024-12-31 \
  --query "environmental DNA eDNA" \
  --update \
  --write-delta \
  --out-dir results \
  --out-prefix biorxiv_edna_2024
```

### デバッグログ

- doi/title/date/versionが記載されるDEBUGう実行モードです。

```bash
python script/biorxiv_search.py \
  --server biorxiv \
  --from-date 2024-01-01 \
  --to-date 2024-12-31 \
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

### 1) llama.cppサーバを起動

```bash
./path/to/llama-server -m /path/to/your-model.gguf --port 8080 --embedding
```

※ 別の埋め込みモデルを使う場合は、別ポートで起動し `--embed-url` を分けて指定します。

### 2) CSVから埋め込みインデックス作成

```bash
python script/llama_rag_csv.py index \
  results/pubmed_edna_2020plus.csv \
  --out-index results/pubmed_edna_2020plus.index.jsonl \
  --text-cols title,journal,year,authors,doi
```

### 3) 質問する

```bash
python script/llama_rag_csv.py ask \
  results/pubmed_edna_2020plus.index.jsonl \
  "Which papers mention CRISPR-Cas and what are the DOIs?" \
  --top-k 5 \
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
| `llama_bin` | no | `llama-cli` のパス (省略時はPATH検索) |
| `llama_args` | no | `llama-cli` の追加引数 (例: `--no-conversation`) |
| `model_profile` | no | `gpt-oss` / `gemma`。省略時は `model_path` から推定 |
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

- `model_profile` を指定するとモデル別の推奨値が自動適用されます。`sampling_temperature` / `ctx_size` を `null` にするとデフォルトが有効になります。
- `model_profile` を省略した場合は `model_path` のファイル名から推定します (`gpt-oss` / `gpt_oss` / `gemma` を含むかで判定)。
- `ctx_size` を `llama_args` に指定した場合は、その値が優先されます (configの `ctx_size` は無視されます)。
- `resume=true` は入力CSVに `flag_*` が埋まっている行、または出力CSVに `flag_*` が埋まっている行をスキップします (`flag_record_id` だけの行は再処理されます)。

設定例 (gpt-oss):

```jsonc
{
  "scope": "environmental DNA/RNA papers for ecology and monitoring",
  "model_path": "/path/to/gpt-oss-20b-Q4_K_M.gguf",
  "model_profile": "gpt-oss",
  "llama_bin": "/path/to/llama-cli",
  "llama_args": "--threads 16 --no-conversation",
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
| `model_profile` | string/null | `gpt-oss` | `gpt-oss` / `gemma` / `null` |
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
  results/pubmed_edna_2020plus.csv \
  --config config/llama_flagger.jsonc \
  --out-csv results/pubmed_edna_2020plus.flagged.csv
```

### 実行例 (コマンド指定)

```bash
python3 /path/to/eDNA-paper-downloader/script/llama_flagger.py \
  results/pubmed_edna_20260118.csv \
  --out-csv results/pubmed_edna_20260118plus.flagged.csv \
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
  results/pubmed_edna_2020plus.csv \
  --config config/llama_flagger.jsonc \
  --batch-size 500 \
  --batch-index 0 \
  --out-csv results/pubmed_edna_2020plus.flagged.csv
```

### 補足

- 行ごとのモデル再ロードを避けるには `--reuse-process` (またはJSONCで `reuse_process: true`) を指定します。
- `--reuse-process` 使用時は `llama_args` に `--single-turn` を渡さないでください。
- `llama-cli` が対話待ちになる場合は `--reuse-process` を維持し、`--llama-args "--no-conversation"` を追加してください。
- `--resume` は入力CSVで `flag_*` が埋まっている行と、出力CSVで `flag_*` が埋まっている行をスキップします。`flag_record_id` だけがある行は再処理されます。再実行する場合は `--no-resume` を使ってください。
