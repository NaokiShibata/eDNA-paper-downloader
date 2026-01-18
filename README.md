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

### 最新版のみ (デフォルト)

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

### 逐次上書き (途中停止でも最新出力が残る)

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

### バッチ処理 (週/月単位)

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

既存の `out_prefix.csv/json` がある場合、**新規/更新DOIのみ**を
`out_prefix.delta.csv/json` に出力します (デフォルト)。

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

### デバッグログ (doi/title/date/version)

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

bioRxiv出力には以下が入ります：

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

## llama.cppでの簡易RAG (CSV)

CSVにまとめた文献メタデータを使って、llama.cppでRAG風のQAを行う簡易スクリプトです。
以下はローカルのllama.cppサーバ (OpenAI互換API) を前提にしています。

### 1) llama.cppサーバを起動

```bash
./server -m /path/to/your-model.gguf --port 8080 --embedding
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

ダウンロード済みファイル (PDF/TXT) をllama.cpp CLIで精査し、目的外の論文にフラグを付けます。
llama.cppサーバは使いません。

### 1) 設定ファイルを準備 (JSONC)

```bash
cp config/llama_flagger.example.jsonc config/llama_flagger.jsonc
```

`scope` と `model_path` を設定してください。

#### 設定項目 (llama_flagger.jsonc)

- `scope` (必須): 対象論文の範囲を1〜3文で記述
- `model_path` (必須): 使用するGGUFモデルのパス
- `llama_bin`: `llama-cli` のパス (省略時はPATH検索)
- `llama_args`: `llama-cli` の追加引数 (例: `--no-conversation`)
- `reuse_process`: `true` でモデルを1回ロードして使い回し
- `batch_size`: バッチ件数 (例: `500`)
- `batch_index`: バッチ番号 (0始まり)
- `files_dir`: PDF/TXTの検索ディレクトリ
- `file_exts`: 検索対象拡張子 (例: `[".pdf",".txt"]`)
- `file_path_col`: CSV内のファイルパス列名 (無ければ `null`)
- `min_token_len`: タイトル一致判定の最小トークン長
- `min_token_matches`: タイトル一致判定に必要な一致数
- `max_chars`: ファイルから読む最大文字数
- `limit`: 読み込み件数の上限 (`batch_size` とは同時指定不可)
- `max_tokens`: 生成トークン数
- `temperature`: 生成温度
- `timeout`: 1件あたりのタイムアウト秒
- `pdftotext`: `pdftotext` のパス (省略時はPATH検索)
- `include_hint`: in-scopeの補助ヒント (文字列 or 配列)
- `exclude_hint`: out-of-scopeの補助ヒント (文字列 or 配列)

### 2) 実行

```bash
python script/llama_flagger.py \
  results/pubmed_edna_2020plus.csv \
  --files-dir downloads \
  --config config/llama_flagger.jsonc \
  --out-csv results/pubmed_edna_2020plus.flagged.csv
```

### バッチ実行例

```bash
# 0番目のバッチ (0始まり) を実行
python script/llama_flagger.py \
  results/pubmed_edna_2020plus.csv \
  --files-dir downloads \
  --config config/llama_flagger.jsonc \
  --batch-size 500 \
  --batch-index 0 \
  --out-csv results/pubmed_edna_2020plus.flagged.csv
```

### 補足

- CSVに `file_path` 列がある場合は優先的に使います (相対パスは `--files-dir` 基準)。
- PDF抽出は `pdftotext` がある場合のみ有効。未インストールならPDF本文はスキップされます。
- 行ごとのモデル再ロードを避けるには `--reuse-process` (またはJSONCで `reuse_process: true`) を指定します。
- `--reuse-process` 使用時は `llama_args` に `--single-turn` を渡さないでください。
- `llama-cli` が対話待ちになる場合は `--reuse-process` を維持し、`--llama-args "--no-conversation"` を追加してください。
