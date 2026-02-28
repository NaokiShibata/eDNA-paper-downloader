from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pandas as pd
import typer

from libs.cli_logging import log_run_header, setup_logger
from libs.edna_pubmed import (
    _date_range_clause,
    _normalize_date_str,
    build_query_with_excludes,
    crossref_fill_missing_doi,
    keep_latest_per_doi_pubmed,
    pubmed_fetch_details,
    pubmed_search_all_pmids,
)

app = typer.Typer(add_completion=False)


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

    if not pmids:
        logger.warning("No results.")
        raise typer.Exit(code=0)

    papers = pubmed_fetch_details(
        pmids=pmids,
        email=email,
        api_key=api_key,
        include_abstract=abstract,
        sleep=sleep,
        logger=logger,
    )

    if crossref:
        papers = crossref_fill_missing_doi(papers, user_agent=user_agent, logger=logger)

    papers = keep_latest_per_doi_pubmed(papers, logger=logger)

    df = pd.DataFrame([asdict(p) for p in papers])

    csv_path = out_dir / f"{out_prefix}.csv"
    json_path = out_dir / f"{out_prefix}.json"

    logger.info(f"Writing CSV: {csv_path}")
    df.to_csv(csv_path, index=False)

    logger.info(f"Writing JSON: {json_path}")
    json_path.write_text(json.dumps(df.to_dict(orient="records"), ensure_ascii=False, indent=2), encoding="utf-8")

    logger.info(f"Done. Count: {len(df)}")


if __name__ == "__main__":
    app()
