# Clef 27Bの実行時間を減らす

## 軽量モデルから27Bへの二段階判定

Strands Deciderで一次判定し、`in_scope` と `out_of_scope` を確定する。
Strandsは5問を順次送信し、27Bは5問を一括送信する。
[RTX 5060 Tiでの5問一括処理の比較](strands-batch-5060.md)では、40件平均で一括が約7%遅かったため、Strandsの順次送信を維持する。
最終ラベルが `unsure` の論文だけGPU 0のClef 27B Q8_0へ渡す。一次推論のエラー・不正な確率は例外として27Bへ回す。
両モデルで同じ `edna-macrofauna-v6` の質問と判定規則を使う。

設定例は `config/clef_cascade.example.jsonc`。
以前の `first_stage_min_probability` は削除し、確信度による追加ゲートを使わない。
Strandsのscope回答ではなく、採用・除外ガードを適用した最終ラベルで振り分ける。
短い抄録で除外を保留した論文は一次でも `unsure` とし、27Bへ渡す。
Strandsが確定した採用・除外の誤りは27Bで修正されない。
既存のローカル設定に `first_stage_min_probability` がある場合は削除する。

CSVの `evaluation_stage` は `first` がStrandsのみ、`second` が27Bで再判定した結果を表す。
最終採用・除外には `flag_label` を使う。
`flag_model_path` は最終スコアを出したモデル名で、一次判定のモデル・ラベル・確率・根拠の強さは `first_stage_*` 列に記録する。
`latency_ms` はHTTP待ち時間と再試行を含む両段合計、`first_stage_latency_ms` は一次判定のHTTP時間である。
一次推論に失敗した行は27Bへ回し、`first_stage_error` に理由を記録する。
実行開始時は両サーバーのhealthを確認する。

## 起動・実行例

通常の`screen`は不足するStrandsと27Bのサーバーを自動起動し、終了時に自分が起動したものだけ停止する。
GPUの空き容量を調べ、両モデルを同じGPUに配置する。
サーバーの手動起動は`fetch --strands`や`screen --no-auto-server`で必要となる。
27Bの取得は[実行手順](clef-default.md)を参照。
手動で常駐させる場合、Strandsは次のコマンドでport 8012へ起動する。

```bash
pixi run serve-strands
```

GPU 1のメモリがFlashなどで使用中の場合は、空きメモリを確認してGPU 0へ配置する。
今回の接続確認では、27BとStrandsをRTX 8000に同居させた。

```bash
pixi run python script/serve.py --preferred-gpu 0
```

別ターミナルで27Bを起動する。
起動済みのサーバーは二重起動しない。

```bash
pixi run serve
```

判定は次のコマンドで行う。
出力先は書き直されるため、既存の結果を保持する場合は新しいファイル名を指定する。

```bash
pixi run screen results/edna_latest.csv --config config/clef_cascade.example.jsonc --out-csv results/edna_latest.cascade.csv --limit 20
# 全件の場合は --limit 20 を外す
```

`fetch --strands` でも二段階にする場合は、ローカルの既定設定を作る。
既存の `config/clef_flagger.jsonc` がある場合は、内容を比較して必要な設定を追記する。

```bash
cp config/clef_cascade.example.jsonc config/clef_flagger.jsonc
pixi run fetch --email you@example.com --days 14 --strands --out-dir results --out-prefix edna_latest
```

既定の設定例とこの環境のローカル設定はStrands→27Bの二段階判定である。
27B単独にする場合は、設定の `first_stage_base_url` を `null` にする。

## キャッシュ

既定のStrands→27B設定は `.cache/strands_clef_cascade_scores.csv` に結果を保存する。
実際のファイル名には質問版を付ける。
入力タイトル・抄録、質問内容、サーバーURL、期待するモデル名、質問の一括送信、二段階の振り分け条件をキーに含める。
同じ論文でも抄録やモデル名を変えると再推論する。
27B単独はスコアだけを再利用し、閾値を変えた判定は再計算する。
二段階は閾値で選ばれるモデルも変わるため、振り分けに使う閾値が変わった場合も再推論する。
`expected_model` と `first_stage_model` はサーバーが返すモデル名と照合する。
同じaliasで重みを差し替える場合はモデル名を変更するかキャッシュを無効化する。
キャッシュヒット時のCSVの時間は元の推論時間であり、新しい実行に要した時間ではない。
キャッシュ・評価CSVはローカルのみで、コミット・pushしない。

## 以前のFlash→27B測定（現在のStrands構成とは異なる）

以下は質問版 `edna-macrofauna-v5` で取得済みCSVの先頭20件を使用した動作確認である。
現在はユーザー指定に従いStrands→27Bへ変更した。以下の時間を現在の構成の実績として扱わない。
RTX 8000で27B、RTX 5060 TiでFlashを実行し、既存の27B全件処理も継続した状態で測った。

| 設定 | Flashで確定 | 27Bで再判定 | 20件の時間 |
|---|---:|---:|---:|
| 27B単独（以前の測定） | 0 | 20 | 約59秒 |
| 一次のscope確率0.95以上 | 0 | 20 | 約71秒 |
| 判定根拠0.90以上 | 3 | 17 | 約63秒 |
| 判定根拠0.80以上 | 10 | 10 | 約44秒 |

0.80では以前の27B単独より約25%短縮し、20件の最終ラベルは27B単独とすべて一致した。
同じ20件を再実行するとキャッシュが20件すべてにヒットし、CSV判定全体が約0.5秒で完了した。
別データへ一般化できるかは未確認である。確定ラベルに基づく精度評価も未実施である。
起動・重みロードを含まず、同時処理と実行時点の違いがあるため厳密な速度比較ではない。
一次で確定した誤りは27Bで修正されない。
閾値を上げると27Bの割合が増え、一次判定の追加コストによって全件27Bより遅くなることもある。

Clefは全質問を一つのpromptにまとめ、一つの物理バッチで評価するため、既定の5問一括送信を維持する。
単にクライアントの並列数を増やしてもGPU計算量は減らない。
[llama.cppのSystem One API仕様](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md#post-v1systemone-typesafe-compatible-system-one-api)

## 以前の確信度ゲートによるStrands→27Bの動作・時間確認

v6の同じ入力20件を、二段階と27B単独で続けて実行した。
Strandsと27BはRTX 8000上に同居させ、キャッシュヒットなしで測った。

| 設定 | Strandsで確定 | 27Bで判定 | HTTP合計 |
|---|---:|---:|---:|
| Strands→27B | 3 | 17 | 96.3秒 |
| 27B単独 | 0 | 20 | 75.3秒 |

最終ラベルの差と処理エラーは0件だった。
ただし二段階は約28%遅く、Strandsの追加処理約43.6秒を27Bの省略3件で回収できなかった。
少数件・同一GPU・時点の異なる参考測定であり、独立した確定ラベルによる精度改善は未検証である。
一次モデルはユーザーの意図に従いStrandsとしたが、この構成が高速化したとは結論しない。
実装は1論文ずつの直列処理で、別論文のStrandsと27Bを重ねる並列パイプラインは未実装。

## unsureだけを再評価する変更

現在はユーザー指定に従い、Strandsの `unsure` だけを27Bで再評価する。
変更前の処理中CSV143件ではStrandsの `unsure` が99件（約69%）だったため、以前の約90%より27Bの割合が減る見込み。
短い抄録の保留を含む実際の割合と、変更後の実行時間・精度は未測定。
進行中のプロセスには変更が反映されないため、次回の実行から適用する。
