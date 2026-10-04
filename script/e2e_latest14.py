from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests
import typer

app = typer.Typer(add_completion=False)

DEFAULT_QUERY = (
    '("environmental DNA"[Title/Abstract] OR eDNA[Title/Abstract] '
    'OR "environmental RNA"[Title/Abstract] OR eRNA[Title/Abstract])'
)
VALID_LABELS = {"in_scope", "out_of_scope", "unsure", "process_error"}


def _parse_date(value: str | None) -> date:
    if value is None:
        return datetime.now().astimezone().date()

    normalized = value.strip().replace("/", "-")
    try:
        return date.fromisoformat(normalized)
    except ValueError as exc:
        raise typer.BadParameter(
            f"invalid date {value!r}; use YYYY-MM-DD or YYYY/MM/DD"
        ) from exc


def _repo_path(repo_root: Path, value: Path) -> Path:
    return value if value.is_absolute() else repo_root / value


def _run(cmd: list[str], *, cwd: Path) -> None:
    typer.echo("")
    typer.echo(f"$ {shlex.join(cmd)}")
    try:
        subprocess.run(cmd, cwd=cwd, check=True)
    except subprocess.CalledProcessError as exc:
        raise typer.Exit(code=exc.returncode or 1) from exc


def _check_strands(base_url: str, timeout: float = 10.0) -> dict[str, object]:
    url = f"{base_url.rstrip('/')}/health"
    try:
        response = requests.get(url, timeout=timeout)
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        raise typer.BadParameter(
            f"Strands Decider is not reachable at {url}: {exc}. "
            "Start it first with: pixi run strands-serve"
        ) from exc

    if not isinstance(data, dict) or data.get("status") != "ok":
        raise typer.BadParameter(f"unexpected Strands health response: {data!r}")
    return data


def _validate_fetch(csv_path: Path) -> pd.DataFrame:
    if not csv_path.exists():
        raise RuntimeError(f"fetch output was not created: {csv_path}")

    df = pd.read_csv(csv_path, dtype=str, keep_default_na=False)
    required = {"title", "abstract"}
    missing = sorted(required - set(df.columns))
    if missing:
        raise RuntimeError(f"fetch output is missing columns: {', '.join(missing)}")
    if df.empty:
        raise RuntimeError("fetch output contains zero papers")
    if df["abstract"].str.strip().eq("").any():
        raise RuntimeError("fetch output contains empty abstracts despite --abstract")

    return df


def _validate_screen(
    csv_path: Path,
    *,
    expected_rows: int,
) -> tuple[pd.DataFrame, dict[str, int]]:
    if not csv_path.exists():
        raise RuntimeError(f"Strands output was not created: {csv_path}")

    df = pd.read_csv(csv_path, dtype=str, keep_default_na=False)
    required = {
        "title",
        "abstract",
        "flag_label",
        "strands_p_in_scope",
        "strands_p_out_of_scope",
        "strands_p_actual_use",
        "strands_p_method_relevance",
    }
    missing = sorted(required - set(df.columns))
    if missing:
        raise RuntimeError(f"Strands output is missing columns: {', '.join(missing)}")
    if len(df) != expected_rows:
        raise RuntimeError(
            f"unexpected Strands row count: expected={expected_rows} actual={len(df)}"
        )

    invalid = sorted(set(df["flag_label"]) - VALID_LABELS)
    if invalid:
        raise RuntimeError(f"unexpected flag_label values: {', '.join(invalid)}")

    counts = {
        label: int((df["flag_label"] == label).sum())
        for label in ("in_scope", "out_of_scope", "unsure", "process_error")
    }
    if counts["process_error"]:
        raise RuntimeError(
            f"Strands screening completed with process_error={counts['process_error']}"
        )

    return df, counts


@app.command()
def run(
    email: str | None = typer.Option(
        None,
        "--email",
        envvar="NCBI_EMAIL",
        help="Email for NCBI Entrez. Can also be set with NCBI_EMAIL.",
    ),
    api_key: str | None = typer.Option(
        None,
        "--api-key",
        envvar="NCBI_API_KEY",
        help="Optional NCBI API key. Can also be set with NCBI_API_KEY.",
    ),
    openalex_api_key: str | None = typer.Option(
        None,
        "--openalex-api-key",
        envvar="OPENALEX_API_KEY",
        help="Optional OpenAlex API key. Can also be set with OPENALEX_API_KEY.",
    ),
    days: int = typer.Option(
        14,
        "--days",
        min=1,
        help="Inclusive retrieval window ending at --until-date.",
    ),
    until_date: str | None = typer.Option(
        None,
        "--until-date",
        help="Window end date. Defaults to the current local date.",
    ),
    query: str = typer.Option(DEFAULT_QUERY, "--query"),
    crossref_max_items: int = typer.Option(300, "--crossref-max-items", min=1),
    openalex_max_items: int = typer.Option(300, "--openalex-max-items", min=1),
    screen_limit: int = typer.Option(
        20,
        "--screen-limit",
        min=0,
        help="Number of fetched papers to screen. 0 means all papers.",
    ),
    config: Path = typer.Option(
        Path("config/strands_flagger.jsonc"),
        "--config",
        help="Strands flagger config.",
    ),
    base_url: str = typer.Option(
        "http://127.0.0.1:8012",
        "--base-url",
        help="Running Strands Decider server.",
    ),
    out_dir: Path = typer.Option(
        Path("test/results"),
        "--out-dir",
        help="Directory for E2E artifacts.",
    ),
    prefix: str | None = typer.Option(
        None,
        "--prefix",
        help="Output prefix. Defaults to e2e_latest14_<YYYYMMDD>.",
    ),
) -> None:
    """Run literature retrieval -> Strands screening -> basic E2E validation."""
    if not email:
        raise typer.BadParameter(
            "--email is required (or set the NCBI_EMAIL environment variable)"
        )

    repo_root = Path(__file__).resolve().parents[1]
    config_path = _repo_path(repo_root, config)
    out_dir_path = _repo_path(repo_root, out_dir)
    if not config_path.exists():
        raise typer.BadParameter(f"Strands config not found: {config_path}")

    until = _parse_date(until_date)
    since = until - timedelta(days=days - 1)
    prefix = prefix or f"e2e_latest{days}_{until:%Y%m%d}"

    out_dir_path.mkdir(parents=True, exist_ok=True)
    logs_dir = repo_root / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    fetch_csv = out_dir_path / f"{prefix}.csv"
    fetch_json = out_dir_path / f"{prefix}.json"
    strands_csv = out_dir_path / f"{prefix}_strands.csv"
    summary_json = out_dir_path / f"{prefix}_summary.json"
    fetch_log = logs_dir / f"{prefix}.fetch.log"
    strands_log = logs_dir / f"{prefix}.strands.log"

    typer.echo("E2E latest-literature test")
    typer.echo(f"window       : {since.isoformat()} .. {until.isoformat()} ({days} days)")
    typer.echo(f"screen limit : {'all' if screen_limit == 0 else screen_limit}")
    typer.echo(f"output dir   : {out_dir_path}")

    health = _check_strands(base_url)
    typer.echo(
        "Strands      : "
        f"status={health.get('status')} "
        f"model={health.get('model')} "
        f"device={health.get('device')}"
    )

    fetch_cmd = [
        sys.executable,
        str(repo_root / "script" / "edna_literature_fetch.py"),
        "--email",
        email,
        "--query",
        query,
        "--since",
        since.strftime("%Y/%m/%d"),
        "--until",
        until.strftime("%Y/%m/%d"),
        "--abstract",
        "--crossref-max-items",
        str(crossref_max_items),
        "--openalex-max-items",
        str(openalex_max_items),
        "--user-agent",
        f"eDNA-paper-downloader-e2e/1.0 (mailto:{email})",
        "--out-dir",
        str(out_dir_path),
        "--out-prefix",
        prefix,
        "--log-file",
        str(fetch_log),
    ]
    if api_key:
        fetch_cmd.extend(["--api-key", api_key])
    if openalex_api_key:
        fetch_cmd.extend(["--openalex-api-key", openalex_api_key])

    started = time.monotonic()
    _run(fetch_cmd, cwd=repo_root)
    fetched = _validate_fetch(fetch_csv)
    fetch_seconds = time.monotonic() - started

    expected_screen_rows = len(fetched)
    if screen_limit > 0:
        expected_screen_rows = min(expected_screen_rows, screen_limit)

    # strands_flagger appends by design. A deterministic E2E run must start clean.
    if strands_csv.exists():
        strands_csv.unlink()

    screen_cmd = [
        sys.executable,
        str(repo_root / "script" / "strands_flagger.py"),
        str(fetch_csv),
        "--config",
        str(config_path),
        "--base-url",
        base_url,
        "--out-csv",
        str(strands_csv),
        "--log-file",
        str(strands_log),
        "--no-resume",
    ]
    if screen_limit > 0:
        screen_cmd.extend(["--limit", str(screen_limit)])

    started = time.monotonic()
    _run(screen_cmd, cwd=repo_root)
    screened, counts = _validate_screen(
        strands_csv,
        expected_rows=expected_screen_rows,
    )
    screen_seconds = time.monotonic() - started

    summary = {
        "window": {
            "since": since.isoformat(),
            "until": until.isoformat(),
            "days": days,
        },
        "query": query,
        "fetched_rows": int(len(fetched)),
        "screened_rows": int(len(screened)),
        "screen_limit": screen_limit,
        "labels": counts,
        "timing_seconds": {
            "fetch": round(fetch_seconds, 3),
            "screen": round(screen_seconds, 3),
            "total": round(fetch_seconds + screen_seconds, 3),
        },
        "artifacts": {
            "fetch_csv": str(fetch_csv),
            "fetch_json": str(fetch_json),
            "strands_csv": str(strands_csv),
            "fetch_log": str(fetch_log),
            "strands_log": str(strands_log),
        },
    }
    summary_json.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    typer.echo("")
    typer.echo("E2E PASS")
    typer.echo(f"fetched      : {len(fetched)}")
    typer.echo(f"screened     : {len(screened)}")
    typer.echo(
        "labels       : "
        f"in_scope={counts['in_scope']} "
        f"out_of_scope={counts['out_of_scope']} "
        f"unsure={counts['unsure']} "
        f"process_error={counts['process_error']}"
    )
    typer.echo(f"fetch time   : {fetch_seconds:.1f}s")
    typer.echo(f"screen time  : {screen_seconds:.1f}s")
    typer.echo(f"result       : {strands_csv}")
    typer.echo(f"summary      : {summary_json}")


if __name__ == "__main__":
    app()
