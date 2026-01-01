# eDNA-paper-downloader

eDNA関連の文献・プレプリント情報を収集し、CSV/JSONとして保存するPython CLIツールです。

- **PubMed**: `script/edna_literature_fetch.py`
  - NCBI Entrez (E-utilities) を利用
  - クエリ検索 + 期間指定 + ページング (全PMID回収)
  - DOI重複が出た場合は **より新しい更新日** (revised/entrez/pub) を採用
  - 出力に **公開日/登録日/受理日/改訂日**など (取得できる範囲で) を含める
  - RUN HEADER付きログ出力 (実行者・コマンド・パラメータなど)

- **bioRxiv/medRxiv**: `script/biorxiv_search.py`
  - 公式APIで期間指定取得
  - ローカルでキーワード/除外語フィルタ
  - DOIごとに **最新版のみ (version最大) ** を採用 (デフォルト)
  - 逐次上書き出力 (途中で落ちても最新CSV/JSONが残る)
  - 既存出力から **差分 (delta) ** を検出し `*.delta.csv/json` を保存 (デフォルト)
  - DEBUGログでヒット内容 (doi/title/date/versionなど) を出力可能

---

## Requirements

- Python 3.10+ (推奨: 3.11/3.12)
- OS: Linux/macOS/Windows (WSL可)

---

## Install

```bash
python -m venv .venv
source .venv/bin/activate

pip install -U pip
pip install "typer[all]" pandas requests tqdm biopython
```

---

## Quick Start

```bash
# PubMed (minimal)
python script/edna_literature_fetch.py \
  --email you@example.com \
  --out-dir results

# bioRxiv (minimal)
python script/biorxiv_search.py \
  --from-date 2024-01-01 \
  --to-date 2024-12-31 \
  --query "eDNA" \
  --out-dir results
```

---

## PubMed: edna_literature_fetch.py

### Help

```bash
python script/edna_literature_fetch.py --help
python script/edna_literature_fetch.py fetch --help
```

### Basic example

```bash
python script/edna_literature_fetch.py \
  --email you@example.com \
  --query '("environmental DNA"[Title/Abstract] OR eDNA[Title/Abstract])' \
  --since 2020/01/01 \
  --out-dir results \
  --out-prefix pubmed_edna_2020plus
```

### Exclude terms

```bash
python script/edna_literature_fetch.py \
  --email you@example.com \
  --query '("environmental DNA"[Title/Abstract] OR eDNA[Title/Abstract])' \
  --exclude microbiome \
  --since 2020/01/01 \
  --out-dir results \
  --out-prefix pubmed_edna_no_microbiome
```

### Include abstract/keywords

```bash
python script/edna_literature_fetch.py \
  --email you@example.com \
  --since 2020/01/01 \
  --abstract \
  --keywords \
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

### Dates in output (columns)

PubMed出力には、取得できる範囲で以下の日付列が入ります。

- `pub_date` : 公開/出版日 (YYYY-MM-DD best effort)
- `entrez_date` : PubMed登録日 (entrez)
- `received_date` : 受領日 (ある場合)
- `accepted_date` : 受理日 (ある場合)
- `revised_date` : レコード改訂日 (MedlineCitation DateRevised)

※論文/ジャーナルによって入っていない列もあります。

### Logging (RUN HEADER)

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

## bioRxiv/medRxiv: biorxiv_search.py

### Help

```bash
python script/biorxiv_search.py --help
python script/biorxiv_search.py search --help
```

### Basic example (bioRxiv 2024)

```bash
python script/biorxiv_search.py \
  --server biorxiv \
  --from-date 2024-01-01 \
  --to-date 2024-12-31 \
  --query "environmental DNA eDNA" \
  --out-dir results \
  --out-prefix biorxiv_edna_2024
```

### Latest version only (default)

同一DOIで複数バージョン (v1, v2, …) がある場合、**versionが最大のものだけ**を残します (デフォルト)。

`--all-versions` を付けると全バージョンを残します。

```bash
# latest only (default)
python script/biorxiv_search.py \
  --from-date 2024-01-01 --to-date 2024-12-31 \
  --query "eDNA" \
  --latest-only

# keep all versions
python script/biorxiv_search.py \
  --from-date 2024-01-01 --to-date 2024-12-31 \
  --query "eDNA" \
  --all-versions
```

### Incremental overwrite (途中停止でも最新出力が残る)

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

### Batch processing (weekly/monthly)

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

### Delta update from existing outputs

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

### Debug log hits (doi/title/date/version)

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

### Dates in output (columns)

bioRxiv出力には以下が入ります：

- `date` / `posted_date` : 投稿/掲載日 (APIのdate)
- `retrieved_at` : 取得時刻 (スクリプト実行時)

---

## Notes / Troubleshooting

### PubMed (NCBI Entrez)
- `--email` は必須です
- 大量取得時は `--sleep` を増やすと安定します (例: 0.5〜1.0)

### bioRxiv
- APIは期間指定取得が基本で、検索 (boolean) はローカルフィルタです
- `--incremental` は安全性 (途中停止) と引き換えにI/Oが増えるため遅くなります
