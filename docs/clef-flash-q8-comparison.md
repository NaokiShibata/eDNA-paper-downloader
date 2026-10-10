# Clef-Flash Q8_0とStandardの比較

Issue: https://github.com/NaokiShibata/eDNA-paper-downloader/issues/8

指定されたchef-flash:Q8は、公式配布名`ggml-org/Clef-Flash-GGUF:Q8_0`として検討した。
Standardは現行のStrands v4の質問と既定閾値を指す。
本番のモデルや設定は変更していない。

2026年10月9日の固定条件比較では、Clefは誤採用を減らしたが、直近100件では誤除外と要確認が増えた。
見逃しを抑える現行方針では全面置換を勧めず、追加した設定での試験利用に留める。

## 実測結果

各区画の基準ラベルが`in_scope`または`out_of_scope`の論文だけを集計した。
正採用は対象論文を自動採用した件数、誤除外は対象論文を自動除外した件数である。
要確認に回した対象論文は誤除外に含めない。

| 区画 | 方法 | 集計対象 | 正採用 | 誤採用 | 誤除外 | 要確認 | 採用precision |
|---|---|---:|---:|---:|---:|---:|---:|
| test | Standard | 107 | 42 | 2 | 0 | 20 | 95.5% |
| test | Clef Q8_0 | 107 | 43 | 1 | 0 | 18 | 97.7% |
| holdout | Standard | 99 | 53 | 1 | 1 | 13 | 98.1% |
| holdout | Clef Q8_0 | 99 | 52 | 0 | 1 | 15 | 100.0% |
| 直近100件 | Standard | 93 | 36 | 5 | 1 | 15 | 87.8% |
| 直近100件 | Clef Q8_0 | 93 | 37 | 2 | 2 | 19 | 94.9% |

testでは正採用が1件増え、誤採用が1件減った。
holdoutでは誤採用を1件減らす一方、正採用も1件減り、要確認が2件増えた。
両モデルが誤除外した論文はH084で一致した。
calibrationでも既定条件のClefには誤採用1件、誤除外1件があり、今回の評価では閾値調整を行っていない。

直近100件では正採用が1件増え、誤採用が3件減ったが、新しい誤除外N025が生じた。
N025はイルカの生検、剥離皮膚、海水eDNAを比較した研究で、個体識別に成功したのは生検だけだった。
eDNAの不成功も報告する実使用研究として基準ラベルは対象だが、Clefは除外した。
既存データの再解析を含むN017も両モデルが除外した。
Clefの誤採用は計画報告N079と、人の考古遺体由来病原体DNAを扱うN091だった。
N091はStandardが除外できた論文なので、誤採用の減少は全例の改善を意味しない。

情報不足ラベルの論文はprecisionの分母から除いた。
holdoutではClefが情報不足1件を採用しており、表の100.0%を全採用論文の確定精度とは解釈しない。
直近100件ではStandardが情報不足4件、Clefが3件を採用した。
情報不足を含む100件全体の要確認は、Standardの16件に対してClefは22件だった。
直近100件の基準ラベルは、以前の検証時にタイトルと抄録から付けた暫定ラベルであり、専門家や全文による確定評価ではない。

400件の推論で回答欠落、不正な確率、処理エラーは0件だった。
予測、指標、入力・ラベル・Standard予測のハッシュ、判定が変わった論文は、ローカルの`benchmark/clef/`に保存した。
`comparison.json`と`paired-differences.csv`で照合できる。
比較に使った保存済みStandard予測と直近100件の入力・ラベルを`benchmark/clef/reference/`へ複製した。
元の資料は変更せず、手元の記録から集計を再現できるようにした。
評価データはコミット・pushせず、このMarkdownにまとめた比較結果だけを公開する。

## 実行条件と時間

llama.cppは`0.6.0-dev`、build 11510、commit `c35b66744`を使用した。
GPU 0のQuadro RTX 8000（48 GiB）で起動し、推論中に観測したGPU全体の使用量は約10.0 GiBだった。
これは他の割り当てを含む観測値であり、Clef単体のピークメモリ測定ではない。
ウォームアップ後のHTTP応答時間の平均は、200件が0.77秒、holdoutが0.86秒、直近100件が0.90秒だった。
入力長、GPU世代、負荷、質問の送信方式が異なるため、既存Strandsの保存済み時間から速度優位は判断しない。
重みロード時間とファイルハッシュ計算は平均応答時間に含めていない。

62件の単体テスト、ruff、mypyを確認し、既存`screen.py`からの1件の実接続も成功した。

## 導入条件

| 項目 | Clef-Flash Q8_0 | Standard（Strands v4） |
|---|---|---|
| 推論方式 | llama.cppでGGUFを実行 | strands-deciderのHTTPサーバー |
| API | `/v1/systemone` | `/v1/systemone` |
| 今回の質問実行 | 5問をまとめて送信 | 保存済み評価は質問ごとに送信 |
| 質問・判定規則 | Strands v4の既存コードを使用 | 既定のStrands v4 |
| 重み | 9B、Q8_0、9,657,260,192 bytes | 現行モデルの保存済み判定を参照 |
| 本番への変更 | 評価後に判断 | 現状維持 |

Cloudflareのモデルカードでは、Clef-FlashはQwen3.5-9Bを基にした決定モデルで、選択肢の確率を返す。
GGUFモデルカードでは専用の`/v1/systemone` APIとllama.cppのClef対応を必要としている。
Q8_0のファイルサイズとは別に、KVキャッシュや推論バッファ用のメモリが必要になる。
Cloudflareが公開したモデルをローカルで実行する検討であり、Workers AIへのデプロイは含めない。

公式資料は[Cloudflareのモデルカード](https://huggingface.co/Cloudflare/clef-flash)、[ggml-orgのGGUF配布](https://huggingface.co/ggml-org/Clef-Flash-GGUF)、[llama.cppのClef対応PR](https://github.com/ggml-org/llama.cpp/pull/29831)を参照した。
両配布のモデルカードにApache-2.0の表示がある。

## 再現手順

GGUFのリビジョンは`4a192915ef971886004b5b13294f2b4c7a7fc39d`に固定する。
Q8_0のSHA256は`d7c352faf1bdd9ea24d0b9347e8eb1eb4bbadeff6c02383bf750215a74f2f1f1`である。
評価スクリプトは入力と質問文のハッシュ、閾値、実測時間を保存する。
既存の出力を上書きせず、不正な確率や欠落した回答があれば停止する。
Strands固有名の診断列は既存の集計コードを使うために維持し、`flag_model_path`とrun.jsonで実際のモデルを区別する。

```sh
rtk proxy mkdir -p .cache/clef
rtk proxy curl -fL --retry 2 https://huggingface.co/ggml-org/Clef-Flash-GGUF/resolve/4a192915ef971886004b5b13294f2b4c7a7fc39d/Clef-Flash-Q8_0.gguf -o .cache/clef/Clef-Flash-Q8_0.gguf
rtk proxy env CUDA_VISIBLE_DEVICES=0 llama-server -m .cache/clef/Clef-Flash-Q8_0.gguf --alias clef-flash-q8 --host 127.0.0.1 --port 8014 -ngl 99 -c 8192 -b 8192 -ub 8192 -np 1
```

サーバーを起動したまま別端末で実行する。
決定モデルは入力全体を物理バッチに収める必要があり、既定の512では質問だけで超過してHTTP 500になった。
今回の入力用に`-b`と`-ub`を8192へ増やした。

通常のCSVを試す場合は、`screen.py`に`--config config/clef_flagger.example.jsonc`を指定できる。
モデルの異なるスコアを流用しないようキャッシュを無効化する。
検証用の例では閾値を既定値から変更せず、既存のStrands設定を上書きしない。
以下は評価時のコマンドであり、直近100件の再実行には公開していないローカルの入力・ラベルが必要となる。

```sh
rtk proxy .venv/bin/python script/benchmark_clef.py benchmark/edna_strands_200.review.csv benchmark/clef/clef.200.csv
rtk proxy .venv/bin/python script/benchmark_clef.py benchmark/edna_holdout_100.review.csv benchmark/clef/clef.holdout.csv
rtk proxy .venv/bin/python script/benchmark.py eval benchmark/edna_strands_200.review.labeled.csv benchmark/clef/clef.200.csv --manifest benchmark/edna_strands_200.manifest.csv --partition test --out-json benchmark/clef/clef.test.metrics.json --out-errors benchmark/clef/clef.test.errors.csv
rtk proxy .venv/bin/python script/benchmark.py eval benchmark/edna_holdout_100.review.labeled.csv benchmark/clef/clef.holdout.csv --partition all --out-json benchmark/clef/clef.holdout.metrics.json --out-errors benchmark/clef/clef.holdout.errors.csv
rtk proxy .venv/bin/python script/benchmark_clef.py benchmark/clef/reference/fresh.review.csv benchmark/clef/clef.fresh.csv
rtk proxy .venv/bin/python script/benchmark.py eval benchmark/clef/reference/fresh.review.labeled.csv benchmark/clef/clef.fresh.csv --partition all --out-json benchmark/clef/clef.fresh.metrics.json --out-errors benchmark/clef/clef.fresh.errors.csv
```

## 評価の限界

Strands向けに調整済みの質問と閾値をClefへそのまま適用するため、まず置換時の挙動を比較する。
両モデルを対称に最適化した性能比較にはならない。
この検討ではtestやholdoutの結果に合わせた閾値調整を行わない。
保存済みStrands評価との比較では、入力、質問文、既定閾値を揃えた精度指標を比較できるが、同一時点のGPU負荷で測定した速度比較にはならない。
これらは既存のモデル比較に使用済みのデータであり、今回新たに取得した独立データではない。
モデルカードの一般ベンチマークを、eDNA判定の性能やQ8_0の実測値として代用しない。

次に検討するなら、不成功のeDNA実使用と考古遺体由来DNAの区別をcalibrationだけで調整し、別の未使用データで誤除外が増えないことを確認する。
今回のtest、holdout、直近100件の結果に合わせて条件を変更し、その同じデータで採用可否を確定しない。
