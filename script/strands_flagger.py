from __future__ import annotations

import csv
import json
from pathlib import Path

import pandas as pd
import requests
import typer
from tqdm import tqdm

from libs.cli_logging import setup_logger
from libs.strands_screening import (
    EXTRA_COLUMNS,
    QUESTIONS,
    ScreeningConfig,
    build_state,
    check_health,
    coalesce,
    format_status,
    load_config,
    prepare_abstract,
    record_id,
    screen_row,
)

app = typer.Typer(add_completion=False)


def _row_to_meta(row: pd.Series) -> dict[str, str]:
    meta: dict[str, str] = {}
    for col in row.index:
        val = row[col]
        if pd.isna(val):
            continue
        meta[str(col)] = str(val)
    return meta


def _existing_processed_ids(out_csv: Path) -> set[str]:
    if not out_csv.exists() or out_csv.stat().st_size == 0:
        return set()
    try:
        existing = pd.read_csv(out_csv, dtype=str, keep_default_na=False)
    except Exception:
        return set()
    if "flag_record_id" not in existing.columns or "flag_label" not in existing.columns:
        return set()
    labels = existing["flag_label"].str.strip()
    mask = (
        existing["flag_record_id"].str.strip().ne("")
        & labels.ne("")
        & labels.ne("process_error")
    )
    return set(existing.loc[mask, "flag_record_id"].tolist())


@app.command()
def flag(
    input_csv: Path = typer.Argument(..., exists=True, dir_okay=False),
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
    out_csv: Path | None = typer.Option(None, "--out-csv"),
    base_url: str | None = typer.Option(None, "--base-url"),
    abstract_column: str | None = typer.Option(None, "--abstract-column"),
    timeout: float | None = typer.Option(None, "--timeout"),
    retries: int | None = typer.Option(None, "--retries"),
    include_threshold: float | None = typer.Option(None, "--include-threshold"),
    include_microbial_only_max: float | None = typer.Option(None, "--include-microbial-only-max"),
    actual_use_threshold: float | None = typer.Option(None, "--actual-use-threshold"),
    exclude_threshold: float | None = typer.Option(None, "--exclude-threshold"),
    exclude_actual_use_max: float | None = typer.Option(None, "--exclude-actual-use-max"),
    exclude_method_relevance_max: float | None = typer.Option(None, "--exclude-method-relevance-max"),
    max_abstract_chars: int | None = typer.Option(None, "--max-abstract-chars"),
    batch_size: int | None = typer.Option(None, "--batch-size"),
    batch_index: int | None = typer.Option(None, "--batch-index"),
    limit: int | None = typer.Option(None, "--limit"),
    resume: bool | None = typer.Option(None, "--resume/--no-resume"),
    dry_run: bool | None = typer.Option(None, "--dry-run"),
    log_file: Path | None = typer.Option(None, "--log-file"),
    log_level: str | None = typer.Option(None, "--log-level"),
) -> None:
    """Flag eDNA/eRNA papers in a CSV using a running Strands Decider server."""
    cfg_dict = load_config(config)

    out_csv = Path(coalesce(out_csv, cfg_dict, "out_csv", "results/strands_flagged.csv"))
    cfg = ScreeningConfig.from_sources(
        cfg_dict,
        base_url=base_url,
        timeout=timeout,
        retries=retries,
        include_threshold=include_threshold,
        include_microbial_only_max=include_microbial_only_max,
        actual_use_threshold=actual_use_threshold,
        exclude_threshold=exclude_threshold,
        exclude_actual_use_max=exclude_actual_use_max,
        exclude_method_relevance_max=exclude_method_relevance_max,
        max_abstract_chars=max_abstract_chars,
    )
    abstract_column = str(coalesce(abstract_column, cfg_dict, "abstract_column", "abstract"))
    batch_size = coalesce(batch_size, cfg_dict, "batch_size", None)
    batch_size = int(batch_size) if batch_size is not None else None
    batch_index = int(coalesce(batch_index, cfg_dict, "batch_index", 0))
    limit = coalesce(limit, cfg_dict, "limit", None)
    limit = int(limit) if limit is not None else None
    resume = bool(coalesce(resume, cfg_dict, "resume", True))
    dry_run = bool(coalesce(dry_run, cfg_dict, "dry_run", False))
    log_file_value = coalesce(log_file, cfg_dict, "log_file", "logs/strands_flagger.log")
    log_file = Path(log_file_value) if log_file_value else None
    log_level = str(coalesce(log_level, cfg_dict, "log_level", "INFO"))

    if batch_size is not None and limit is not None:
        raise typer.BadParameter("--batch-size and --limit cannot be used together")
    if batch_size is not None and batch_size <= 0:
        raise typer.BadParameter("--batch-size must be > 0")
    if batch_index < 0:
        raise typer.BadParameter("--batch-index must be >= 0")

    logger = setup_logger("strands_flagger", log_level=log_level, log_file=log_file)
    df = pd.read_csv(input_csv, dtype=str, keep_default_na=False)
    if abstract_column not in df.columns:
        raise typer.BadParameter(
            f"abstract column {abstract_column!r} was not found; available columns: {', '.join(df.columns)}"
        )

    if batch_size is not None:
        start = batch_index * batch_size
        df = df.iloc[start : start + batch_size]
    elif limit is not None:
        df = df.iloc[:limit]

    if df.empty:
        typer.echo("No rows to process.")
        return

    if dry_run:
        for _, row in df.iterrows():
            meta = _row_to_meta(row)
            abstract = prepare_abstract(meta.get(abstract_column, ""), cfg.max_abstract_chars)
            if abstract:
                typer.echo(json.dumps({"state": build_state(meta, cfg, abstract_column), "questions": QUESTIONS}, indent=2, ensure_ascii=False))
                break
        return

    out_csv.parent.mkdir(parents=True, exist_ok=True)
    processed = _existing_processed_ids(out_csv) if resume else set()
    fieldnames = list(df.columns) + [col for col in EXTRA_COLUMNS if col not in df.columns]

    session = requests.Session()
    try:
        health_data = check_health(session, cfg.base_url, cfg.timeout)
        logger.info(
            "server status=%s model=%s device=%s max_length=%s",
            health_data.get("status"),
            health_data.get("model"),
            health_data.get("device"),
            health_data.get("max_length"),
        )
    except Exception as exc:
        raise typer.BadParameter(f"could not connect to Strands Decider at {cfg.base_url}: {exc}") from exc

    write_header = not out_csv.exists() or out_csv.stat().st_size == 0
    processed_count = 0
    skipped_count = 0
    error_count = 0

    with out_csv.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        if write_header:
            writer.writeheader()

        progress = tqdm(
            df.iterrows(),
            total=len(df),
            desc="Strands screening",
            position=0,
            dynamic_ncols=True,
        )
        status = tqdm(
            total=0,
            position=1,
            bar_format="{desc}",
            leave=False,
        )

        for _, row in progress:
            meta = _row_to_meta(row)
            rec_id = record_id(meta)
            if resume and rec_id in processed:
                skipped_count += 1
                continue

            out_row = meta | screen_row(session, meta, cfg, abstract_column)
            if out_row["flag_label"] == "process_error":
                error_count += 1
                logger.warning("process_error record=%s error=%s", rec_id, out_row["flag_reason"])

            writer.writerow(out_row)
            handle.flush()
            processed_count += 1

            status.set_description_str(format_status(out_row, meta.get("title", "").strip()), refresh=True)

        status.clear()
        status.close()
        progress.refresh()

    typer.echo(
        f"Finished: processed={processed_count} skipped={skipped_count} errors={error_count} output={out_csv}"
    )


if __name__ == "__main__":
    app()
