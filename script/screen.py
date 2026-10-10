from __future__ import annotations

import csv
import json
import logging
import shlex
import sys
import tempfile
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import pandas as pd
import requests
import typer
from tqdm import tqdm

from libs.cli_logging import log_run_header, setup_logger
from libs.screen_servers import MODEL_CHOICES, OMNI_MODEL, STRANDS_MODEL, gpu_progress, screening_servers
from libs.strands_screening import (
    EXTRA_COLUMNS,
    PROMPT_VERSION,
    QUESTIONS,
    ScoreCache,
    ScreeningConfig,
    build_state,
    check_health,
    default_config_path,
    first_stage_details,
    format_status,
    load_config,
    output_column,
    output_row,
    prepare_abstract,
    screen_row,
)

app = typer.Typer(add_completion=False)


def _select_models(cfg: ScreeningConfig, model1: str, model2: str | None = None) -> ScreeningConfig:
    for name in (model1, model2):
        if name is not None and name not in MODEL_CHOICES:
            raise typer.BadParameter(f"unknown model {name!r}; choose from {', '.join(MODEL_CHOICES)}")
    if model1 == model2:
        raise typer.BadParameter("--model1 and --model2 must select different models")

    def endpoint(name: str) -> tuple[str, str, bool]:
        model, url, batch = MODEL_CHOICES[name]
        if cfg.expected_model == model or (model == STRANDS_MODEL and cfg.expected_model is None):
            url = cfg.base_url
            batch = cfg.batch_questions if model == STRANDS_MODEL else batch
        elif cfg.first_stage_model == model and cfg.first_stage_base_url is not None:
            url = cfg.first_stage_base_url
            batch = cfg.first_stage_batch_questions if model == STRANDS_MODEL else batch
        return model, url, batch

    primary, primary_url, primary_batch = endpoint(model1)
    if model2 is None:
        return replace(cfg, base_url=primary_url, expected_model=primary, batch_questions=primary_batch,
                       first_stage_base_url=None, first_stage_model=None)
    secondary, secondary_url, secondary_batch = endpoint(model2)
    return replace(cfg, base_url=secondary_url, expected_model=secondary, batch_questions=secondary_batch,
                   first_stage_base_url=primary_url, first_stage_model=primary,
                   first_stage_batch_questions=primary_batch)


def _select_mode(cfg: ScreeningConfig, mode: str | None) -> ScreeningConfig:
    if mode is None:
        return cfg
    strands_url = cfg.first_stage_base_url or (
        cfg.base_url if cfg.expected_model in (None, STRANDS_MODEL) else "http://127.0.0.1:8012"
    )
    strands_batch = cfg.first_stage_batch_questions if cfg.first_stage_base_url else (
        cfg.batch_questions if cfg.expected_model in (None, STRANDS_MODEL) else False
    )
    if mode == "strands":
        return replace(cfg, base_url=strands_url, expected_model=STRANDS_MODEL, batch_questions=strands_batch,
                       first_stage_base_url=None, first_stage_model=None)
    clef_url = cfg.base_url if cfg.expected_model == "clef-27b-q8" else "http://127.0.0.1:8014"
    return replace(cfg, base_url=clef_url, expected_model="clef-27b-q8", batch_questions=True,
                   first_stage_base_url=strands_url if mode == "both" else None,
                   first_stage_model=STRANDS_MODEL if mode == "both" else None,
                   first_stage_batch_questions=strands_batch)


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
    strands: bool = typer.Option(False, "--strands", "--strandes", help="Use Strands only."),
    both: bool = typer.Option(False, "--both", help="Use Strands, then Clef 27B for unsure results."),
    clef: bool = typer.Option(False, "--clef", help="Use Clef 27B only."),
    model1: str | None = typer.Option(None, "--model1", help="Initial model: strands, clef, clef_flash, clef_omni."),
    model2: str | None = typer.Option(None, "--model2", help="Recheck unsure results with this model; requires --model1."),
    auto_server: bool = typer.Option(True, "--auto-server/--no-auto-server", help="Start missing local model servers and stop owned servers on exit."),
    server_startup_timeout: float = typer.Option(180, "--server-startup-timeout", min=1, help="Maximum seconds to wait for each automatically started server."),
    log_file: Path = typer.Option(Path("logs/screen.log"), "--log-file"),
    log_level: str = typer.Option("INFO", "--log-level"),
) -> None:
    """Flag eDNA/eRNA papers, reusing or automatically starting model servers."""
    if sum((strands, both, clef)) > 1:
        raise typer.BadParameter("choose only one of --strands/--strandes, --both, --clef")
    if (model1 is not None or model2 is not None) and any((strands, both, clef)):
        raise typer.BadParameter("do not combine --model1/--model2 with --strands, --both or --clef")
    if model2 is not None and model1 is None:
        raise typer.BadParameter("--model2 requires --model1")
    config = config or default_config_path()
    out_csv = out_csv or input_csv.with_name(f"{input_csv.stem}.strands.csv")
    logger = setup_logger("screen", log_level=log_level, log_file=log_file)
    log_run_header(logger, params={
        "input_csv": str(input_csv.resolve()), "out_csv": str(out_csv.resolve()),
        "config": str(config.resolve()), "abstract_column": abstract_column,
        "limit": limit, "dry_run": dry_run, "model1": model1, "model2": model2,
        "strands": strands, "both": both, "clef": clef, "auto_server": auto_server,
        "server_startup_timeout": server_startup_timeout, "log_level": log_level,
    }, log_file=log_file, command=shlex.join([sys.executable, *sys.argv]),
        versions={"pandas": pd.__version__, "typer": typer.__version__})
    logger.info("Screening config: %s", config)
    try:
        cfg = ScreeningConfig.from_sources(load_config(config))
    except (FileNotFoundError, ValueError) as exc:
        logger.exception("Invalid screening config: %s", config)
        raise typer.BadParameter(f"invalid screening config: {exc}") from exc
    mode = "strands" if strands else "both" if both else "clef" if clef else None
    cfg = _select_models(cfg, model1, model2) if model1 is not None else _select_mode(cfg, mode)
    logger.info("Effective screening settings: %s", json.dumps(asdict(cfg), sort_keys=True))
    logger.info("prompt_version=%s input_fields=title+%s", PROMPT_VERSION, abstract_column)
    logger.info("screening mode=%s", mode or ("both" if cfg.first_stage_base_url else cfg.expected_model or "configured"))
    logger.info("model1=%s model2=%s", cfg.first_stage_model or cfg.expected_model,
                cfg.expected_model if cfg.first_stage_base_url else None)
    df = pd.read_csv(input_csv, dtype=str, keep_default_na=False)
    input_count = len(df)
    if abstract_column not in df.columns:
        raise typer.BadParameter(
            f"abstract column {abstract_column!r} was not found; available columns: {', '.join(df.columns)}"
        )

    if limit is not None:
        df = df.iloc[:limit]
    logger.info("Input records=%d selected=%d", input_count, len(df))

    if dry_run:
        for _, row in df.iterrows():
            meta = _row_to_meta(row)
            abstract = prepare_abstract(meta.get(abstract_column, ""), cfg.max_abstract_chars)
            if abstract:
                typer.echo(json.dumps({"state": build_state(meta, cfg, abstract_column), "questions": QUESTIONS}, indent=2, ensure_ascii=False))
                break
        logger.info("Dry run finished: selected=%d; no inference performed", len(df))
        return

    with requests.Session() as session:
        try:
            if cfg.first_stage_base_url and OMNI_MODEL in (cfg.expected_model, cfg.first_stage_model):
                _screen_staged(df, out_csv, abstract_column, cfg, session, logger, enabled=auto_server,
                               startup_timeout=server_startup_timeout, log_dir=log_file.parent)
                return
            with screening_servers(cfg, session, logger, log_file.parent, enabled=auto_server,
                                   startup_timeout=server_startup_timeout) as running_cfg:
                _screen(df, out_csv, abstract_column, running_cfg, session, logger)
        except RuntimeError as exc:
            logger.exception("Screening failed")
            raise typer.BadParameter(str(exc)) from exc


def _screen(
    df: pd.DataFrame, out_csv: Path, abstract_column: str, cfg: ScreeningConfig,
    session: requests.Session, logger: logging.Logger, *, collect: bool = False,
) -> list[dict[str, Any]]:
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    cache = ScoreCache(Path(cfg.cache_csv)) if cfg.cache_csv is not None else None
    fieldnames = list(dict.fromkeys(output_column(col) for col in list(df.columns) + EXTRA_COLUMNS))

    try:
        health_data = check_health(session, cfg.base_url, cfg.timeout, cfg.first_stage_base_url)
        logger.info(
            "server status=%s model=%s device=%s max_length=%s",
            health_data.get("status"),
            health_data.get("model"),
            health_data.get("device"),
            health_data.get("max_length"),
        )
    except Exception as exc:
        raise typer.BadParameter(f"screening server check failed: {exc}") from exc

    processed_count = 0
    error_count = 0
    rows: list[dict[str, Any]] = []

    with out_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()

        progress = tqdm(
            df.iterrows(),
            total=len(df),
            desc="Screening",
            file=sys.stdout,
            position=0,
            dynamic_ncols=True,
        )
        status = tqdm(
            total=0,
            position=1,
            bar_format="{desc}",
            leave=False,
            file=sys.stdout,
        )

        urls = [cfg.base_url] + ([cfg.first_stage_base_url] if cfg.first_stage_base_url else [])
        try:
            with gpu_progress(urls, lambda value: progress.set_postfix_str(value, refresh=True)):
                for _, row in progress:
                    meta = _row_to_meta(row)
                    out_row = meta | screen_row(session, meta, cfg, abstract_column, cache=cache)
                    if out_row["flag_label"] == "process_error":
                        error_count += 1
                        logger.warning("process_error record=%s error=%s", out_row["flag_record_id"], out_row["flag_reason"])

                    writer.writerow(output_row(out_row))
                    if collect:
                        rows.append(out_row)
                    handle.flush()
                    processed_count += 1

                    status.set_description_str(format_status(out_row, meta.get("title", "").strip()), refresh=True)
        finally:
            status.clear()
            status.close()
            progress.close()

    if cache is not None:
        logger.info("Score cache hits=%d misses=%d", cache.hits, cache.misses)

    typer.echo(f"Finished: processed={processed_count} errors={error_count} output={out_csv}")
    logger.info("Finished: processed=%d errors=%d output=%s", processed_count, error_count, out_csv.resolve())
    return rows


def _screen_staged(
    df: pd.DataFrame, out_csv: Path, abstract_column: str, cfg: ScreeningConfig,
    session: requests.Session, logger: logging.Logger, *, enabled: bool, startup_timeout: float,
    log_dir: Path,
) -> None:
    """Release owned first-stage weights before loading a GPU-filling Omni model."""
    primary = replace(cfg, base_url=cfg.first_stage_base_url or cfg.base_url,
                      expected_model=cfg.first_stage_model, batch_questions=cfg.first_stage_batch_questions,
                      first_stage_base_url=None, first_stage_model=None)
    secondary = replace(cfg, first_stage_base_url=None, first_stage_model=None)
    typer.echo(f"Stage 1: model={primary.expected_model}; stage 2: model={secondary.expected_model} (unsure only)")
    with screening_servers(primary, session, logger, log_dir, enabled=enabled,
                           startup_timeout=startup_timeout) as running:
        rows = _screen(df, out_csv, abstract_column, running, session, logger, collect=True)
    pending = []
    details = {}
    for index, row in enumerate(rows):
        abstract = prepare_abstract(str(row.get(abstract_column, "")), cfg.max_abstract_chars)
        if not abstract:
            continue
        try:
            metadata, accepted = first_stage_details(row, cfg, abstract)
        except (KeyError, TypeError, ValueError):
            metadata = {"first_stage_model": primary.expected_model or "", "first_stage_label": "process_error",
                        "first_stage_error": row.get("flag_reason", "invalid first-stage scores"),
                        "first_stage_latency_ms": row.get("strands_latency_ms", 0)}
            accepted = False
        details[index] = metadata
        row.update(metadata | {"evaluation_stage": "first"})
        if not accepted:
            pending.append(index)
            row["evaluation_stage"] = "pending"
            if row.get("flag_label") in ("in_scope", "out_of_scope"):
                row["flag_label"] = "unsure"
                row["flag_reason"] = "unsure: invalid first-stage probabilities"
    fieldnames = list(dict.fromkeys(output_column(col) for col in list(df.columns) + EXTRA_COLUMNS))
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="screen-stages-", dir=out_csv.parent) as directory:
        temporary = Path(directory) / "merged.csv"

        def save() -> None:
            with temporary.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(output_row(row) for row in rows)
            temporary.replace(out_csv)

        # Preserve completed first-stage work even if startup or stage 2 is interrupted.
        save()
        typer.echo(f"Stage 2: {len(pending)}/{len(rows)} records require rechecking")
        logger.info("Stage 2: model=%s rechecking=%d total=%d", secondary.expected_model, len(pending), len(rows))
        if pending:
            with screening_servers(secondary, session, logger, log_dir, enabled=enabled,
                                   startup_timeout=startup_timeout, wait_for_memory=True) as running:
                checked = _screen(df.iloc[pending], Path(directory) / "second.csv", abstract_column,
                                  running, session, logger, collect=True)
            for index, result in zip(pending, checked, strict=True):
                metadata = details[index]
                if "strands_latency_ms" in result:
                    result["strands_latency_ms"] = float(result["strands_latency_ms"]) + float(metadata["first_stage_latency_ms"] or 0)
                rows[index] = result | metadata | {"evaluation_stage": "second"}
            save()
    errors = sum(row.get("flag_label") == "process_error" for row in rows)
    typer.echo(f"Finished cascade: processed={len(rows)} errors={errors} output={out_csv}")
    logger.info("Finished cascade: processed=%d errors=%d output=%s", len(rows), errors, out_csv.resolve())

if __name__ == "__main__":
    app()
