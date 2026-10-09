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
    ScoreCache,
    ScreeningConfig,
    build_state,
    check_health,
    default_config_path,
    format_status,
    load_config,
    output_column,
    output_row,
    prepare_abstract,
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


@app.command()
def flag(
    input_csv: Path = typer.Argument(..., exists=True, dir_okay=False),
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
    out_csv: Path | None = typer.Option(None, "--out-csv"),
    abstract_column: str = typer.Option("abstract", "--abstract-column"),
    limit: int | None = typer.Option(None, "--limit", min=1),
    dry_run: bool = typer.Option(False, "--dry-run"),
    log_file: Path = typer.Option(Path("logs/screen.log"), "--log-file"),
    log_level: str = typer.Option("INFO", "--log-level"),
) -> None:
    """Flag eDNA/eRNA papers in a CSV using a running Strands Decider server."""
    config = config or default_config_path()
    out_csv = out_csv or input_csv.with_name(f"{input_csv.stem}.strands.csv")
    logger = setup_logger("screen", log_level=log_level, log_file=log_file)
    logger.info("Strands config: %s", config)
    try:
        cfg = ScreeningConfig.from_sources(load_config(config))
    except (FileNotFoundError, ValueError) as exc:
        raise typer.BadParameter(f"invalid Strands config: {exc}") from exc
    df = pd.read_csv(input_csv, dtype=str, keep_default_na=False)
    if abstract_column not in df.columns:
        raise typer.BadParameter(
            f"abstract column {abstract_column!r} was not found; available columns: {', '.join(df.columns)}"
        )

    if limit is not None:
        df = df.iloc[:limit]

    if dry_run:
        for _, row in df.iterrows():
            meta = _row_to_meta(row)
            abstract = prepare_abstract(meta.get(abstract_column, ""), cfg.max_abstract_chars)
            if abstract:
                typer.echo(json.dumps({"state": build_state(meta, cfg, abstract_column), "questions": QUESTIONS}, indent=2, ensure_ascii=False))
                break
        return

    out_csv.parent.mkdir(parents=True, exist_ok=True)
    cache = ScoreCache(Path(cfg.cache_csv)) if cfg.cache_csv is not None else None
    fieldnames = list(dict.fromkeys(output_column(col) for col in list(df.columns) + EXTRA_COLUMNS))

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

    processed_count = 0
    error_count = 0

    with out_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
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
            out_row = meta | screen_row(session, meta, cfg, abstract_column, cache=cache)
            if out_row["flag_label"] == "process_error":
                error_count += 1
                logger.warning("process_error record=%s error=%s", out_row["flag_record_id"], out_row["flag_reason"])

            writer.writerow(output_row(out_row))
            handle.flush()
            processed_count += 1

            status.set_description_str(format_status(out_row, meta.get("title", "").strip()), refresh=True)

        status.clear()
        status.close()
        progress.refresh()

    if cache is not None:
        logger.info("Strands cache hits=%d misses=%d", cache.hits, cache.misses)

    typer.echo(
        f"Finished: processed={processed_count} errors={error_count} output={out_csv}"
    )


if __name__ == "__main__":
    app()
