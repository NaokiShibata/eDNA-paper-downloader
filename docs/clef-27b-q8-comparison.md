# Clef 27B Q8_0とFlash・Standardの比較

Issue: https://github.com/NaokiShibata/eDNA-paper-downloader/issues/8

RTX 8000で27B版のClefを実行し、前回のClef-Flash Q8_0とStandard（Strands v4）の保存済み結果に対して検証した。
質問、既定閾値、入力、基準ラベルは前回と同じものを使用する。
評価結果を見て質問や閾値を調整せず、直近100件、holdout 100件、既存200件の順に推論した。
評価データはローカルの`benchmark/clef/27b/`に保持し、コミット・pushしない。

直近100件とholdout 100件の完了後、既存200件の58件保存時点で一度中断した。
ユーザーの再開指示に従い、200件は別の出力先`benchmark/clef/27b/resumed/`で最初から再実行した。
途中の58件は最終集計に使用していない。
再実行後に中断前の58件と照合し、7種類の確率と判定ラベルがすべて一致した。

今回の固定条件では27BがFlashより有望だった。
3区画とも誤採用件数を増やさず正採用が2件ずつ増え、直近100件の誤除外も2件から0件に減った。
採用precisionの上昇幅は小さく、主な利点は対象論文の採用数と確認負担の改善にある。
一方、HTTP平均応答は約2.6〜3.1秒で、Flashの保存済み測定より約2.9〜3.8倍長かった。
本番のStandard設定は変更せず、独立した未使用データと専門家確認による検証を経てから置換を判断する。

## 直近100件の結果

情報不足ラベル7件を除いた93件で集計した。
基準ラベルは対象47件、対象外46件である。

| 方法 | 正採用 | 誤採用 | 誤除外 | 要確認 | 採用precision |
|---|---:|---:|---:|---:|---:|
| Standard | 36 | 5 | 1 | 15 | 87.8% |
| Clef-Flash Q8_0 | 37 | 2 | 2 | 19 | 94.9% |
| Clef 27B Q8_0 | 39 | 2 | 0 | 18 | 95.1% |

27BはFlashより正採用が2件多く、誤除外が2件少なかった。
採用precisionの上昇は小さく、主な改善は対象論文を自動除外しなかった点にある。
不成功の海水eDNA採取も報告するイルカ研究N025を採用し、両モデルが除外していた既存データ再解析N017を要確認へ戻した。
N074とN095も要確認から採用へ変わった一方、Flashが採用していた考古学的環境試料の研究N023は要確認となった。

誤採用2件の内容は同じではない。
27Bは考古遺体由来病原体DNAの研究N091を要確認へ戻したが、Flashが要確認にした鳥の食性解析N046を採用した。
計画報告N079の誤採用は両モデルに残った。

情報不足を含む100件全体の要確認は、Standardが16件、Flashが22件、27Bが21件だった。
情報不足のまま採用された論文は、Standardが4件、Flashが3件、27Bが4件である。
これらは正採用にも誤採用にも数えていない。

## Holdoutの結果

情報不足1件を除く99件で比較した。

| 方法 | 正採用 | 誤採用 | 誤除外 | 要確認 | 採用precision |
|---|---:|---:|---:|---:|---:|
| Standard | 53 | 1 | 1 | 13 | 98.1% |
| Clef-Flash Q8_0 | 52 | 0 | 1 | 15 | 100.0% |
| Clef 27B Q8_0 | 54 | 0 | 1 | 13 | 100.0% |

27BはFlashより正採用が2件増え、要確認が2件減った。
誤採用・誤除外の件数は変わらず、H084の誤除外はStandardを含む3モデルすべてに残った。
Flashと27Bはともに情報不足の1件を採用しているため、100.0%は全採用論文の確定精度ではない。

## Test区画の結果

既存200件のうち、test区画の基準ラベルが対象・対象外の107件で集計した。

| 方法 | 正採用 | 誤採用 | 誤除外 | 要確認 | 採用precision |
|---|---:|---:|---:|---:|---:|
| Standard | 42 | 2 | 0 | 20 | 95.5% |
| Clef-Flash Q8_0 | 43 | 1 | 0 | 18 | 97.7% |
| Clef 27B Q8_0 | 45 | 1 | 0 | 17 | 97.8% |

27BはFlashより正採用が2件増え、要確認が1件減った。
誤採用は両方1件だが、FlashはアメーバのDNAバーコーディング研究B0189、27Bは土壌細菌の群集解析手法の研究B0184を採用した。
同じ件数でも誤りの内容は異なり、すべての論文で27Bの判定が改善したわけではない。

calibration区画でも固定条件のまま集計し、27Bは正採用43件、誤採用1件、誤除外0件、要確認14件だった。
この結果に合わせた閾値調整は行っていない。

## 確率と実行コスト

scopeの`p_in_scope`に対するBrier scoreは低いほどよい。
27Bの確率予測は直近100件では改善したが、testとholdoutではFlashより悪化した。
固定閾値での採用判断の改善と、確率予測そのものの改善を区別する。

| 区画 | Flash Brier | 27B Brier |
|---|---:|---:|
| test | 0.0708 | 0.0785 |
| holdout | 0.0564 | 0.0583 |
| 直近100件 | 0.1108 | 0.1007 |

llama.cpp `0.6.0-dev`、build 11510、commit `c35b66744`、ドライバ595.84を使用した。
推論中に観測したRTX 8000全体のメモリ使用量は28,898 MiB、約28.2 GiBだった。
他の割り当てを含む観測値であり、モデル単体のピークメモリ測定ではない。

| 入力 | Flash HTTP平均 | 27B HTTP平均 | 観測された時間比 |
|---|---:|---:|---:|
| 既存200件 | 0.77秒 | 2.95秒 | 3.8倍 |
| holdout 100件 | 0.86秒 | 3.12秒 | 3.6倍 |
| 直近100件 | 0.90秒 | 2.61秒 | 2.9倍 |

両モデルを同じGPUとllama.cpp、5問一括送信で測定したが、実行時点と周辺負荷は異なる。
この時間比を厳密な速度比較とは扱わない。
ウォームアップ後のHTTP応答時間であり、重みロードとファイルハッシュ計算を含めない。

400件の完成した予測で回答欠落、不正な確率、処理エラーは0件だった。
入力・ラベル・質問・設定の固定確認、比較指標、判定差分、再開時の再現確認はローカルに保存した。
検証用サーバーは停止済みである。
27B対応コードは63件の単体テスト、ruff、mypyを通過した。

## モデルと固定条件

[ggml-org/Clef-GGUF](https://huggingface.co/ggml-org/Clef-GGUF)の`Clef-Q8_0.gguf`を使用する。
27B版は[Cloudflare/clef](https://huggingface.co/Cloudflare/clef)に由来する決定モデルである。
Flash版と同じQ8_0で比較するが、モデルサイズだけでなく基盤モデルと学習も異なるため、パラメータ数の増加だけの効果を分離した実験ではない。

| 項目 | 設定 |
|---|---|
| GGUFリビジョン | `63840a1a68cb7084c88610cffc328509356b04cb` |
| ファイルサイズ | 28,732,215,360 bytes |
| SHA256 | `07c6410af7011e0e56873a3b0b3f4ad9e31fb176f0ca80d6fd1525fe6f036548` |
| GPU | 物理GPU 0、Quadro RTX 8000、48 GiB |
| API | `/v1/systemone`、5問を一括送信 |
| 質問と閾値 | Strands v4の既定設定、調整なし |
| キャッシュ | 無効 |

前回の[Flash比較資料](clef-flash-q8-comparison.md)で示した指標を基準とする。
保存済み予測を照合し、情報不足ラベルを正採用・誤採用の集計から除く。
誤除外と要確認の負担も併記し、自動判定した論文だけのaccuracyや採用precisionから優劣を断定しない。

## 再現手順

```sh
rtk proxy mkdir -p .cache/clef
rtk proxy curl -fL --retry 2 https://huggingface.co/ggml-org/Clef-GGUF/resolve/63840a1a68cb7084c88610cffc328509356b04cb/Clef-Q8_0.gguf -o .cache/clef/Clef-Q8_0.gguf
rtk proxy env CUDA_VISIBLE_DEVICES=0 llama-server -m .cache/clef/Clef-Q8_0.gguf --alias clef-27b-q8 --host 127.0.0.1 --port 8014 -ngl 99 -c 8192 -b 8192 -ub 8192 -np 1
```

サーバー起動後、別端末で実行する。
前回の直近100件の入力・基準ラベルは公開していないため、対応するローカル資料が必要となる。

```sh
rtk proxy .venv/bin/python script/benchmark_clef.py benchmark/clef/reference/fresh.review.csv benchmark/clef/27b/clef.fresh.csv --model 27b
rtk proxy .venv/bin/python script/benchmark_clef.py benchmark/edna_holdout_100.review.csv benchmark/clef/27b/clef.holdout.csv --model 27b
rtk proxy .venv/bin/python script/benchmark_clef.py benchmark/edna_strands_200.review.csv benchmark/clef/27b/resumed/clef.200.csv --model 27b
rtk proxy .venv/bin/python script/benchmark.py eval benchmark/clef/reference/fresh.review.labeled.csv benchmark/clef/27b/clef.fresh.csv --partition all --out-json benchmark/clef/27b/clef.fresh.metrics.json
rtk proxy .venv/bin/python script/benchmark.py eval benchmark/edna_holdout_100.review.labeled.csv benchmark/clef/27b/clef.holdout.csv --partition all --out-json benchmark/clef/27b/clef.holdout.metrics.json
rtk proxy .venv/bin/python script/benchmark.py eval benchmark/edna_strands_200.review.labeled.csv benchmark/clef/27b/resumed/clef.200.csv --manifest benchmark/edna_strands_200.manifest.csv --partition test --out-json benchmark/clef/27b/clef.test.metrics.json
```

## 評価の限界

前回と同じ既存データを使うため、新しい独立データでの検証ではない。
直近100件のラベルはタイトルと抄録による暫定基準で、専門家や全文の確認を経ていない。
Standard向けに調整済みの質問・閾値を固定して適用する比較であり、Clefごとに最適化した最高性能を測る実験ではない。
