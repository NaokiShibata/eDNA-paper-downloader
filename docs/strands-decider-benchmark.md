# Strands Decider benchmark

このドキュメントでは、`script/strands_flagger.py` のeDNA/eRNA論文スクリーニングを検証するための
benchmark作成、gold label付与、閾値調整、評価手順をまとめます。

現在の論文判定はStrands Deciderに統一しています。

## 現在の判定閾値

`PROMPT_VERSION = "strands-v3"` では、実データ200件benchmarkのcalibration partitionで選んだ以下をデフォルトとしています。

```json
{
  "include_threshold": 0.45,
  "include_microbial_only_max": 0.35,
  "actual_use_threshold": 0.60,
  "exclude_threshold": 0.50,
  "exclude_actual_use_max": 0.50,
  "exclude_method_relevance_max": 0.60,
  "max_abstract_chars": 6000
}
```

判定は次の方針です。

- モデルへの入力は `Title: ...` と `Abstract: ...` の両方（gold付与と同じ情報）
- `in_scope`: `P(in_scope) >= 0.45`、`P(actual_use) >= 0.60`、`P(microbial_only) < 0.35` をすべて満たす
- `out_of_scope`: `P(out_of_scope) >= 0.50`、`P(actual_use) <= 0.50`、
  `P(method_relevance) <= 0.60` をすべて満たす。ただしAbstractが300文字未満の場合は自動除外しない
- それ以外: `unsure` として人手確認

`include_microbial_only_max` は感度が高く、0.25に下げると人手確認率がほぼ倍になります。
`max_abstract_chars` を外すと、非常に長いAbstractでサーバの最大長（4096 token）を超え、
CUDA device-side assertでサーバが使用不能になることがあります。

## Benchmarkの考え方

False Negative、特に本来 `in_scope` の論文を自動で `out_of_scope` に落とすことを最も避けます。

主に見る指標:

- `hard_false_negative_rate`: gold=`in_scope` を自動で `out_of_scope` にした割合
- `operational_recall_if_unsure_is_reviewed`: `unsure` を人手確認する前提のRecall
- `manual_review_rate`: 人手確認が必要な割合
- `auto_coverage`: `in_scope` / `out_of_scope` を自動決定できた割合
- `auto_accuracy`: 自動決定したレコードだけのaccuracy
- `in_scope_precision`: 自動採用した論文のprecision
- `brier_p_in_scope`: `P(in_scope)` の確率品質

閾値調整には `calibration` partitionだけを使用し、最終確認には `test` partitionを使用します。

## Benchmarkセットの作成

実データからblind benchmarkを作る場合:

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

`review.csv` には論文情報と空のgold列を出し、`manifest.csv` にはnatural/challenge、
stratum、calibration/testの情報を分離します。人手判定中はmanifestを見ないことでラベル付けのバイアスを抑えます。

## Gold labelの基準

### gold_label

使用する値は次の3つです。

#### in_scope

研究が環境試料・環境マトリクスから直接得たDNA/RNAを実際に採取、検出、定量、配列決定、
解析、比較、検証、モデリングしている場合。

`eDNA` / `eRNA` という語がタイトルやAbstractに明示されている必要はありません。

対象となる環境試料の例:

- 水、海水、淡水
- 底質、土壌
- 空気、粉塵
- 雪、氷
- wastewater
- biofilm
- passive sampler
- environmental swab

#### out_of_scope

環境由来DNA/RNAを研究中で実際には解析していない場合。

例:

- 組織、血液、単離個体のみのDNA/RNA解析
- 個体ゲノム、一般的なtranscriptomics
- 培養株のみの解析
- host-associated microbiome
- eDNA/eRNAを背景、考察、比較、引用、将来展望で述べるだけの研究
- 一般的なmicrobiome / microbial-community / metagenomics / metatranscriptomicsで、
  eDNA/eRNAの検出・モニタリング・手法開発としての意味がない研究

#### unsure

Abstractだけでは環境由来DNA/RNAを実際に扱ったか判断できない場合。
不足しているMethodsを推測して補完せず、`unsure` とします。

## 補助gold label

必要に応じて次も付与します。

### gold_actual_use

`true` / `false` / `unsure`

環境試料から直接得たDNA/RNAをMethodsまたはResultsで実際に使用しているか。

### gold_microbial_only

`true` / `false` / `unsure`

一般的なmicrobiome、微生物群集、metagenomics、metatranscriptomics解析に留まり、
eDNA/eRNAの検出、モニタリング、採取、定量、検証、Method上の意味がないか。

16S、18S、ITS、COI、metabarcoding、metagenomicsという単語だけを理由に `true` にしません。

### gold_method_relevance

`true` / `false` / `unsure`

採取、保存、抽出、検出、増幅、シーケンス、定量、bioinformatics、modeling、monitoringなど、
eDNA/eRNA研究へ直接応用可能なMethod上の知見があるか。

## 人手ラベル付けの手順

1. Strandsの予測を見ずに `gold_label` を先に付ける
2. 必要に応じて3つの補助gold列を付ける
3. titleとabstractだけを使用する
4. 判断が難しい場合は `gold_note` に短い理由を残す
5. `benchmark_id` と `benchmark_record_id` は変更しない

## Strandsで判定

```bash
pixi run strands-screen \
  benchmark/edna_strands_200.review.csv \
  --config config/strands_flagger.jsonc \
  --out-csv benchmark/edna_strands_200.strands.csv
```

## Calibrationで除外閾値を調整

```bash
pixi run benchmark-tune \
  benchmark/strands_benchmark_80_gold.csv \
  benchmark/strands_benchmark_80.strands.csv \
  --manifest benchmark/strands_benchmark_80_manifest.csv \
  --max-hard-fn 0 \
  --out-json benchmark/strands_benchmark_80.thresholds.json
```

`benchmark-tune` はinclusion側の閾値を固定し、以下を探索します。

- `exclude_threshold`
- `exclude_actual_use_max`
- `exclude_method_relevance_max`

原則として `hard_false_negative=0` の組み合わせから、自動除外できるgold-negative数が多い設定を選びます。

## Test partitionで評価

```bash
pixi run benchmark-eval \
  benchmark/strands_benchmark_80_gold.csv \
  benchmark/strands_benchmark_80.strands.methodsafe.csv \
  --manifest benchmark/strands_benchmark_80_manifest.csv \
  --partition test \
  --out-errors benchmark/strands_benchmark_80.methodsafe.errors.csv \
  --out-json benchmark/strands_benchmark_80.methodsafe.metrics.json
```

## 合成80件benchmarkでの確認結果

Method系を安全側に残す設定
`exclude_threshold=0.50`、`exclude_actual_use_max=0.50`、
`exclude_method_relevance_max=0.60` をtest partitionで確認した結果:

| 指標 | 結果 |
| --- | ---: |
| binary gold | 46件 |
| gold in_scope | 24件 |
| gold out_of_scope | 22件 |
| hard false negative | 0件 |
| operational recall | 1.000 |
| auto coverage | 0.674 |
| manual review rate | 0.326 |
| auto accuracy | 0.968 |
| in-scope precision | 0.958 |

Method-positive 7件、microbial-method-positive 3件、explicit-eDNA-method 2件はいずれも正しく保持されました。

一方、genericなmicrobial/metatranscriptomics研究を `in_scope` とするFalse Positiveが1件ありました。
これは必要論文を捨てるFalse Negativeではなく余分な人手確認につながる誤差なので、
今回のRecall重視の運用では追加ルールを増やさず許容しています。

この80件は合成benchmarkであり、実運用精度の保証ではありません。
以後は通常運用で明らかな誤除外が見つかった場合に再検証する方針とします。

## 実データ200件benchmarkでの確認結果

`benchmark/edna_strands_200.review.labeled.csv`（natural 120件 + challenge 80件、人手gold）で評価しました。

### Gold labelの修正

当初のgoldでは、eDNAが主題のレビュー・展望・書籍・論説・会議報告24件が `in_scope` になっていましたが、
上記の基準（自ら環境DNA/RNAを解析していない研究は `out_of_scope`）に合わせて `out_of_scope` へ修正しました
（calibration 10件、test 14件）。該当行の `gold_note` に理由を記録しています。

### strands-v2からstrands-v3への変更

calibration partitionだけで質問文と閾値を比較し、以下を採用しました。

- scope: レビュー類、host-associated microbiome、消化管内容物DNA、汎用的な微生物群集解析を `out_of_scope` と明記
- actual_use: レビュー類は自らの解析がないため `false` と明記
- microbial_only: 微生物群集の記述が主目的かを直接問う形に変更し、`in_scope` の追加条件に使用
- method_relevance: 論文自身のデータで手法を検証しているかを問う形に変更
- 入力にタイトルを追加。Abstractが途中で切れた論文（B0162）が、v3の質問文でもAbstractだけでは自動除外されていたため
- 300文字未満のAbstractは自動除外しない安全策を追加

### Test partitionの結果（修正後gold、107件）

| 指標 | strands-v2 + 旧閾値 | strands-v3 |
| --- | ---: | ---: |
| hard false negative | 0件 | 0件 |
| false positive | 12件 | 2件 |
| 自動除外できたgold-negative | 19件 / 58件 | 36件 / 58件 |
| manual review rate | 0.374 | 0.252 |
| auto accuracy | 0.821 | 0.975 |
| in-scope precision | 0.750 | 0.955 |
| operational recall | 1.000 | 1.000 |

test partitionは質問文の比較途中で一度参照しています（タイトル追加の前後）。
タイトル追加は入力の不一致の修正であり閾値はすべてcalibrationで決めていますが、test結果はやや楽観的な可能性があります。

300文字未満の安全策は、このbenchmarkでは正しい自動除外7件を `unsure` に回しており、防いだ誤除外はありません。
Recall優先の方針から残していますが、人手確認を減らしたい場合は外す候補です。

最新14日E2E（141件）では、v2の `in_scope=59 / unsure=65 / out_of_scope=17` が
v3で `in_scope=61 / unsure=40 / out_of_scope=40` になりました。

再現手順:

```bash
pixi run strands-screen benchmark/edna_strands_200.review.csv \
  --config config/strands_flagger.example.jsonc \
  --out-csv benchmark/edna_strands_200.strands.v3.csv
pixi run benchmark-eval benchmark/edna_strands_200.review.labeled.csv \
  benchmark/edna_strands_200.strands.v3.csv \
  --manifest benchmark/edna_strands_200.manifest.csv --partition test \
  --out-json benchmark/edna_strands_200.v3.test.metrics.json
```
