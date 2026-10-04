from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import pandas as pd
import requests
import typer
from tqdm import tqdm

from libs.cli_logging import log_run_header, setup_logger
from libs.sources import (
    _date_range_clause,
    build_query_with_excludes,
    crossref_fill_missing_doi,
    crossref_search_papers,
    merge_papers_by_doi_title,
    openalex_search_papers,
    pubmed_fetch_details,
    pubmed_search_all_pmids,
)
from libs.strands_screening import (
    EXTRA_COLUMNS,
    ScreeningConfig,
    check_health,
    format_status,
    load_config,
    screen_row,
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
    strands_filter: bool = typer.Option(
        False,
        "--strands-filter/--no-strands-filter",
        help="Screen merged papers with Strands Decider and exclude only out_of_scope from final output.",
    ),
    strands_config: Path | None = typer.Option(
        None,
        "--strands-config",
        help="Strands JSON/JSONC config. Defaults to config/strands_flagger.example.jsonc.",
    ),
    strands_base_url: str | None = typer.Option(
        None,
        "--strands-base-url",
        help="Override the Strands Decider server URL from config.",
    ),
    crossref: bool = typer.Option(False, help="Try filling missing DOI via Crossref (heuristic)."),
    user_agent: str | None = typer.Option(
        None,
        help="User-Agent header for Crossref requests.",
    ),
    sleep: float = typer.Option(0.34, min=0.0, help="Sleep seconds between Entrez requests."),
    out_prefix: str = typer.Option("edna_papers", help="Output prefix (CSV/JSON)."),
    out_dir: Path = typer.Option(Path("."), help="Output directory."),
    log_level: str = typer.Option("INFO", help="Log level: DEBUG, INFO, WARNING, ERROR"),
    log_file: Path | None = typer.Option(None, help="Write logs to this file as well."),
) -> None:
    """
    Fetch paper metadata from PubMed and export CSV/JSON.
    Merge duplicate records by DOI or title.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    logger = setup_logger("edna_literature_fetch", log_level=log_level, log_file=log_file)

    if strands_filter and not abstract:
        abstract = True
        logger.info("Strands filtering requires abstracts; enabling abstract retrieval.")

    strands_cfg = ScreeningConfig()
    strands_config_path: Path | None = None
    strands_session: requests.Session | None = None

    if strands_filter:
        strands_config_path = strands_config
        if strands_config_path is None:
            strands_config_path = (
                Path(__file__).resolve().parents[1]
                / "config"
                / "strands_flagger.example.jsonc"
            )
        try:
            cfg_dict = load_config(strands_config_path)
        except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
            raise typer.BadParameter(f"invalid Strands config: {exc}") from exc
        strands_cfg = ScreeningConfig.from_sources(cfg_dict, base_url=strands_base_url)
        strands_session = requests.Session()
        try:
            health_data = check_health(strands_session, strands_cfg.base_url, strands_cfg.timeout)
        except Exception as exc:
            raise typer.BadParameter(
                f"could not connect to Strands Decider at {strands_cfg.base_url}: {exc}"
            ) from exc
        logger.info(
            "Strands filter server status=%s model=%s device=%s max_length=%s",
            health_data.get("status"),
            health_data.get("model"),
            health_data.get("device"),
            health_data.get("max_length"),
        )

    exclude_terms = exclude or []
    final_query = build_query_with_excludes(query, exclude_terms)
    date_clause = _date_range_clause(since, until, datetype)
    if date_clause:
        final_query = f"({final_query}) AND {date_clause}"

    if user_agent is None:
        user_agent = f"edna-literature-fetch/1.0 (mailto:{email})"

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
        "strands_filter": strands_filter,
        "strands_config": str(strands_config_path) if strands_config_path else None,
        "strands_base_url": strands_cfg.base_url if strands_filter else None,
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

    if date_clause:
        logger.info(f"Date clause applied: {date_clause}")

    all_papers = []

    if source_pubmed:
        pmids = pubmed_search_all_pmids(
            query=final_query,
            email=email,
            api_key=api_key,
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
        logger.error("No results from selected sources.")
        raise typer.Exit(code=1)

    if crossref:
        all_papers = crossref_fill_missing_doi(all_papers, user_agent=user_agent, logger=logger)
    papers = merge_papers_by_doi_title(all_papers, logger=logger)

    if abstract:
        before_filter = len(papers)
        papers = [p for p in papers if p.abstract and p.abstract.strip()]
        if len(papers) != before_filter:
            logger.info(f"Filtered records without abstract: {before_filter} -> {len(papers)}")

    df = pd.DataFrame([asdict(p) for p in papers])

    rejected_df: pd.DataFrame | None = None
    if strands_filter:
        if strands_session is None:
            raise RuntimeError("Strands session was not initialized")

        kept_rows: list[dict[str, object]] = []
        rejected_rows: list[dict[str, object]] = []
        error_count = 0

        progress = tqdm(
            df.to_dict(orient="records"),
            total=len(df),
            desc="Strands filtering",
            position=0,
            dynamic_ncols=True,
        )
        status = tqdm(
            total=0,
            position=1,
            bar_format="{desc}",
            leave=False,
        )

        for row in progress:
            meta = {
                str(key): "" if pd.isna(value) else str(value)
                for key, value in row.items()
            }
            screened_row: dict[str, object] = dict(row) | screen_row(strands_session, meta, strands_cfg)
            if screened_row["flag_label"] == "process_error":
                error_count += 1
                logger.warning(
                    "Strands process_error record=%s error=%s",
                    screened_row["flag_record_id"],
                    screened_row["flag_reason"],
                )

            label = str(screened_row.get("flag_label", ""))
            if label == "out_of_scope":
                rejected_rows.append(screened_row)
            else:
                kept_rows.append(screened_row)

            status.set_description_str(format_status(screened_row, meta.get("title", "")), refresh=True)

        status.clear()
        status.close()
        progress.refresh()

        screened_columns = list(df.columns) + [
            col for col in EXTRA_COLUMNS if col not in df.columns
        ]
        df = pd.DataFrame(kept_rows, columns=screened_columns)
        rejected_df = pd.DataFrame(rejected_rows, columns=screened_columns)
        logger.info(
            "Strands filter result: retained=%d rejected=%d process_error=%d",
            len(df),
            len(rejected_df),
            error_count,
        )

    csv_path = out_dir / f"{out_prefix}.csv"
    json_path = out_dir / f"{out_prefix}.json"

    logger.info(f"Writing CSV: {csv_path}")
    df.to_csv(csv_path, index=False)

    logger.info(f"Writing JSON: {json_path}")
    df.to_json(json_path, orient="records", force_ascii=False, indent=2)

    if rejected_df is not None:
        rejected_csv_path = out_dir / f"{out_prefix}.rejected.csv"
        rejected_json_path = out_dir / f"{out_prefix}.rejected.json"
        logger.info(f"Writing Strands rejected CSV: {rejected_csv_path}")
        rejected_df.to_csv(rejected_csv_path, index=False)
        logger.info(f"Writing Strands rejected JSON: {rejected_json_path}")
        rejected_df.to_json(rejected_json_path, orient="records", force_ascii=False, indent=2)

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

    if strands_filter and rejected_df is not None:
        logger.info(
            "Done. Retained: %d, rejected out_of_scope: %d",
            len(df),
            len(rejected_df),
        )
    else:
        logger.info(f"Done. Count: {len(df)}")


if __name__ == "__main__":
    app()
