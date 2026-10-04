from __future__ import annotations

import json
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
VALID_RETAINED_LABELS = {"in_scope", "unsure", "process_error"}


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


def _display_cmd(cmd: list[str]) -> str:
    masked = cmd.copy()
    for flag in ("--api-key", "--openalex-api-key"):
        if flag in masked:
            index = masked.index(flag)
            if index + 1 < len(masked):
                masked[index + 1] = "***"
    return shlex.join(masked)


def _run(cmd: list[str], *, cwd: Path) -> None:
    typer.echo("")
    typer.echo(f"$ {_display_cmd(cmd)}")
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


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise RuntimeError(f"expected E2E artifact was not created: {path}")
    return pd.read_csv(path, dtype=str, keep_default_na=False)


def _validate_filtered_outputs(
    retained_path: Path,
    rejected_path: Path,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, int]]:
    retained = _read_csv(retained_path)
    rejected = _read_csv(rejected_path)

    required = {
        "title",
        "abstract",
        "flag_label",
        "strands_p_in_scope",
        "strands_p_out_of_scope",
        "strands_p_actual_use",
        "strands_p_method_relevance",
    }
    for name, df in (("retained", retained), ("rejected", rejected)):
        missing = sorted(required - set(df.columns))
        if missing:
            raise RuntimeError(
                f"{name} output is missing columns: {', '.join(missing)}"
            )
        if not df.empty and df["abstract"].str.strip().eq("").any():
            raise RuntimeError(f"{name} output contains an empty abstract")

    total = len(retained) + len(rejected)
    if total == 0:
        raise RuntimeError("E2E retrieval produced zero papers with abstracts")

    retained_invalid = sorted(set(retained["flag_label"]) - VALID_RETAINED_LABELS)
    if retained_invalid:
        raise RuntimeError(
            "retained output contains invalid labels: "
            + ", ".join(retained_invalid)
        )

    rejected_invalid = sorted(set(rejected["flag_label"]) - {"out_of_scope"})
    if rejected_invalid:
        raise RuntimeError(
            "rejected output contains non-out_of_scope labels: "
            + ", ".join(rejected_invalid)
        )

    counts = {
        "in_scope": int((retained["flag_label"] == "in_scope").sum()),
        "unsure": int((retained["flag_label"] == "unsure").sum()),
        "process_error": int((retained["flag_label"] == "process_error").sum()),
        "out_of_scope": int(len(rejected)),
    }
    if counts["process_error"]:
        raise RuntimeError(
            f"Strands filtering completed with process_error={counts['process_error']}"
        )

    return retained, rejected, counts


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
    config: Path = typer.Option(
        Path("config/strands_flagger.example.jsonc"),
        "--config",
        help="Strands screening config.",
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
    """Run latest literature retrieval with integrated Strands filtering."""
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

    retained_csv = out_dir_path / f"{prefix}.csv"
    retained_json = out_dir_path / f"{prefix}.json"
    rejected_csv = out_dir_path / f"{prefix}.rejected.csv"
    rejected_json = out_dir_path / f"{prefix}.rejected.json"
    summary_json = out_dir_path / f"{prefix}_summary.json"
    fetch_log = logs_dir / f"{prefix}.fetch.log"

    typer.echo("E2E latest-literature + integrated Strands filter")
    typer.echo(f"window       : {since.isoformat()} .. {until.isoformat()} ({days} days)")
    typer.echo(f"output dir   : {out_dir_path}")

    health = _check_strands(base_url)
    typer.echo(
        "Strands      : "
        f"status={health.get('status')} "
        f"model={health.get('model')} "
        f"device={health.get('device')}"
    )

    for path in (retained_csv, retained_json, rejected_csv, rejected_json):
        if path.exists():
            path.unlink()

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
        "--strands-filter",
        "--strands-config",
        str(config_path),
        "--strands-base-url",
        base_url,
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
    elapsed = time.monotonic() - started

    retained, rejected, counts = _validate_filtered_outputs(
        retained_csv,
        rejected_csv,
    )

    summary = {
        "window": {
            "since": since.isoformat(),
            "until": until.isoformat(),
            "days": days,
        },
        "query": query,
        "retrieved_with_abstract": int(len(retained) + len(rejected)),
        "retained_rows": int(len(retained)),
        "rejected_rows": int(len(rejected)),
        "labels": counts,
        "timing_seconds": {
            "total": round(elapsed, 3),
        },
        "artifacts": {
            "retained_csv": str(retained_csv),
            "retained_json": str(retained_json),
            "rejected_csv": str(rejected_csv),
            "rejected_json": str(rejected_json),
            "fetch_log": str(fetch_log),
        },
    }
    summary_json.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    typer.echo("")
    typer.echo("E2E PASS")
    typer.echo(f"retrieved     : {len(retained) + len(rejected)}")
    typer.echo(f"retained      : {len(retained)}")
    typer.echo(f"rejected      : {len(rejected)}")
    typer.echo(
        "labels        : "
        f"in_scope={counts['in_scope']} "
        f"out_of_scope={counts['out_of_scope']} "
        f"unsure={counts['unsure']} "
        f"process_error={counts['process_error']}"
    )
    typer.echo(f"total time    : {elapsed:.1f}s")
    typer.echo(f"retained CSV  : {retained_csv}")
    typer.echo(f"rejected CSV  : {rejected_csv}")
    typer.echo(f"summary       : {summary_json}")


if __name__ == "__main__":
    app()
