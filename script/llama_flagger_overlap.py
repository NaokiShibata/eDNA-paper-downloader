from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
import typer

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


def _try_import_plotting():
    try:
        import matplotlib.pyplot as plt
    except Exception:
        return None, None, None
    try:
        from matplotlib_venn import venn2, venn3
    except Exception:
        venn2 = None
        venn3 = None
    return plt, venn2, venn3


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


def _plot_venn(
    records: Dict[str, Dict[str, str]],
    models: List[str],
    label: str,
    venn_models: Optional[List[str]],
    out_path: Path,
    plt,
    venn2,
    venn3,
) -> None:
    selected = venn_models[:] if venn_models else models[:]
    selected = [m for m in selected if m in models]
    if len(selected) < 2:
        return
    if len(selected) > 3:
        selected = selected[:3]

    sets = []
    for model in selected:
        ids = {rid for rid, labels in records.items() if labels.get(model, "") == label}
        sets.append(ids)

    plt.figure(figsize=(4.5, 4.5))
    if len(selected) == 2:
        if venn2 is None:
            return
        venn2(sets, set_labels=selected)
    else:
        if venn3 is None:
            return
        venn3(sets, set_labels=selected)
    plt.title(f"Venn overlap for label: {label}")
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

    plt, venn2, venn3 = _try_import_plotting()
    if plt is None:
        typer.echo("WARNING: matplotlib not available; skipping plots.")
    else:
        plots_dir.mkdir(parents=True, exist_ok=True)
        _plot_agreement_status(agreement_counts, plots_dir / f"{plots_prefix}.agreement.png", plt)
        _plot_pairwise_heatmap(records, models, plots_dir / f"{plots_prefix}.pairwise.png", plt)
        _plot_label_distribution(records, models, plots_dir / f"{plots_prefix}.labels.png", plt)
        if len(models) >= 2:
            _plot_venn(
                records,
                models,
                venn_label,
                venn_models,
                plots_dir / f"{plots_prefix}.venn_{venn_label}.png",
                plt,
                venn2,
                venn3,
            )
        if (venn2 is None or venn3 is None) and len(models) >= 2:
            typer.echo("WARNING: matplotlib-venn not available; skipped Venn plot.")

    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        df_out = pd.DataFrame(out_rows)
        df_out.to_csv(out, index=False)
        typer.echo(f"Wrote overlap CSV: {out}")


if __name__ == "__main__":
    app()
