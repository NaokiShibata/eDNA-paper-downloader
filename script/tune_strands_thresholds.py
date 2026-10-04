from __future__ import annotations

import itertools
import json
from pathlib import Path

import pandas as pd
import typer

from libs.strands_screening import decide_label

app = typer.Typer(add_completion=False)


def _safe_div(num: int, den: int) -> float:
    return num / den if den else 0.0


def _metrics(frame: pd.DataFrame, labels: pd.Series) -> dict[str, float | int]:
    gold = frame["gold_label"].astype(str).str.strip().str.lower()
    keep = gold.isin({"in_scope", "out_of_scope"})
    gold = gold.loc[keep]
    pred = labels.loc[keep]

    pos = gold.eq("in_scope")
    neg = gold.eq("out_of_scope")
    tp = int((pos & pred.eq("in_scope")).sum())
    tn = int((neg & pred.eq("out_of_scope")).sum())
    hard_fn = int((pos & pred.eq("out_of_scope")).sum())
    fp = int((neg & pred.eq("in_scope")).sum())
    pos_review = int((pos & pred.eq("unsure")).sum())
    neg_review = int((neg & pred.eq("unsure")).sum())

    n = int(len(gold))
    auto = tp + tn + hard_fn + fp
    return {
        "n": n,
        "gold_pos": int(pos.sum()),
        "gold_neg": int(neg.sum()),
        "tp": tp,
        "tn": tn,
        "hard_fn": hard_fn,
        "fp": fp,
        "pos_review": pos_review,
        "neg_review": neg_review,
        "auto_coverage": _safe_div(auto, n),
        "negative_auto_exclusion": _safe_div(tn, int(neg.sum())),
        "hard_fn_rate": _safe_div(hard_fn, int(pos.sum())),
        "auto_accuracy": _safe_div(tp + tn, auto),
    }


@app.command()
def tune(
    gold_csv: Path = typer.Argument(..., exists=True, dir_okay=False),
    predictions_csv: Path = typer.Argument(..., exists=True, dir_okay=False),
    manifest_csv: Path = typer.Option(..., "--manifest", exists=True, dir_okay=False),
    include_threshold: float = typer.Option(0.45, "--include-threshold"),
    include_microbial_only_max: float = typer.Option(0.35, "--include-microbial-only-max"),
    actual_use_threshold: float = typer.Option(0.60, "--actual-use-threshold"),
    max_hard_fn: int = typer.Option(0, "--max-hard-fn", min=0),
    max_results: int = typer.Option(20, "--max-results", min=1),
    out_json: Path | None = typer.Option(None, "--out-json"),
) -> None:
    """Tune exclusion thresholds on the calibration partition only.

    Inclusion thresholds stay fixed. Candidates are ranked by the number of correctly
    auto-excluded gold-negative papers while respecting the hard false-negative limit.
    """
    gold = pd.read_csv(gold_csv, dtype=str, keep_default_na=False)
    pred = pd.read_csv(predictions_csv, dtype=str, keep_default_na=False)
    pred_count = len(pred)
    pred = pred.drop_duplicates(subset="flag_record_id", keep="last")
    if dropped := pred_count - len(pred):
        typer.echo(f"Dropped {dropped} duplicate prediction rows")
    manifest = pd.read_csv(manifest_csv, dtype=str, keep_default_na=False)

    required_gold = {"benchmark_record_id", "gold_label"}
    required_pred = {
        "flag_record_id",
        "strands_p_in_scope",
        "strands_p_out_of_scope",
        "strands_p_actual_use",
        "strands_p_microbial_only",
        "strands_p_method_relevance",
    }
    required_manifest = {"benchmark_record_id", "benchmark_partition"}

    if not required_gold.issubset(gold.columns):
        raise typer.BadParameter("gold CSV is missing required benchmark/gold columns")
    if not required_pred.issubset(pred.columns):
        raise typer.BadParameter("prediction CSV is missing required Strands probability columns")
    if not required_manifest.issubset(manifest.columns):
        raise typer.BadParameter("manifest CSV is missing benchmark_partition")

    frame = gold.merge(
        pred,
        left_on="benchmark_record_id",
        right_on="flag_record_id",
        how="inner",
        suffixes=("", "_pred"),
    ).merge(
        manifest[["benchmark_record_id", "benchmark_partition"]],
        on="benchmark_record_id",
        how="left",
    )

    frame = frame.loc[frame["benchmark_partition"].eq("calibration")].copy()
    frame = frame.loc[
        frame["gold_label"].astype(str).str.strip().str.lower().isin({"in_scope", "out_of_scope"})
    ].copy()
    if frame.empty:
        raise typer.BadParameter("no binary gold rows found in calibration partition")

    for col in [
        "strands_p_in_scope",
        "strands_p_out_of_scope",
        "strands_p_actual_use",
        "strands_p_microbial_only",
        "strands_p_method_relevance",
    ]:
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame = frame.dropna(
        subset=[
            "strands_p_in_scope",
            "strands_p_out_of_scope",
            "strands_p_actual_use",
            "strands_p_microbial_only",
            "strands_p_method_relevance",
        ]
    )

    # Coarse grid intentionally favors interpretable thresholds and avoids overfitting
    # a small calibration set to arbitrary hundredths.
    exclude_thresholds = [0.50, 0.60, 0.70, 0.75, 0.80, 0.85, 0.90]
    actual_max_values = [0.15, 0.20, 0.30, 0.40, 0.50, 0.60]
    method_max_values = [0.30, 0.40, 0.50, 0.60, 0.70, 0.80]

    rows: list[dict[str, float | int]] = []
    for ex, au, mr in itertools.product(
        exclude_thresholds, actual_max_values, method_max_values
    ):
        labels = frame.apply(
            lambda row, **thresholds: decide_label(
                float(row["strands_p_in_scope"]),
                float(row["strands_p_out_of_scope"]),
                float(row["strands_p_actual_use"]),
                float(row["strands_p_method_relevance"]),
                float(row["strands_p_microbial_only"]),
                **thresholds,
            ),
            axis=1,
            include_threshold=include_threshold,
            include_microbial_only_max=include_microbial_only_max,
            actual_use_threshold=actual_use_threshold,
            exclude_threshold=ex,
            exclude_actual_use_max=au,
            exclude_method_relevance_max=mr,
        )
        m = _metrics(frame, labels)
        if int(m["hard_fn"]) > max_hard_fn:
            continue
        rows.append(
            {
                "exclude_threshold": ex,
                "exclude_actual_use_max": au,
                "exclude_method_relevance_max": mr,
                **m,
            }
        )

    if not rows:
        raise typer.BadParameter(
            f"no threshold combination satisfied max_hard_fn={max_hard_fn}"
        )

    result = pd.DataFrame(rows)
    result = result.sort_values(
        by=[
            "tn",
            "negative_auto_exclusion",
            "auto_accuracy",
            "auto_coverage",
            "exclude_threshold",
            "exclude_actual_use_max",
            "exclude_method_relevance_max",
        ],
        ascending=[False, False, False, False, False, True, True],
    ).reset_index(drop=True)

    typer.echo(
        f"Calibration rows: {len(frame)} "
        f"(in_scope={(frame['gold_label'].str.lower() == 'in_scope').sum()}, "
        f"out_of_scope={(frame['gold_label'].str.lower() == 'out_of_scope').sum()})"
    )
    typer.echo("")
    typer.echo(result.head(max_results).to_string(index=False))

    best = result.iloc[0].to_dict()
    typer.echo("")
    typer.echo("Suggested exclusion thresholds (calibration only):")
    typer.echo(f'  "exclude_threshold": {best["exclude_threshold"]:.2f},')
    typer.echo(f'  "exclude_actual_use_max": {best["exclude_actual_use_max"]:.2f},')
    typer.echo(
        f'  "exclude_method_relevance_max": '
        f'{best["exclude_method_relevance_max"]:.2f}'
    )
    typer.echo(
        f'  calibration: TN={int(best["tn"])}/{int(best["gold_neg"])} '
        f'hard_FN={int(best["hard_fn"])} auto_coverage={best["auto_coverage"]:.3f}'
    )

    if out_json is not None:
        out_json.parent.mkdir(parents=True, exist_ok=True)
        top = json.loads(result.head(max_results).to_json(orient="records", force_ascii=False, indent=2))
        payload = {
            "fixed_include_threshold": include_threshold,
            "fixed_include_microbial_only_max": include_microbial_only_max,
            "fixed_actual_use_threshold": actual_use_threshold,
            "max_hard_fn": max_hard_fn,
            "best": top[0],
            "top": top,
        }
        out_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        typer.echo(f"Threshold search -> {out_json}")


if __name__ == "__main__":
    app()
