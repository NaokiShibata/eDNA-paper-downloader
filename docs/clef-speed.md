# Clef 27Bの実行時間を減らす

## 軽量モデルから27Bへの二段階判定

GPU 1のClef-Flash Q8_0で一次判定し、確信度の高い採用・除外を確定する。
要確認と確信度の低い判定だけGPU 0のClef 27B Q8_0へ渡す。
両モデルで同じ `edna-macrofauna-v5` の質問と判定規則を使う。

設定例は `config/clef_cascade.example.jsonc`。
`first_stage_min_probability` は速度重視の初期値0.80とした。
採用の根拠は `p_in_scope`、除外の根拠は `p_out_of_scope`、除外条件を満たす `p_microbial_only` または `p_review` とする。
一次判定が `unsure` の場合は、scope確率が高くても27Bへ渡す。
短い抄録の自動除外防止は最終段でも維持する。
0.80は独立データで校正した閾値ではなく、モデルの出す確率を確定精度と解釈しない。

CSVの `evaluation_stage` は `first` がFlashのみ、`second` が27Bで再判定した結果を表す。
最終採用・除外には `flag_label` を使う。
`flag_model_path` は最終スコアを出したモデル名で、一次判定のモデル・ラベル・確率・根拠の強さは `first_stage_*` 列に記録する。
`latency_ms` はHTTP待ち時間と再試行を含む両段合計、`first_stage_latency_ms` は一次判定のHTTP時間である。
一次推論に失敗した行は27Bへ回し、`first_stage_error` に理由を記録する。
実行開始時は両サーバーのhealthを確認する。

## 起動・実行例

両GPUとモデルファイルが必要である。
27Bの取得は[実行手順](clef-default.md)を参照。
Flashをまだ取得していない場合は、リポジトリのルートで実行する。

```bash
mkdir -p .cache/clef
curl -fL --retry 2 https://huggingface.co/ggml-org/Clef-Flash-GGUF/resolve/4a192915ef971886004b5b13294f2b4c7a7fc39d/Clef-Flash-Q8_0.gguf -o .cache/clef/Clef-Flash-Q8_0.gguf
printf '%s  %s\n' d7c352faf1bdd9ea24d0b9347e8eb1eb4bbadeff6c02383bf750215a74f2f1f1 .cache/clef/Clef-Flash-Q8_0.gguf | sha256sum -c -
pixi run serve-fast
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

既定の設定例は27B単独のままで、二段階判定には明示指定か上記のローカル設定が必要である。

## キャッシュ

既定の27B設定は `.cache/clef_27b_scores.csv`、二段階設定は `.cache/clef_cascade_scores.csv` に結果を保存する。
実際のファイル名には質問版を付ける。
入力タイトル・抄録、質問内容、サーバーURL、期待するモデル名、質問の一括送信、二段階の振り分け条件をキーに含める。
同じ論文でも抄録やモデル名を変えると再推論する。
27B単独はスコアだけを再利用し、閾値を変えた判定は再計算する。
二段階は閾値で選ばれるモデルも変わるため、振り分けに使う閾値が変わった場合も再推論する。
`expected_model` と `first_stage_model` はサーバーが返すモデル名と照合する。
同じaliasで重みを差し替える場合はモデル名を変更するかキャッシュを無効化する。
キャッシュヒット時のCSVの時間は元の推論時間であり、新しい実行に要した時間ではない。
キャッシュ・評価CSVはローカルのみで、コミット・pushしない。

## 実測と限界

取得済みCSVの先頭20件を使用した動作確認。
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
