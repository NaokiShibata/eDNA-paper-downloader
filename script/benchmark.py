from __future__ import annotations

import hashlib
import itertools
import json
import random
import re
from pathlib import Path
from typing import Any

import pandas as pd
import typer

from libs.strands_screening import (
    ScreeningConfig,
    decide_label,
    default_config_path,
    load_config,
    record_id,
)

app = typer.Typer(add_completion=False)

GOLD_COLUMNS = [
    "gold_label",
    "gold_actual_use",
    "gold_microbial_only",
    "gold_method_relevance",
    "gold_note",
]

STRATUM_ORDER = [
    "environmental_nonexplicit",
    "microbial_profile",
    "host_tissue_genomics",
    "wastewater",
    "methodological",
    "review_perspective",
    "explicit_edna",
    "other",
]


def _contains(pattern: str, text: str) -> bool:
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def _classify_row(row: pd.Series, abstract_column: str) -> tuple[str, str, int]:
    title = str(row.get("title", "") or "")
    abstract = str(row.get(abstract_column, "") or "")
    text = f"{title}\n{abstract}".lower()

    explicit = _contains(
        r"\b(?:environmental\s+(?:dna|rna)|e[- ]?dna|e[- ]?rna)\b",
        text,
    )
    microbial = _contains(
        r"microbiom|microbiota|metagenom|metatranscript|\b16s\b|\b18s\b|\bits\b|"
        r"bacteri(?:a|al)|archaea|mycobiom|virom|resistom|fungal community|microbial community",
        text,
    )
    host_tissue = _contains(
        r"\btissue\b|\bblood\b|\bserum\b|\bplasma\b|\bbiopsy\b|"
        r"host-associated|gut microbi|skin microbi|oral microbi|rumen microbi|"
        r"whole[- ]genome|genome assembly|transcriptom",
        text,
    )
    wastewater = _contains(r"wastewater|sewage|sewer|waste water", text)
    environmental = _contains(
        r"\bwater\b|seawater|freshwater|river|lake|stream|pond|ocean|marine|estuar|"
        r"sediment|\bsoil\b|\bair\b|airborne|dust|snow|ice|wetland|groundwater|biofilm|"
        r"wastewater|sewage|sewer",
        text,
    )
    methodological = _contains(
        r"sampl(?:e|ing|er)|filter|filtration|preserv|extract|primer|assay|\bqpcr\b|"
        r"\bddpcr\b|\blamp\b|metabarcod|amplicon|bioinformatic|occupancy|transport|"
        r"degradation|persistence|quantif|validation|benchmark|workflow",
        text,
    )
    review = _contains(
        r"systematic review|meta-analysis|\breview\b|perspective|overview|"
        r"state of the art|future directions",
        text,
    )

    tags: list[str] = []
    if explicit:
        tags.append("explicit_edna")
    if environmental:
        tags.append("environmental_matrix")
    if microbial:
        tags.append("microbial")
    if host_tissue:
        tags.append("host_tissue")
    if wastewater:
        tags.append("wastewater")
    if methodological:
        tags.append("methodological")
    if review:
        tags.append("review_perspective")

    if environmental and not explicit:
        stratum = "environmental_nonexplicit"
    elif microbial:
        stratum = "microbial_profile"
    elif host_tissue:
        stratum = "host_tissue_genomics"
    elif wastewater:
        stratum = "wastewater"
    elif methodological:
        stratum = "methodological"
    elif review:
        stratum = "review_perspective"
    elif explicit:
        stratum = "explicit_edna"
    else:
        stratum = "other"

    score = 0
    if environmental and not explicit:
        score += 4
    if microbial:
        score += 3
    if host_tissue:
        score += 3
    if wastewater:
        score += 2
    if methodological:
        score += 2
    if review:
        score += 2
    if explicit and (microbial or review or host_tissue):
        score += 2
    if len(abstract) < 500:
        score += 1
    if len(abstract) > 2500:
        score += 1

    return stratum, ";".join(tags), score


def _partition(record_id: str, seed: int, calibration_fraction: float) -> str:
    digest = hashlib.sha256(f"{seed}|{record_id}".encode()).digest()
    value = int.from_bytes(digest[:8], "big") / float(2**64)
    return "calibration" if value < calibration_fraction else "test"


def _round_robin_challenge(
    pool: pd.DataFrame,
    n: int,
    seed: int,
) -> pd.DataFrame:
    if n <= 0 or pool.empty:
        return pool.iloc[0:0].copy()

    rng = random.Random(seed)
    buckets: dict[str, list[int]] = {}
    for stratum in STRATUM_ORDER:
        group = pool.loc[pool["_benchmark_stratum"] == stratum].copy()
        if group.empty:
            continue
        # High challenge score first, random tie-breaker.
        keyed = [
            (-int(row["_challenge_score"]), rng.random(), int(idx))
            for idx, row in group.iterrows()
        ]
        keyed.sort()
        buckets[stratum] = [idx for _, _, idx in keyed]

    selected: list[int] = []
    while len(selected) < n and any(buckets.values()):
        progressed = False
        for stratum in STRATUM_ORDER:
            values = buckets.get(stratum, [])
            if values and len(selected) < n:
                selected.append(values.pop(0))
                progressed = True
        if not progressed:
            break

    if len(selected) < n:
        remaining = [int(i) for i in pool.index if int(i) not in set(selected)]
        rng.shuffle(remaining)
        selected.extend(remaining[: n - len(selected)])

    return pool.loc[selected].copy()


@app.command()
def make(
    input_csv: Path = typer.Argument(..., exists=True, dir_okay=False),
    out_prefix: Path = typer.Option(
        Path("benchmark/strands_edna_200"),
        "--out-prefix",
        help="Writes <prefix>.review.csv and <prefix>.manifest.csv.",
    ),
    abstract_column: str = typer.Option("abstract", "--abstract-column"),
    n_total: int = typer.Option(200, "--n-total", min=20),
    n_natural: int = typer.Option(120, "--n-natural", min=1),
    calibration_fraction: float = typer.Option(0.40, "--calibration-fraction", min=0.1, max=0.9),
    seed: int = typer.Option(3407, "--seed"),
) -> None:
    """Create a blinded, stratified benchmark set from a literature CSV."""
    if n_natural >= n_total:
        raise typer.BadParameter("--n-natural must be smaller than --n-total")

    df = pd.read_csv(input_csv, dtype=str, keep_default_na=False)
    if abstract_column not in df.columns:
        raise typer.BadParameter(
            f"abstract column {abstract_column!r} was not found; "
            f"available columns: {', '.join(df.columns)}"
        )

    df = df.copy()
    record_ids: list[str] = []
    for _, row in df.iterrows():
        meta = {k: "" if pd.isna(v) else str(v) for k, v in row.items()}
        rid = record_id(meta)
        record_ids.append(f"row:{row.name}" if rid.startswith("{") else rid)
    df["benchmark_record_id"] = record_ids
    df = df.loc[df[abstract_column].str.strip().ne("")].copy()
    df = df.drop_duplicates(subset=["benchmark_record_id"], keep="first").reset_index(drop=True)

    if len(df) < n_total:
        raise typer.BadParameter(
            f"only {len(df)} unique rows with abstracts are available; "
            f"reduce --n-total from {n_total}"
        )

    classified = df.apply(
        lambda row: _classify_row(row, abstract_column),
        axis=1,
        result_type="expand",
    )
    classified.columns = ["_benchmark_stratum", "_benchmark_tags", "_challenge_score"]
    df = pd.concat([df, classified], axis=1)

    natural = df.sample(n=n_natural, random_state=seed).copy()
    natural["_benchmark_source"] = "natural"

    challenge_n = n_total - n_natural
    remaining = df.drop(index=natural.index)
    challenge = _round_robin_challenge(remaining, challenge_n, seed + 1)
    challenge["_benchmark_source"] = "challenge"

    benchmark = pd.concat([natural, challenge], axis=0).copy()
    benchmark["_sort_key"] = benchmark["benchmark_record_id"].map(
        lambda rid: hashlib.sha256(f"order|{seed}|{rid}".encode()).hexdigest()
    )
    benchmark = benchmark.sort_values("_sort_key").drop(columns=["_sort_key"]).reset_index(drop=True)

    benchmark["benchmark_id"] = [f"B{i:04d}" for i in range(1, len(benchmark) + 1)]
    benchmark["_benchmark_partition"] = benchmark["benchmark_record_id"].map(
        lambda rid: _partition(rid, seed, calibration_fraction)
    )

    for col in GOLD_COLUMNS:
        benchmark[col] = ""

    # Reviewer file deliberately excludes sampling strata and partition to reduce labeling bias.
    helper_cols = {
        "_benchmark_stratum",
        "_benchmark_tags",
        "_challenge_score",
        "_benchmark_source",
        "_benchmark_partition",
    }
    original_cols = [c for c in df.columns if c not in helper_cols and c != "benchmark_record_id"]
    review_cols = ["benchmark_id", "benchmark_record_id"] + original_cols + GOLD_COLUMNS
    review = benchmark[review_cols].copy()

    manifest = benchmark[
        [
            "benchmark_id",
            "benchmark_record_id",
            "_benchmark_source",
            "_benchmark_stratum",
            "_benchmark_tags",
            "_benchmark_partition",
            "_challenge_score",
        ]
    ].rename(
        columns={
            "_benchmark_source": "benchmark_source",
            "_benchmark_stratum": "benchmark_stratum",
            "_benchmark_tags": "benchmark_tags",
            "_benchmark_partition": "benchmark_partition",
            "_challenge_score": "challenge_score",
        }
    )

    out_prefix.parent.mkdir(parents=True, exist_ok=True)
    review_path = Path(f"{out_prefix}.review.csv")
    manifest_path = Path(f"{out_prefix}.manifest.csv")
    review.to_csv(review_path, index=False)
    manifest.to_csv(manifest_path, index=False)

    typer.echo(f"Input unique rows : {len(df)}")
    typer.echo(f"Benchmark rows    : {len(benchmark)}")
    typer.echo(f"  natural         : {(manifest['benchmark_source'] == 'natural').sum()}")
    typer.echo(f"  challenge       : {(manifest['benchmark_source'] == 'challenge').sum()}")
    typer.echo(f"  calibration     : {(manifest['benchmark_partition'] == 'calibration').sum()}")
    typer.echo(f"  test            : {(manifest['benchmark_partition'] == 'test').sum()}")
    typer.echo("Challenge strata:")
    for name, count in manifest.loc[
        manifest["benchmark_source"] == "challenge", "benchmark_stratum"
    ].value_counts().items():
        typer.echo(f"  {name:24s} {count}")
    typer.echo(f"Reviewer CSV      : {review_path}")
    typer.echo(f"Manifest CSV      : {manifest_path}")


def _binary_metrics(gold_b: pd.Series, pred_b: pd.Series) -> dict[str, float | int]:
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
    n = int(len(gold_b))
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

    return metrics


VALID_GOLD = {"in_scope", "out_of_scope", "unsure"}
AUTO_LABELS = {"in_scope", "out_of_scope"}


def _safe_div(num: float, den: float) -> float:
    return float(num / den) if den else 0.0


def _norm_label(value: Any) -> str:
    text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return text


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

    metrics = _binary_metrics(gold_b, pred_b)

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


@app.command("eval")
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
            "strands_p_review",
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


@app.command()
def tune(
    gold_csv: Path = typer.Argument(..., exists=True, dir_okay=False),
    predictions_csv: Path = typer.Argument(..., exists=True, dir_okay=False),
    manifest_csv: Path = typer.Option(..., "--manifest", exists=True, dir_okay=False),
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
    max_hard_fn: int = typer.Option(0, "--max-hard-fn", min=0),
    max_results: int = typer.Option(20, "--max-results", min=1),
    out_json: Path | None = typer.Option(None, "--out-json"),
) -> None:
    """Tune exclusion thresholds on the calibration partition only.

    Inclusion thresholds stay fixed. Candidates are ranked by the number of correctly
    auto-excluded gold-negative papers while respecting the hard false-negative limit.
    """
    cfg = ScreeningConfig.from_sources(load_config(config or default_config_path()))
    include_threshold = cfg.include_threshold
    include_microbial_only_max = cfg.include_microbial_only_max
    actual_use_threshold = cfg.actual_use_threshold
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
        "strands_p_review",
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
        "strands_p_review",
    ]:
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame = frame.dropna(
        subset=[
            "strands_p_in_scope",
            "strands_p_out_of_scope",
            "strands_p_actual_use",
            "strands_p_microbial_only",
            "strands_p_method_relevance",
            "strands_p_review",
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
                float(row["strands_p_review"]),
                **thresholds,
            ),
            axis=1,
            include_threshold=include_threshold,
            include_microbial_only_max=include_microbial_only_max,
            actual_use_threshold=actual_use_threshold,
            exclude_threshold=ex,
            exclude_actual_use_max=au,
            exclude_method_relevance_max=mr,
            exclude_review_min=cfg.exclude_review_min,
            exclude_review_out_min=cfg.exclude_review_out_min,
        )
        counts = _binary_metrics(frame["gold_label"].astype(str).str.strip().str.lower(), labels)
        m = {
            new: counts[old]
            for new, old in {
                "n": "n_binary_gold", "gold_pos": "gold_in_scope",
                "gold_neg": "gold_out_of_scope", "tp": "tp", "tn": "tn",
                "hard_fn": "hard_false_negative", "fp": "false_positive",
                "pos_review": "positive_sent_to_review",
                "neg_review": "negative_sent_to_review",
                "auto_coverage": "auto_coverage", "hard_fn_rate": "hard_false_negative_rate",
                "auto_accuracy": "auto_accuracy",
            }.items()
        }
        m["negative_auto_exclusion"] = _safe_div(m["tn"], m["gold_neg"])
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
