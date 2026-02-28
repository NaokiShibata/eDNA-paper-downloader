from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path


def _ensure_runtime(modules: tuple[str, ...]) -> None:
    missing = None
    for name in modules:
        try:
            __import__(name)
        except ModuleNotFoundError:
            missing = name
            break
    if missing is None:
        return

    venv_python = Path(__file__).resolve().parents[1] / ".venv" / "bin" / "python"
    current = Path(sys.executable).resolve()
    if venv_python.exists() and current != venv_python.resolve():
        os.execv(str(venv_python), [str(venv_python), __file__, *sys.argv[1:]])

    raise ModuleNotFoundError(
        f"Missing dependency '{missing}'. Install requirements or run with .venv/bin/python."
    )


_ensure_runtime(("pandas", "typer"))

import pandas as pd
import typer

from libs.cli_logging import log_run_header, setup_logger
from libs.edna_pubmed import (
    _date_range_clause,
    _normalize_date_str,
    build_query_with_excludes,
    crossref_search_papers,
    crossref_fill_missing_doi,
    keep_latest_per_doi_pubmed,
    merge_papers_by_doi_title,
    openalex_search_papers,
    pubmed_fetch_details,
    pubmed_search_all_pmids,
)

app = typer.Typer(add_completion=False)


def _run_biorxiv_search(
    *,
    server: str,
    from_date: str,
    to_date: str,
    query: str,
    out_dir: Path,
    out_prefix: str,
    sleep: float,
    log_level: str,
    log_file: Path | None,
) -> None:
    script_path = Path(__file__).resolve().parent / "biorxiv_search.py"
    cmd = [
        sys.executable,
        str(script_path),
        "--server",
        server,
        "--from-date",
        from_date,
        "--to-date",
        to_date,
        "--query",
        query,
        "--out-dir",
        str(out_dir),
        "--out-prefix",
        out_prefix,
        "--sleep",
        str(sleep),
        "--log-level",
        log_level,
    ]
    if log_file is not None:
        cmd.extend(["--log-file", str(log_file)])

    subprocess.run(cmd, check=True)


@app.command()
def fetch(
    email: str = typer.Option(..., help="Your email for NCBI Entrez."),
    api_key: str | None = typer.Option(None, help="NCBI API key (optional)."),
    query: str = typer.Option(
        '("environmental DNA"[Title/Abstract] OR eDNA[Title/Abstract])',
        help="Base PubMed query string.",
    ),
    exclude: list[str] | None = typer.Option(
        None,
        "--exclude",
        help="Exclude term(s). Can be repeated. Example: --exclude review --exclude '\"meta-analysis\"'",
    ),
    since: str | None = typer.Option(
        None,
        "--since",
        help="Start date (YYYY/MM/DD) for date filter.",
    ),
    until: str | None = typer.Option(
        None,
        "--until",
        help="End date (YYYY/MM/DD) for date filter (optional).",
    ),
    datetype: str = typer.Option(
        "pdat",
        help='Date filter type: "pdat" (publication date) or "edat" (Entrez date).',
    ),
    sort: str = typer.Option(
        "most+recent",
        help='Sort: "most+recent" (recent) or "pub+date" (publication date).',
    ),
    pmid_batch: int = typer.Option(
        10000,
        min=100,
        max=100000,
        help="Batch size for PMID paging in esearch.",
    ),
    source_pubmed: bool = typer.Option(True, "--source-pubmed/--no-source-pubmed", help="Use PubMed source."),
    source_crossref: bool = typer.Option(
        True,
        "--source-crossref/--no-source-crossref",
        help="Use Crossref source (DOI-rich).",
    ),
    source_openalex: bool = typer.Option(
        True,
        "--source-openalex/--no-source-openalex",
        help="Use OpenAlex source (DOI-rich).",
    ),
    crossref_max_items: int = typer.Option(1000, min=1, help="Max items to collect from Crossref."),
    openalex_max_items: int = typer.Option(1000, min=1, help="Max items to collect from OpenAlex."),
    openalex_api_key: str | None = typer.Option(None, help="OpenAlex API key (recommended/required by policy)."),
    run_biorxiv: bool = typer.Option(
        False,
        "--run-biorxiv/--no-run-biorxiv",
        help="Run script/biorxiv_search.py after literature fetch.",
    ),
    biorxiv_server: str = typer.Option("biorxiv", help='bioRxiv target server: "biorxiv" or "medrxiv".'),
    biorxiv_from_date: str | None = typer.Option(None, help="bioRxiv start date YYYY/MM/DD."),
    biorxiv_to_date: str | None = typer.Option(None, help="bioRxiv end date YYYY/MM/DD."),
    biorxiv_query: str = typer.Option("eDNA", help="Query string for bioRxiv local filtering."),
    biorxiv_out_prefix: str | None = typer.Option(None, help="Output prefix for bioRxiv results."),
    abstract: bool = typer.Option(False, help="Include abstracts."),
    crossref: bool = typer.Option(False, help="Try filling missing DOI via Crossref (heuristic)."),
    user_agent: str = typer.Option(
        "edna-literature-fetch/1.0 (mailto:your_email@example.com)",
        help="User-Agent header for Crossref requests.",
    ),
    sleep: float = typer.Option(0.34, min=0.0, help="Sleep seconds between Entrez requests."),
    out_prefix: str = typer.Option("edna_papers", help="Output prefix (CSV/JSON)."),
    out_dir: Path = typer.Option(Path("."), help="Output directory."),
    log_level: str = typer.Option("INFO", help="Log level: DEBUG, INFO, WARNING, ERROR"),
    log_file: Path | None = typer.Option(None, help="Write logs to this file as well."),
):
    """
    Fetch paper metadata from PubMed and export CSV/JSON.
    If DOI duplicates occur, keeps the record with newest (year, PMID).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    logger = setup_logger("edna_literature_fetch", log_level=log_level, log_file=log_file)

    exclude_terms = exclude or []
    final_query = build_query_with_excludes(query, exclude_terms)
    date_clause = _date_range_clause(since, until, datetype)
    if date_clause:
        final_query = f"({final_query}) AND {date_clause}"

    params = {
        "email": email,
        "api_key": "***" if api_key else None,
        "query": query,
        "exclude": exclude_terms,
        "final_query": final_query,
        "since": since,
        "until": until,
        "date_clause": date_clause,
        "datetype": datetype,
        "sort": sort,
        "pmid_batch": pmid_batch,
        "source_pubmed": source_pubmed,
        "source_crossref": source_crossref,
        "source_openalex": source_openalex,
        "crossref_max_items": crossref_max_items,
        "openalex_max_items": openalex_max_items,
        "openalex_api_key": "***" if openalex_api_key else None,
        "run_biorxiv": run_biorxiv,
        "biorxiv_server": biorxiv_server,
        "biorxiv_from_date": biorxiv_from_date,
        "biorxiv_to_date": biorxiv_to_date,
        "biorxiv_query": biorxiv_query,
        "biorxiv_out_prefix": biorxiv_out_prefix,
        "abstract": abstract,
        "crossref": crossref,
        "user_agent": user_agent,
        "sleep": sleep,
        "out_prefix": out_prefix,
        "out_dir": str(out_dir),
        "log_level": log_level,
    }
    log_run_header(
        logger,
        params=params,
        log_file=log_file,
        param_width=16,
        versions={
            "pandas": getattr(pd, "__version__", "unknown"),
            "typer": getattr(typer, "__version__", "unknown"),
        },
        fallback_command="python script/edna_literature_fetch.py",
    )

    since_norm = _normalize_date_str(since)
    until_norm = _normalize_date_str(until)
    if date_clause:
        logger.info(f"Date clause applied: {date_clause}")

    all_papers = []

    if source_pubmed:
        pmids = pubmed_search_all_pmids(
            query=final_query,
            email=email,
            api_key=api_key,
            mindate=since_norm,
            maxdate=until_norm,
            datetype=datetype,
            sort=sort,
            batch=pmid_batch,
            sleep=sleep,
            logger=logger,
        )

        if pmids:
            pubmed_papers = pubmed_fetch_details(
                pmids=pmids,
                email=email,
                api_key=api_key,
                include_abstract=abstract,
                sleep=sleep,
                logger=logger,
            )
            all_papers.extend(pubmed_papers)
        else:
            logger.warning("No PubMed results.")

    if source_crossref:
        all_papers.extend(
            crossref_search_papers(
                query=query,
                user_agent=user_agent,
                max_items=crossref_max_items,
                from_date=since,
                until_date=until,
                include_abstract=abstract,
                excludes=exclude_terms,
                sleep=sleep,
                logger=logger,
            )
        )

    if source_openalex:
        all_papers.extend(
            openalex_search_papers(
                query=query,
                max_items=openalex_max_items,
                from_date=since,
                until_date=until,
                email=email,
                api_key=openalex_api_key,
                include_abstract=abstract,
                excludes=exclude_terms,
                sleep=sleep,
                logger=logger,
            )
        )

    if not all_papers:
        logger.warning("No results from selected sources.")
        raise typer.Exit(code=0)

    papers = merge_papers_by_doi_title(all_papers, logger=logger)
    if crossref:
        papers = crossref_fill_missing_doi(papers, user_agent=user_agent, logger=logger)
    papers = keep_latest_per_doi_pubmed(papers, logger=logger)

    if abstract:
        before_filter = len(papers)
        papers = [p for p in papers if p.abstract and p.abstract.strip()]
        if len(papers) != before_filter:
            logger.info(f"Filtered records without abstract: {before_filter} -> {len(papers)}")

    df = pd.DataFrame([asdict(p) for p in papers])

    csv_path = out_dir / f"{out_prefix}.csv"
    json_path = out_dir / f"{out_prefix}.json"

    logger.info(f"Writing CSV: {csv_path}")
    df.to_csv(csv_path, index=False)

    logger.info(f"Writing JSON: {json_path}")
    json_path.write_text(json.dumps(df.to_dict(orient="records"), ensure_ascii=False, indent=2), encoding="utf-8")

    if run_biorxiv:
        if not biorxiv_from_date or not biorxiv_to_date:
            raise typer.BadParameter("--biorxiv-from-date and --biorxiv-to-date are required with --run-biorxiv")
        bio_prefix = biorxiv_out_prefix or f"{out_prefix}_biorxiv"
        bio_log_file = None
        if log_file is not None:
            bio_log_file = log_file.with_name(f"{log_file.stem}.biorxiv{log_file.suffix}")
        logger.info("Running bioRxiv fetch via script/biorxiv_search.py")
        _run_biorxiv_search(
            server=biorxiv_server,
            from_date=biorxiv_from_date,
            to_date=biorxiv_to_date,
            query=biorxiv_query,
            out_dir=out_dir,
            out_prefix=bio_prefix,
            sleep=sleep,
            log_level=log_level,
            log_file=bio_log_file,
        )

    logger.info(f"Done. Count: {len(df)}")


if __name__ == "__main__":
    app()
