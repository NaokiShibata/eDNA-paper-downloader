# eDNA-paper-downloader

デフォルトの判定モデルは **Clef 27B Q8_0** です。`screen` と `fetch --strands` が使用します。
モデル取得・起動・実行例は [Clefの実行手順](docs/clef-default.md) を参照してください。
`--strands` というオプション名とCSV列名は互換性のため維持しています。

[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Python: 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/)
[![Platform](https://img.shields.io/badge/platform-linux--64-lightgrey.svg)](#動作環境)

eDNA関連の文献・プレプリント情報を収集し、CSVとして保存するPython CLIツールです。

主なスクリプト:

- `script/fetch.py`
  - デフォルトで `PubMed + Crossref + OpenAlex` から統合取得
  - DOI優先、タイトル+年フォールバックで重複統合
  - `Crossref/OpenAlex` は `type` による preprint 除外を実施
  - 要旨を常に取得し、要旨がないレコードは別CSVへ保存
  - `--strands` で統合後のAbstractをStrands Deciderで判定し、`out_of_scope` のみ最終出力から除外
  - 除外した論文は `*.rejected.csv` に保存して監査可能
  - `--sources` で bioRxiv/medRxiv も選択可能
- `script/serve.py`
  - GPU 1 → GPU 0 → CPUの順で計算デバイスを自動選択
  - CPU fallback時は低速になることを明示してから起動
- `script/screen.py`
  - Strands Deciderを使って `abstract` 全文からeDNA/eRNA関連性を高速判定
  - `in_scope` / `out_of_scope` / `unsure` の3段階でスクリーニング
  - 曖昧な論文は `unsure` に残すRecall重視の運用

---

## 動作環境

- Python 3.12
- 文献取得・CSV処理: Linux
- Strands Decider: Ubuntu/Linux。NVIDIA CUDA GPUがあればGPUを使用し、利用できない場合はCPUへ明示的にフォールバック
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

### 4) 計算デバイスを確認

```bash
pixi run serve-check
```

CUDA GPUが利用可能な環境ではGPUを選択し、利用できない環境ではCPU fallbackを明示します。

TritonはCUDA利用時にDriver API用の小さなC拡張をJITコンパイルするため、`cuda.h` も必要です。
conda-forgeではこのヘッダを `cuda-cudart-dev` から `targets/x86_64-linux/include/` に配置します。

### Pixiタスク

`pixi.toml` には以下のタスクを定義しています。

| タスク | 内容 |
| --- | --- |
| `pixi run serve-check` | Strands Deciderが選択するGPU/CPUを表示 |
| `pixi run serve` | GPU 0でClef 27B Q8_0を起動（port 8014） |
| `pixi run clef-health` | Clef APIの稼働確認 |
| `pixi run serve-strands` | 旧Strands Deciderを起動（port 8012） |
| `pixi run strands-health` | 起動中のStrands Decider APIを確認 |
| `pixi run fetch ...` | `script/fetch.py` を実行 |
| `pixi run benchmark make\|eval\|tune ...` | benchmarkの作成、評価、除外閾値の調整 |
| `pixi run screen ...` | `script/screen.py` を実行 |
| `pixi run e2e ...` | 最新14日の文献取得→Strands判定→簡易検証を通し実行 |
| `pixi run lint` | Ruff |
| `pixi run typecheck` | mypy |
| `pixi run test` | Strands判定閾値・除外ガードの単体テスト |

---

## クイックスタート

```bash
# 文献統合取得 (PubMed + Crossref + OpenAlex)
python3 script/fetch.py \
  --email you@example.com \
  --since 2020/01/01 \
  --out-dir results \
  --out-prefix edna_multisource_2020plus
```

---

## 文献統合取得: fetch.py

### ヘルプ

```bash
python3 script/fetch.py --help
```

### 基本例 (デフォルトで全ソース有効)

```bash
python3 script/fetch.py \
  --email you@example.com \
  --query '("environmental DNA"[Title/Abstract] OR eDNA[Title/Abstract])' \
  --since 2020/01/01 \
  --out-dir results \
  --out-prefix edna_multisource_2020plus
```

### 除外語

```bash
python3 script/fetch.py \
  --email you@example.com \
  --query '("environmental DNA"[Title/Abstract] OR eDNA[Title/Abstract])' \
  --exclude 'metagenome OR probiotic OR microbiome' \
  --since 2020/01/01 \
  --out-dir results \
  --out-prefix edna_multisource_no_microbiome
```

`--exclude` は複数指定でき、各値内の `OR` も解釈されます。
`--days N` は `--until` または当日を含むN日間を指定し、`--since` との併用はエラーになります。
`--email` は必須ですが、`NCBI_EMAIL` でも指定できます。
APIキーは `NCBI_API_KEY` と `OPENALEX_API_KEY` からも読み込みます。

### ソース切り替え

```bash
# デフォルト: 3ソースすべて有効
python3 script/fetch.py --email you@example.com

# 例: PubMedのみ
python3 script/fetch.py \
  --email you@example.com \
  --sources pubmed

# 例: Crossref/OpenAlex件数上限を指定
python3 script/fetch.py \
  --email you@example.com \
  --max-items 2000
```

### bioRxiv/medRxivを統合取得

```bash
python3 script/fetch.py \
  --email you@example.com \
  --sources pubmed,crossref,openalex,biorxiv,medrxiv \
  --since 2024/01/01 \
  --until 2024/12/31
```

`--sources` はカンマ区切りで指定します。
使用できる名前は `pubmed`, `crossref`, `openalex`, `biorxiv`, `medrxiv` です。
bioRxiv/medRxivには `--since` と `--until` の期間を使い、`--query` から抽出した検索語をOR条件で照合します。
開始日は `--since` または `--days` で指定してください。

### 要旨の取得

要旨は常に取得し、統合後にEurope PMCでDOIから不足する要旨を補完します。
補完後も要旨がない論文は `{out_prefix}.no_abstract.csv` に保存し、主出力とStrands判定から除外します。
件数はログに表示します。

### 取得時にStrands Deciderで関連論文を絞り込む

Strands Deciderサーバを起動した状態で `--strands` を指定すると、`--sources` で選択した取得元から取得・統合・重複除去した後の論文をAbstractで判定します。

```bash
pixi run serve
```

別ターミナル:

```bash
pixi run python script/fetch.py \
  --email you@example.com \
  --days 14 \
  --until 2026/10/04 \
  --strands \
  --out-dir results \
  --out-prefix edna_latest
```

`--strands` はデフォルトで無効です。指定時の処理順は以下です。

```text
PubMed / Crossref / OpenAlex / bioRxiv・medRxiv (任意)
        ↓
統合・重複除去
        ↓
Europe PMCでAbstract補完
        ↓
Abstractなしをno_abstractへ保存
        ↓
Strands Decider
        ├─ in_scope      → retained
        ├─ unsure        → retained
        ├─ process_error → retained
        └─ out_of_scope  → rejected
        ↓
CSV出力
```

最終出力:

```text
results/edna_latest.csv
results/edna_latest.rejected.csv
results/edna_latest.no_abstract.csv
```

通常のCSVには `in_scope` / `unsure` / `process_error` を残し、`out_of_scope` のみ `rejected` 側へ分離します。個別論文の判定エラーは取りこぼし防止のため自動除外しません。

`--strands` 指定時にStrands Deciderのhealth checkが失敗した場合は、未判定データをフィルタ済みとして出力せず処理を停止します。

fetchとscreenは設定の `cache_csv` を共用し、判定スコアをCSVに保存して再利用します。
Clefの既定設定は `cache_csv: null` でキャッシュを無効にします。旧Strands設定では `.cache/strands_scores.strands-v4.csv` に保存します。
キャッシュは論文IDとプロンプトのバージョンで照合し、判定ラベルは毎回現在の閾値で計算します。
判定エラーと要旨なしは保存せず、終了時にヒット数とミス数を表示します。
Clef設定の `batch_questions` は `true` で、旧Strands設定では `false` です。
`true` にすると4質問を1リクエストにまとめますが、GPUメモリの使用量が増えます。


### 色々総まとめ

```bash
python3 script/fetch.py \
  --email you@example.com \
  --query '("environmental DNA"[Title/Abstract] OR eDNA[Title/Abstract])' \
  --exclude 'metagenome OR probiotic OR microbiome' \
  --since 2008/01/01 \
  --until 2025/12/31 \
  --max-items 2000 \
  --sources pubmed,crossref,openalex,biorxiv \
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
- OpenAlex取得は `Works().filter(title_and_abstract={"search": ...}).filter(...).paginate(...)` で実行しています。
  - `per_page=200`
  - `n_max` は `--max-items` に対応
- `pyalex.config.email` は `--email` の値を設定しています（OpenAlexの polite pool 利用を意図）。
- OpenAlex APIキーは `--openalex-api-key` で設定できます（ログにはマスク表示）。
- Crossrefはpreprintを除外し、OpenAlexは文献型（article、review、letter、book-chapter、conference-paper、report、dissertation、data-paper、editorial、book）のみ取得します。
- 全ソースの取得結果から、Zenodo・FigshareのDOIを持つ文献は灰色文献として統合前に除外します。
- 要旨補完後もabstractが空のレコードを `no_abstract` 側へ保存します。
- Crossrefはcursor pagingで取得します。Crossrefはboolean検索に対応しないため、クエリ語 (例: `environmental DNA`, `eDNA`) をタイトルまたは要旨に含む論文だけを残し、一致が0件のページに達した時点で取得を打ち切ります。
- Crossref/OpenAlexの取得件数が上限に達するとwarningを表示します。必要に応じて `--max-items` を増やしてください。
- ログは `rich` を使って標準出力に表示されます（RUN HEADER含む）。

### OpenAlex APIメモ (pyalex)

- `pyalex` ではページングは `paginate()` が推奨です（cursor paging がデフォルト）。
- OpenAlexのレスポンス安定化には polite pool が有効です（`pyalex.config.email` 設定）。
- `pyalex` READMEでは、OpenAlex APIは **2026年2月13日** から APIキー必須と案内されています。運用時は `--openalex-api-key` を指定してください。
- 必要なら `pyalex.config.max_retries` / `retry_backoff_factor` / `retry_http_codes` で再試行挙動を調整できます。

### ログ出力 (RUN HEADER)

```bash
python3 script/fetch.py \
  --email you@example.com \
  --since 2020/01/01 \
  --out-dir results \
  --out-prefix edna_multisource \
  --log-file logs/edna_multisource.log \
  --log-level INFO
```

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

## Strands DeciderでのAbstract判定 (screen.py)

`script/screen.py` は、Strands DeciderのHTTP APIを使ってCSVの `abstract` 列を判定します。
既定はClef 27B Q8_0です。旧Strandsも同じHTTP APIで利用できます。

判定は単純なキーワード一致ではなく、1つのAbstractに対して以下を評価します。Clefの既定設定は5問を1 HTTP requestにまとめて送信します。

- `study_type`: 原著・レビュー・その他の分類
- `scope`: `in_scope` / `out_of_scope` / `unsure`
- `actual_use`: 環境試料由来DNA/RNAをMethods/Resultsで実際に扱っているか
- `microbial_only`: 一般的な微生物群集・microbiome・metagenomics解析に留まるか
- `method_relevance`: eDNA/eRNAの採取・保存・抽出・検出・定量・解析・モデリング等にMethodとして有用か

タイトルやAbstract中に `eDNA` / `eRNA` の語がなくても、研究内容そのものから判定します。
取りこぼしを減らすため、判断が曖昧な場合は `out_of_scope` に落とさず `unsure` に残します。

### 1) 旧Strands Deciderを起動（任意）

既定のClefを起動する場合は [Clefの実行手順](docs/clef-default.md) を参照してください。

起動前に、どの計算デバイスが選ばれるか確認できます。

```bash
pixi run serve-check
```

自動選択の優先順位は次のとおりです。

1. 物理GPU 1が利用可能ならGPU 1
2. GPUが1枚だけ、またはGPU 1が利用できない場合はGPU 0
3. CUDA GPUが利用できない場合はCPU

GPUを選んだ場合は、選択した物理GPUだけを `CUDA_VISIBLE_DEVICES` でStrands Deciderへ公開するため、Strandsプロセス内では論理 `cuda:0` として見えます。

CPUへ切り替える場合は黙ってフォールバックせず、起動前に次のようなwarningを表示します。

```text
[serve] No CUDA GPU detected
[serve] WARNING: Falling back to CPU explicitly (--device cpu). Inference will be slower than CUDA.
```

起動:

```bash
pixi run serve-strands
```

GPU環境では `--device cuda`、GPUがない環境では `--device cpu` を明示してStrands Deciderを起動します。古いStrands Decider CLIとの互換性を保つため、`--max-batch` など新しいサーバオプションには依存しません。

別ターミナルからhealth checkします。

```bash
pixi run strands-health
```

`"status":"ok"` が返れば実行可能です。GPU利用時は `"device":"cuda"`、CPU fallback時は `"device":"cpu"` になります。GPU利用時は選択した物理GPUだけを `CUDA_VISIBLE_DEVICES` で公開するため、health responseには物理GPU番号は表示されません。

`causal_conv1d` や `flash-linear-attention` が未導入というwarningが出る場合でも、
最適化カーネルを使わないPyTorch実装へフォールバックします。まず判定精度の検証を優先してください。

この節の旧Strandsを使う場合は、以下のClef設定ではなく `--config config/strands_flagger.example.jsonc` を指定してください。

### 2) 設定ファイルを準備

```bash
cp config/clef_flagger.example.jsonc config/clef_flagger.jsonc
```

設定は `config/clef_flagger.jsonc` があれば読み込み、なければ `config/clef_flagger.example.jsonc` を使います。
使用したパスをログに表示します。
設定には判定パラメータと `cache_csv` だけを指定し、未知のキーがあればエラーになります。
flaggerでは `--config` で別のファイルも指定できます。

デフォルトではCSVの `abstract` 列を使用します。
`--abstract-column` で列名を変更できます。
モデルにはタイトルとAbstractを渡します。`max_abstract_chars` のデフォルトは `6000` です。外すと非常に長いAbstractでサーバの最大長を超え、サーバが使用不能になることがあります。

主な閾値:

| 設定キー | デフォルト | 意味 |
| --- | ---: | --- |
| `include_threshold` | `0.45` | `P(in_scope)` の自動採用閾値 |
| `include_microbial_only_max` | `0.35` | 自動採用で許容する `microbial_only` の上限（未満） |
| `actual_use_threshold` | `0.60` | 自動採用時に必要な `actual_use` |
| `exclude_threshold` | `0.50` | `P(out_of_scope)` の自動除外閾値 |
| `exclude_actual_use_max` | `0.50` | 自動除外で許容する `actual_use` の上限 |
| `exclude_method_relevance_max` | `0.60` | 自動除外で許容するMethod関連性の上限 |
| `exclude_review_min` | `0.60` | レビューの自動除外に必要な `P(review)` の下限 |
| `exclude_review_out_min` | `0.40` | レビューの自動除外に必要な `P(out_of_scope)` の下限 |

現在のデフォルトは、実データ200件benchmarkのcalibration partitionで選んだ値です（`docs/strands-decider-benchmark.md`）。Abstractが300文字未満の論文は自動除外しません。曖昧な論文は引き続き `unsure` として人手確認に回します。

### 3) 少数件でテスト

```bash
pixi run screen \
  results/edna_multisource_2020plus.csv \
  --out-csv results/edna_multisource_2020plus.strands.csv \
  --limit 20
```

### 4) 全件実行

```bash
pixi run screen \
  results/edna_multisource_2020plus.csv \
  --out-csv results/edna_multisource_2020plus.strands.csv
```

出力CSVは毎回書き直し、判定した行から順に保存します。
デフォルトの出力先は入力CSVと同じディレクトリの `<入力stem>.strands.csv` です。
再実行時は共通キャッシュのスコアを使い、判定エラーは再試行します。

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

## 最新文献のE2Eテスト

`script/e2e.py` は、実行日を終点とする最新14日間（`--days` で変更可能）について、文献取得から取得処理内のStrandsフィルタまでを通しで確認します。

Clefを別ターミナルで起動します。

```bash
pixi run serve
```

別ターミナル:

```bash
pixi run e2e --email you@example.com
```

`NCBI_EMAIL` を設定している場合は `--email` を省略できます。

```bash
export NCBI_EMAIL="you@example.com"
pixi run e2e
```

E2Eでは以下を確認します。

- 実行日を含む直近14日のPubMed + Crossref + OpenAlex取得
- Abstractを持つレコードのStrands判定
- retained側に `out_of_scope` が混ざっていないこと
- rejected側が `out_of_scope` のみであること
- 必要なStrands診断列が存在すること
- `process_error=0`

期間を固定して再現実行する場合:

```bash
pixi run e2e \
  --email you@example.com \
  --until-date 2026-10-04 \
  --days 14
```

E2Eも共通のStrands設定を自動で読み込みます。
取得処理を同じプロセス内で呼び出し、検索クエリと取得上限にはfetchの既定値を使います。
`--out-dir` と `--prefix` で出力先とファイル名を変更できます。

生成物はデフォルトで `test/results/` に出力されます。

```text
e2e_latest14_YYYYMMDD.csv
e2e_latest14_YYYYMMDD.rejected.csv
e2e_latest14_YYYYMMDD_summary.json
```

正常終了時は最後に `E2E PASS` と取得件数、retained/rejected件数、ラベル分布、処理時間を表示します。

---

## Strands Deciderベンチマーク

ベンチマークの作成、gold labelの基準、calibration/test分割、閾値調整、検証結果は
[docs/strands-decider-benchmark.md](docs/strands-decider-benchmark.md) にまとめています。
