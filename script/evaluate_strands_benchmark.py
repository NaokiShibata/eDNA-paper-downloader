from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd
import typer

app = typer.Typer(add_completion=False)

VALID_GOLD = {"in_scope", "out_of_scope", "unsure"}
AUTO_LABELS = {"in_scope", "out_of_scope"}


def _safe_div(num: float, den: float) -> float:
    return float(num / den) if den else 0.0


def _norm_label(value: Any) -> str:
    text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "include": "in_scope",
        "included": "in_scope",
        "relevant": "in_scope",
        "yes": "in_scope",
        "exclude": "out_of_scope",
        "excluded": "out_of_scope",
        "irrelevant": "out_of_scope",
        "no": "out_of_scope",
        "unclear": "unsure",
        "uncertain": "unsure",
        "review": "unsure",
    }
    return aliases.get(text, text)


def _binary_aux_metrics(
    merged: pd.DataFrame,
    gold_col: str,
    pred_col: str,
) -> dict[str, float | int] | None:
    if gold_col not in merged.columns or pred_col not in merged.columns:
        return None

    frame = merged[[gold_col, pred_col]].copy()
    frame[gold_col] = frame[gold_col].astype(str).str.strip().str.lower()
    frame = frame.loc[frame[gold_col].isin({"true", "false", "1", "0", "yes", "no"})].copy()
    if frame.empty:
        return None

    gold = frame[gold_col].isin({"true", "1", "yes"})
    pred_score = pd.to_numeric(frame[pred_col], errors="coerce")
    valid = pred_score.notna()
    gold = gold.loc[valid]
    pred_score = pred_score.loc[valid]
    if pred_score.empty:
        return None

    pred = pred_score >= 0.5
    tp = int((gold & pred).sum())
    tn = int((~gold & ~pred).sum())
    fp = int((~gold & pred).sum())
    fn = int((gold & ~pred).sum())

    return {
        "n": int(len(pred)),
        "accuracy": _safe_div(tp + tn, len(pred)),
        "precision": _safe_div(tp, tp + fp),
        "recall": _safe_div(tp, tp + fn),
        "tp": tp,
        "tn": tn,
        "fp": fp,
        "fn": fn,
    }


def _metrics(frame: pd.DataFrame) -> dict[str, float | int]:
    gold = frame["gold_label_norm"]

    binary = frame.loc[gold.isin({"in_scope", "out_of_scope"})].copy()
    gold_b = binary["gold_label_norm"]
    pred_b = binary["flag_label_norm"]

    positive = gold_b.eq("in_scope")
    negative = gold_b.eq("out_of_scope")
    pred_in = pred_b.eq("in_scope")
    pred_out = pred_b.eq("out_of_scope")
    review = ~pred_b.isin(AUTO_LABELS)

    tp = int((positive & pred_in).sum())
    hard_fn = int((positive & pred_out).sum())
    positive_review = int((positive & review).sum())
    fp = int((negative & pred_in).sum())
    tn = int((negative & pred_out).sum())
    negative_review = int((negative & review).sum())

    auto_decided = int((pred_b.isin(AUTO_LABELS)).sum())
    n = int(len(binary))
    gold_pos = int(positive.sum())
    gold_neg = int(negative.sum())
    reviewed = positive_review + negative_review

    metrics: dict[str, float | int] = {
        "n_binary_gold": n,
        "gold_in_scope": gold_pos,
        "gold_out_of_scope": gold_neg,
        "tp": tp,
        "tn": tn,
        "false_positive": fp,
        "hard_false_negative": hard_fn,
        "positive_sent_to_review": positive_review,
        "negative_sent_to_review": negative_review,
        "auto_decided": auto_decided,
        "manual_review": reviewed,
        "auto_coverage": _safe_div(auto_decided, n),
        "manual_review_rate": _safe_div(reviewed, n),
        "auto_accuracy": _safe_div(tp + tn, auto_decided),
        "in_scope_precision": _safe_div(tp, tp + fp),
        "raw_recall_if_review_counts_as_miss": _safe_div(tp, gold_pos),
        "operational_recall_if_unsure_is_reviewed": _safe_div(tp + positive_review, gold_pos),
        "hard_false_negative_rate": _safe_div(hard_fn, gold_pos),
        "auto_false_positive_rate": _safe_div(fp, gold_neg),
    }

    if "strands_p_in_scope" in binary.columns:
        prob = pd.to_numeric(binary["strands_p_in_scope"], errors="coerce")
        valid = prob.notna()
        if valid.any():
            y = binary.loc[valid, "gold_label_norm"].eq("in_scope").astype(float)
            p = prob.loc[valid].clip(0.0, 1.0)
            metrics["brier_p_in_scope"] = float(((p - y) ** 2).mean())

    return metrics


def _print_metrics(title: str, metrics: dict[str, float | int]) -> None:
    typer.echo("")
    typer.echo(title)
    typer.echo("-" * len(title))
    order = [
        "n_binary_gold",
        "gold_in_scope",
        "gold_out_of_scope",
        "tp",
        "tn",
        "false_positive",
        "hard_false_negative",
        "positive_sent_to_review",
        "negative_sent_to_review",
        "auto_coverage",
        "manual_review_rate",
        "auto_accuracy",
        "in_scope_precision",
        "raw_recall_if_review_counts_as_miss",
        "operational_recall_if_unsure_is_reviewed",
        "hard_false_negative_rate",
        "auto_false_positive_rate",
        "brier_p_in_scope",
    ]
    for key in order:
        if key not in metrics:
            continue
        value = metrics[key]
        if isinstance(value, float):
            typer.echo(f"{key:40s} {value:.4f}")
        else:
            typer.echo(f"{key:40s} {value}")


def _subset_table(frame: pd.DataFrame, column: str) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if column not in frame.columns:
        return pd.DataFrame()
    for value, group in frame.groupby(column, dropna=False):
        metrics = _metrics(group)
        rows.append(
            {
                column: str(value),
                "n": metrics["n_binary_gold"],
                "auto_coverage": metrics["auto_coverage"],
                "manual_review_rate": metrics["manual_review_rate"],
                "auto_accuracy": metrics["auto_accuracy"],
                "in_scope_precision": metrics["in_scope_precision"],
                "operational_recall": metrics["operational_recall_if_unsure_is_reviewed"],
                "hard_fn_rate": metrics["hard_false_negative_rate"],
            }
        )
    return pd.DataFrame(rows).sort_values(["n", column], ascending=[False, True])


@app.command()
def evaluate(
    gold_csv: Path = typer.Argument(..., exists=True, dir_okay=False),
    predictions_csv: Path = typer.Argument(..., exists=True, dir_okay=False),
    manifest_csv: Path | None = typer.Option(None, "--manifest", exists=True, dir_okay=False),
    partition: str = typer.Option(
        "test",
        "--partition",
        help="test, calibration, or all. Default keeps threshold tuning separate from final evaluation.",
    ),
    out_errors: Path | None = typer.Option(None, "--out-errors"),
    out_json: Path | None = typer.Option(None, "--out-json"),
) -> None:
    """Evaluate Strands predictions against blinded human labels."""
    gold = pd.read_csv(gold_csv, dtype=str, keep_default_na=False)
    pred = pd.read_csv(predictions_csv, dtype=str, keep_default_na=False)
    pred_count = len(pred)
    pred = pred.drop_duplicates(subset="flag_record_id", keep="last")
    if dropped := pred_count - len(pred):
        typer.echo(f"Dropped {dropped} duplicate prediction rows")

    if "benchmark_record_id" not in gold.columns:
        raise typer.BadParameter("gold CSV is missing benchmark_record_id")
    if "gold_label" not in gold.columns:
        raise typer.BadParameter("gold CSV is missing gold_label")
    if "flag_record_id" not in pred.columns:
        raise typer.BadParameter("predictions CSV is missing flag_record_id")
    if "flag_label" not in pred.columns:
        raise typer.BadParameter("predictions CSV is missing flag_label")

    gold = gold.copy()
    pred = pred.copy()
    gold["gold_label_norm"] = gold["gold_label"].map(_norm_label)
    pred["flag_label_norm"] = pred["flag_label"].map(_norm_label)

    invalid_gold = sorted(
        set(gold.loc[gold["gold_label_norm"].ne(""), "gold_label_norm"]) - VALID_GOLD
    )
    if invalid_gold:
        raise typer.BadParameter(
            "invalid gold_label values: " + ", ".join(invalid_gold)
        )

    merged = gold.merge(
        pred,
        left_on="benchmark_record_id",
        right_on="flag_record_id",
        how="left",
        suffixes=("", "_pred"),
    )

    if manifest_csv is not None:
        manifest = pd.read_csv(manifest_csv, dtype=str, keep_default_na=False)
        if "benchmark_record_id" not in manifest.columns:
            raise typer.BadParameter("manifest CSV is missing benchmark_record_id")
        merged = merged.merge(
            manifest,
            on="benchmark_record_id",
            how="left",
            suffixes=("", "_manifest"),
        )

    merged = merged.loc[merged["gold_label_norm"].isin(VALID_GOLD)].copy()
    if merged.empty:
        raise typer.BadParameter("no labeled benchmark rows were found")

    missing_pred = merged["flag_label"].astype(str).str.strip().eq("")
    if missing_pred.any():
        typer.echo(f"WARNING: {int(missing_pred.sum())} labeled rows have no prediction")
        merged.loc[missing_pred, "flag_label_norm"] = "missing_prediction"

    if partition not in {"all", "test", "calibration"}:
        raise typer.BadParameter("--partition must be one of: all, test, calibration")
    if partition != "all":
        if "benchmark_partition" not in merged.columns:
            raise typer.BadParameter(
                "--partition requires --manifest with benchmark_partition"
            )
        merged = merged.loc[merged["benchmark_partition"] == partition].copy()
        if merged.empty:
            raise typer.BadParameter(f"no labeled rows found in partition {partition!r}")

    metrics = _metrics(merged)
    _print_metrics(f"Strands benchmark ({partition})", metrics)

    if "benchmark_source" in merged.columns:
        table = _subset_table(merged, "benchmark_source")
        if not table.empty:
            typer.echo("")
            typer.echo("By benchmark source")
            typer.echo(table.to_string(index=False))

    if "benchmark_stratum" in merged.columns:
        table = _subset_table(merged, "benchmark_stratum")
        if not table.empty:
            typer.echo("")
            typer.echo("By challenge stratum")
            typer.echo(table.to_string(index=False))

    aux_results: dict[str, Any] = {}
    aux_pairs = [
        ("gold_actual_use", "strands_p_actual_use"),
        ("gold_microbial_only", "strands_p_microbial_only"),
        ("gold_method_relevance", "strands_p_method_relevance"),
    ]
    for gold_col, pred_col in aux_pairs:
        aux = _binary_aux_metrics(merged, gold_col, pred_col)
        if aux is not None:
            aux_results[gold_col] = aux
            typer.echo("")
            typer.echo(f"Auxiliary: {gold_col}")
            for key, value in aux.items():
                if isinstance(value, float):
                    typer.echo(f"  {key:16s} {value:.4f}")
                else:
                    typer.echo(f"  {key:16s} {value}")

    error_type = pd.Series("", index=merged.index, dtype=str)
    gold_norm = merged["gold_label_norm"]
    pred_norm = merged["flag_label_norm"]
    error_type.loc[gold_norm.eq("in_scope") & pred_norm.eq("out_of_scope")] = "hard_false_negative"
    error_type.loc[gold_norm.eq("out_of_scope") & pred_norm.eq("in_scope")] = "false_positive"
    error_type.loc[
        gold_norm.isin({"in_scope", "out_of_scope"})
        & ~pred_norm.isin(AUTO_LABELS)
    ] = "manual_review"
    merged["benchmark_error_type"] = error_type

    if out_errors is not None:
        out_errors.parent.mkdir(parents=True, exist_ok=True)
        errors = merged.loc[merged["benchmark_error_type"].ne("")].copy()
        preferred = [
            "benchmark_id",
            "benchmark_record_id",
            "benchmark_error_type",
            "benchmark_source",
            "benchmark_stratum",
            "title",
            "doi",
            "gold_label",
            "flag_label",
            "strands_p_in_scope",
            "strands_p_out_of_scope",
            "strands_p_unsure",
            "strands_p_actual_use",
            "strands_p_microbial_only",
            "strands_p_method_relevance",
            "gold_note",
            "abstract",
        ]
        cols = [c for c in preferred if c in errors.columns]
        errors[cols].to_csv(out_errors, index=False)
        typer.echo(f"Error/review rows -> {out_errors}")

    if out_json is not None:
        out_json.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "partition": partition,
            "metrics": metrics,
            "auxiliary": aux_results,
        }
        out_json.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        typer.echo(f"Metrics JSON -> {out_json}")


if __name__ == "__main__":
    app()
