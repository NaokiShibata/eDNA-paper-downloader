# Strands Decider benchmark

このドキュメントでは、`script/strands_flagger.py` のeDNA/eRNA論文スクリーニングを検証するための
benchmark作成、gold label付与、閾値調整、評価手順をまとめます。

現在の論文判定はStrands Deciderに統一しています。

## 現在の判定閾値

Method系の関連論文を誤って除外しにくいことを優先し、現在は以下をデフォルトとしています。

```json
{
  "include_threshold": 0.70,
  "actual_use_threshold": 0.60,
  "exclude_threshold": 0.50,
  "exclude_actual_use_max": 0.50,
  "exclude_method_relevance_max": 0.60
}
```

判定は次の方針です。

- `in_scope`: `P(in_scope) >= 0.70` かつ `P(actual_use) >= 0.60`
- `out_of_scope`: `P(out_of_scope) >= 0.50`、`P(actual_use) <= 0.50`、
  `P(method_relevance) <= 0.60` をすべて満たす
- それ以外: `unsure` として人手確認

`microbial_only` は診断値として保存しますが、現時点では最終ラベルの条件には使用しません。

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

`benchmark/edna_strands_200.review.labeled.csv`（natural 120件 + challenge 80件、人手gold）で、
現行の閾値（上記の既定値）をtest partitionで評価した結果:

| 指標 | 結果 |
| --- | ---: |
| binary gold | 107件 |
| gold in_scope | 63件 |
| gold out_of_scope | 44件 |
| hard false negative | 0件 |
| operational recall | 1.000 |
| auto coverage | 0.626 |
| manual review rate | 0.374 |
| auto accuracy | 0.925 |
| in-scope precision | 0.896 |

calibration partition（86件）で除外閾値を探索すると、
`exclude_actual_use_max=0.60`、`exclude_method_relevance_max=0.80` が提案されました
（`benchmark/edna_strands_200.thresholds.json`）。
ただしtest partitionでの改善は自動除外が19件から20件に増える1件のみで、
`exclude_method_relevance_max=0.80` は探索範囲の上限でもあるため、Recall重視の方針から既定値は変更していません。

False Positive 5件はいずれもmicrobial community profiling系（土壌真菌、ブロメリア貯水の微生物群集、
有害微細藻類群集、16S定量）と、魚類の捕食検出PCRでした。
`microbial_only` を `in_scope` の追加条件にする案も検証しましたが、
これらの論文では `P(microbial_only)` が0.5未満のため効果はありませんでした。
改善するには `microbial_only` の質問文の見直し（`PROMPT_VERSION` の更新を伴う）が必要です。

再現手順:

```bash
pixi run strands-screen benchmark/edna_strands_200.review.csv \
  --config config/strands_flagger.example.jsonc \
  --out-csv benchmark/edna_strands_200.strands.v2.csv
pixi run benchmark-eval benchmark/edna_strands_200.review.labeled.csv \
  benchmark/edna_strands_200.strands.v2.csv \
  --manifest benchmark/edna_strands_200.manifest.csv --partition test \
  --out-errors benchmark/edna_strands_200.errors.csv \
  --out-json benchmark/edna_strands_200.metrics.json
pixi run benchmark-tune benchmark/edna_strands_200.review.labeled.csv \
  benchmark/edna_strands_200.strands.v2.csv \
  --manifest benchmark/edna_strands_200.manifest.csv --max-hard-fn 0 \
  --out-json benchmark/edna_strands_200.thresholds.json
```
