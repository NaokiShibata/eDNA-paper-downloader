from __future__ import annotations

import re
from itertools import combinations
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
import typer
from matplotlib_venn import venn2, venn3

app = typer.Typer(add_completion=False)


def _clean_doi(value: str) -> str:
    v = (value or "").strip().lower()
    v = re.sub(r"^https?://(dx\.)?doi\.org/", "", v)
    v = re.sub(r"^doi:\s*", "", v)
    return v.strip()


def _get_str(value: object) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    return str(value).strip()


def _record_id_from_row(row: pd.Series) -> Optional[str]:
    doi = _clean_doi(_get_str(row.get("doi", "")))
    if doi:
        return f"doi:{doi}"
    title = _get_str(row.get("title", "")).lower()
    year = _get_str(row.get("year", ""))
    if title or year:
        return f"title:{title}|year:{year}"
    return None


def _pick_model_name(df: pd.DataFrame, path: Path) -> str:
    if "flag_model_path" in df.columns:
        values = [v for v in df["flag_model_path"].dropna().unique().tolist() if str(v).strip()]
        if len(values) == 1:
            return str(values[0])
    return path.stem


def _sanitize_column_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_")
    return cleaned or "model"


def _label_counts_str(counter: Counter[str]) -> str:
    parts = [f"{label}:{count}" for label, count in counter.most_common()]
    return "|".join(parts)


def _plot_agreement_status(
    agreement_counts: Counter[str],
    out_path: Path,
    plt,
) -> None:
    order = ["all_agree", "majority_agree", "split", "all_disagree", "no_labels"]
    labels = [k for k in order if k in agreement_counts]
    counts = [agreement_counts.get(k, 0) for k in labels]
    if not labels:
        return
    plt.figure(figsize=(6, 4))
    plt.bar(labels, counts, color="#2b7bba")
    plt.title("Agreement Status")
    plt.ylabel("Records")
    plt.xticks(rotation=30, ha="right")
    plt.tight_layout()
    plt.savefig(out_path, dpi=150)
    plt.close()


def _plot_pairwise_heatmap(
    records: Dict[str, Dict[str, str]],
    models: List[str],
    out_path: Path,
    plt,
) -> None:
    n = len(models)
    if n < 2:
        return
    matrix = [[0.0 for _ in range(n)] for _ in range(n)]
    totals = [[0 for _ in range(n)] for _ in range(n)]
    for i, m1 in enumerate(models):
        for j, m2 in enumerate(models):
            if i == j:
                matrix[i][j] = 1.0
                totals[i][j] = 0
                continue
            total = 0
            agree = 0
            for labels in records.values():
                l1 = labels.get(m1, "")
                l2 = labels.get(m2, "")
                if not l1 or not l2:
                    continue
                total += 1
                if l1 == l2:
                    agree += 1
            totals[i][j] = total
            matrix[i][j] = (agree / total) if total else 0.0

    plt.figure(figsize=(0.9 * n + 2, 0.9 * n + 2))
    im = plt.imshow(matrix, vmin=0.0, vmax=1.0, cmap="Blues")
    plt.colorbar(im, fraction=0.046, pad=0.04)
    plt.xticks(range(n), models, rotation=45, ha="right")
    plt.yticks(range(n), models)
    for i in range(n):
        for j in range(n):
            total = totals[i][j]
            label = f"{matrix[i][j]:.2f}"
            if total:
                label = f"{label}\n{total}"
            plt.text(j, i, label, ha="center", va="center", fontsize=8)
    plt.title("Pairwise Agreement Ratio (count shown)")
    plt.tight_layout()
    plt.savefig(out_path, dpi=150)
    plt.close()


def _plot_label_distribution(
    records: Dict[str, Dict[str, str]],
    models: List[str],
    out_path: Path,
    plt,
) -> None:
    if not models:
        return
    label_counts: Dict[str, Counter[str]] = {m: Counter() for m in models}
    all_labels: set[str] = set()
    for labels in records.values():
        for m in models:
            label = labels.get(m, "")
            if label:
                label_counts[m][label] += 1
                all_labels.add(label)

    preferred = ["in_scope", "out_of_scope", "unsure", "parse_error", "process_error"]
    ordered_labels = [l for l in preferred if l in all_labels]
    ordered_labels += sorted([l for l in all_labels if l not in ordered_labels])
    if not ordered_labels:
        return

    x = list(range(len(models)))
    bottoms = [0] * len(models)
    plt.figure(figsize=(max(6, len(models) * 1.2), 4.5))
    for label in ordered_labels:
        values = [label_counts[m].get(label, 0) for m in models]
        plt.bar(x, values, bottom=bottoms, label=label)
        bottoms = [bottoms[i] + values[i] for i in range(len(models))]
    plt.xticks(x, models, rotation=45, ha="right")
    plt.ylabel("Records")
    plt.title("Label Distribution by Model")
    plt.legend(fontsize=8, ncol=2)
    plt.tight_layout()
    plt.savefig(out_path, dpi=150)
    plt.close()


def _short_model_label(value: str, max_len: int = 24) -> str:
    try:
        name = Path(str(value)).name
    except Exception:
        name = str(value)
    stem = Path(name).stem if name else str(value)
    # Drop common quant suffixes for compact labels.
    stem = re.sub(r"-(Q\d+[_A-Za-z0-9]+)$", "", stem)
    stem = re.sub(r"-(Q\d+)$", "", stem)
    label = stem or name or str(value)
    if len(label) > max_len:
        label = label[: max_len - 3] + "..."
    return label


def _model_aliases(value: str) -> List[str]:
    raw = str(value)
    try:
        name = Path(raw).name
    except Exception:
        name = raw
    stem = Path(name).stem if name else raw
    cleaned = _sanitize_column_name(raw).lower()
    aliases = [
        raw,
        raw.lower(),
        name,
        name.lower(),
        stem,
        stem.lower(),
        cleaned,
    ]
    seen = set()
    out = []
    for a in aliases:
        if not a:
            continue
        key = a.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def _resolve_venn_models(models: List[str], requested: Optional[List[str]]) -> tuple[List[str], List[str]]:
    if not requested:
        return models[:], []
    alias_map = {m: set(_model_aliases(m)) for m in models}
    selected: List[str] = []
    unmatched: List[str] = []
    for req in requested:
        req_key = str(req).strip()
        if not req_key:
            continue
        req_norm = req_key.lower()
        match = None
        for model, aliases in alias_map.items():
            if model in selected:
                continue
            if req_key in aliases or req_norm in aliases:
                match = model
                break
        if match:
            selected.append(match)
        else:
            unmatched.append(req_key)
    return selected, unmatched


def _build_venn_groups(
    models: List[str],
    requested: Optional[List[str]],
) -> tuple[List[List[str]], List[str], bool]:
    selected, unmatched = _resolve_venn_models(models, requested)
    if len(selected) < 2:
        return [], unmatched, True
    if len(selected) <= 3:
        return [selected], unmatched, False
    # More than 3 models: generate all 3-way combinations.
    groups = [list(g) for g in combinations(selected, 3)]
    return groups, unmatched, False


def _plot_venn(
    records: Dict[str, Dict[str, str]],
    models: List[str],
    label: str,
    venn_models: List[str],
    display_labels: List[str],
    out_path: Path,
    plt,
) -> None:
    selected = [m for m in venn_models if m in models]
    if len(selected) < 2:
        plt.figure(figsize=(4.5, 4.0))
        plt.text(0.5, 0.5, "Venn plot skipped\n(need 2-3 models)", ha="center", va="center")
        plt.axis("off")
        plt.tight_layout()
        plt.savefig(out_path, dpi=150)
        plt.close()
        return
    if len(selected) > 3:
        selected = selected[:3]

    sets = []
    for model in selected:
        ids = {rid for rid, labels in records.items() if labels.get(model, "") == label}
        sets.append(ids)

    # Degenerate case: all selected sets are identical.
    if sets and all(s == sets[0] for s in sets):
        plt.figure(figsize=(4.5, 4.0))
        plt.text(
            0.5,
            0.55,
            "All selected models\nhave identical membership",
            ha="center",
            va="center",
        )
        plt.text(0.5, 0.35, f"count = {len(sets[0])}", ha="center", va="center")
        plt.axis("off")
        plt.tight_layout()
        plt.savefig(out_path, dpi=150)
        plt.close()
        return

    plt.figure(figsize=(4.8, 4.8))
    if len(selected) == 2:
        v = venn2(sets, set_labels=display_labels[:2])
    else:
        v = venn3(sets, set_labels=display_labels[:3])
    if v is not None:
        for t in (v.set_labels or []):
            if t:
                t.set_fontsize(8)
        for t in (v.subset_labels or []):
            if t:
                t.set_fontsize(8)
    if sum(len(s) for s in sets) == 0:
        plt.text(0.5, 0.5, "No records for this label", ha="center", va="center")
    plt.title(f"Venn overlap for label: {label}", fontsize=10)
    plt.tight_layout()
    plt.savefig(out_path, dpi=150)
    plt.close()


@app.command()
def main(
    inputs: List[Path] = typer.Argument(..., exists=True, dir_okay=False),
    out: Optional[Path] = typer.Option(None, help="Write per-record overlap CSV."),
    id_column: str = typer.Option("flag_record_id", help="Column used as record id."),
    label_column: str = typer.Option("flag_label", help="Column used as label."),
    plots_dir: Path = typer.Option(Path("."), help="Write plots to this directory."),
    plots_prefix: str = typer.Option("flag_overlap", help="Prefix for plot filenames."),
    venn_label: str = typer.Option("in_scope", help="Label to visualize in the Venn diagram."),
    venn_models: Optional[List[str]] = typer.Option(
        None,
        help="Model names to include in Venn (2 or 3). Defaults to first models.",
    ),
):
    """
    Summarize label overlap across multiple llama_flagger CSV outputs.
    """
    records: Dict[str, Dict[str, str]] = {}
    meta: Dict[str, Dict[str, str]] = {}
    models: List[str] = []
    col_names: Dict[str, str] = {}

    for path in inputs:
        try:
            df = pd.read_csv(path)
        except Exception as e:
            empty_err = getattr(getattr(pd, "errors", None), "EmptyDataError", None)
            if empty_err is not None and isinstance(e, empty_err):
                typer.echo(f"WARNING: empty CSV skipped: {path}")
                continue
            raise

        if df.empty:
            typer.echo(f"WARNING: empty CSV skipped: {path}")
            continue

        model_name = _pick_model_name(df, path)
        if model_name in models:
            model_name = f"{model_name}_{len(models)+1}"
        models.append(model_name)
        col_names[model_name] = f"label_{_sanitize_column_name(model_name)}"

        for _, row in df.iterrows():
            rid = _get_str(row.get(id_column, ""))
            if not rid:
                rid = _record_id_from_row(row) or ""
            if not rid:
                continue

            label = _get_str(row.get(label_column, ""))
            records.setdefault(rid, {})[model_name] = label

            if rid not in meta:
                meta[rid] = {}
            for key in ("doi", "title", "year"):
                if not meta[rid].get(key):
                    meta_value = _get_str(row.get(key, ""))
                    if meta_value:
                        meta[rid][key] = meta_value

    if not records:
        typer.echo("No records found in inputs.")
        raise typer.Exit(code=1)

    out_rows: List[Dict[str, object]] = []
    coverage_counts: Counter[int] = Counter()
    agreement_counts: Counter[str] = Counter()

    for rid, labels in records.items():
        row: Dict[str, object] = {"record_id": rid}
        row.update(meta.get(rid, {}))

        label_values: List[str] = []
        for model_name in models:
            label = labels.get(model_name, "")
            row[col_names[model_name]] = label
            if label:
                label_values.append(label)

        coverage = len(label_values)
        coverage_counts[coverage] += 1

        counts = Counter(label_values)
        if counts:
            majority_label, agree_count = counts.most_common(1)[0]
            agree_ratio = round(agree_count / max(1, coverage), 3)
            unique_labels = len(counts)
        else:
            majority_label = ""
            agree_count = 0
            agree_ratio = 0.0
            unique_labels = 0

        if coverage == 0:
            status = "no_labels"
        elif agree_count == coverage:
            status = "all_agree"
        elif agree_count >= (coverage // 2 + 1):
            status = "majority_agree"
        elif unique_labels == coverage:
            status = "all_disagree"
        else:
            status = "split"

        agreement_counts[status] += 1

        row.update(
            {
                "coverage": coverage,
                "agree_count": agree_count,
                "agree_ratio": agree_ratio,
                "majority_label": majority_label,
                "label_counts": _label_counts_str(counts),
                "agreement_status": status,
            }
        )
        out_rows.append(row)

    typer.echo(f"Inputs: {len(inputs)}")
    typer.echo(f"Models: {', '.join(models)}")
    typer.echo(f"Records: {len(records)}")
    typer.echo("Coverage counts:")
    for cov in sorted(coverage_counts.keys()):
        typer.echo(f"  {cov} models: {coverage_counts[cov]}")
    typer.echo("Agreement status:")
    for status, count in agreement_counts.most_common():
        typer.echo(f"  {status}: {count}")

    if len(models) > 1:
        typer.echo("Pairwise agreement:")
        for i in range(len(models)):
            for j in range(i + 1, len(models)):
                m1 = models[i]
                m2 = models[j]
                total = 0
                agree = 0
                for rid, labels in records.items():
                    l1 = labels.get(m1, "")
                    l2 = labels.get(m2, "")
                    if not l1 or not l2:
                        continue
                    total += 1
                    if l1 == l2:
                        agree += 1
                ratio = round(agree / total, 3) if total else 0.0
                typer.echo(f"  {m1} vs {m2}: {agree}/{total} ({ratio})")

    plots_dir.mkdir(parents=True, exist_ok=True)
    _plot_agreement_status(agreement_counts, plots_dir / f"{plots_prefix}.agreement.png", plt)
    _plot_pairwise_heatmap(records, models, plots_dir / f"{plots_prefix}.pairwise.png", plt)
    _plot_label_distribution(records, models, plots_dir / f"{plots_prefix}.labels.png", plt)
    if len(models) >= 2:
        groups, unmatched, invalid = _build_venn_groups(models, venn_models)
        if unmatched:
            typer.echo(f"WARNING: Venn models not matched and ignored: {', '.join(unmatched)}")
        if invalid:
            typer.echo("WARNING: Venn plot skipped (need at least 2 matched models).")
        elif len(groups) == 1:
            group = groups[0]
            _plot_venn(
                records,
                models,
                venn_label,
                group,
                [_short_model_label(m) for m in group],
                plots_dir / f"{plots_prefix}.venn_{venn_label}.png",
                plt,
            )
        else:
            typer.echo(
                f"Generating Venn plots for all 3-way combinations: {len(groups)} plots."
            )
            for idx, group in enumerate(groups, start=1):
                _plot_venn(
                    records,
                    models,
                    venn_label,
                    group,
                    [_short_model_label(m) for m in group],
                    plots_dir / f"{plots_prefix}.venn_{venn_label}.{idx}.png",
                    plt,
                )

    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        df_out = pd.DataFrame(out_rows)
        df_out.to_csv(out, index=False)
        typer.echo(f"Wrote overlap CSV: {out}")


if __name__ == "__main__":
    app()
