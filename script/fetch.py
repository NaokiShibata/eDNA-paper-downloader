from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import requests
import typer
from tqdm import tqdm

from libs.cli_logging import log_run_header, setup_logger
from libs.edna_models import Paper
from libs.sources import (
    _date_range_clause,
    _query_terms,
    biorxiv_search_papers,
    build_query_with_excludes,
    crossref_search_papers,
    drop_repository_records,
    europepmc_fill_abstracts,
    merge_papers_by_doi_title,
    openalex_search_papers,
    pubmed_fetch_details,
    pubmed_search_all_pmids,
)
from libs.strands_screening import (
    EXTRA_COLUMNS,
    ScoreCache,
    ScreeningConfig,
    check_health,
    default_config_path,
    format_status,
    load_config,
    output_column,
    output_row,
    screen_row,
)

DEFAULT_QUERY = '("environmental DNA"[Title/Abstract] OR eDNA[Title/Abstract])'

app = typer.Typer(add_completion=False)


def parse_sources(value: str) -> set[str]:
    sources = {name.strip() for name in value.split(",")}
    unknown = sources - {"pubmed", "crossref", "openalex", "biorxiv", "medrxiv"}
    if unknown:
        raise ValueError(f"Unknown sources: {', '.join(sorted(unknown))}")
    return sources


def date_window(since: str | None, until: str | None, days: int | None,
                today: date | None = None) -> tuple[str | None, str]:
    end = date.fromisoformat(until.replace("/", "-")) if until else today or date.today()
    if days is not None:
        if since is not None:
            raise ValueError("--days cannot be combined with --since")
        if days < 1:
            raise ValueError("--days must be > 0")
        since = (end - timedelta(days=days - 1)).strftime("%Y/%m/%d")
    return since, end.strftime("%Y/%m/%d")


@app.command()
def fetch(
    email: str = typer.Option(..., envvar="NCBI_EMAIL", help="Your email for NCBI Entrez."),
    api_key: str | None = typer.Option(None, envvar="NCBI_API_KEY"),
    openalex_api_key: str | None = typer.Option(None, envvar="OPENALEX_API_KEY"),
    query: str = typer.Option(DEFAULT_QUERY),
    exclude: list[str] | None = typer.Option(None, "--exclude"),
    since: str | None = typer.Option(None, help="Start date (YYYY/MM/DD or YYYY-MM-DD)."),
    until: str | None = typer.Option(None, help="End date; defaults to today."),
    days: int | None = typer.Option(None, min=1, help="Inclusive window ending at --until or today."),
    datetype: str = typer.Option("pdat"),
    sources: str = typer.Option("pubmed,crossref,openalex", help="Comma-separated source names."),
    max_items: int = typer.Option(1000, min=1, help="Maximum items per Crossref/OpenAlex source."),
    strands: bool = typer.Option(False, "--strands/--no-strands"),
    sleep: float = typer.Option(0.34, min=0.0),
    out_prefix: str = typer.Option("edna_papers"),
    out_dir: Path = typer.Option(Path(".")),
    log_level: str = typer.Option("INFO"),
    log_file: Path | None = typer.Option(None),
) -> None:
    """
    Fetch papers with abstracts from selected sources and export CSV.
    Merge duplicate records by DOI or title.
    """
    try:
        selected_sources = parse_sources(sources)
        since, until = date_window(since, until, days)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if selected_sources & {"biorxiv", "medrxiv"} and not since:
        raise typer.BadParameter("--since or --days is required for bioRxiv/medRxiv")

    out_dir.mkdir(parents=True, exist_ok=True)
    logger = setup_logger("fetch", log_level=log_level, log_file=log_file)

    strands_cfg = ScreeningConfig()
    strands_config_path: Path | None = None
    strands_session: requests.Session | None = None

    if strands:
        strands_config_path = default_config_path()
        logger.info("Strands config: %s", strands_config_path)
        try:
            strands_cfg = ScreeningConfig.from_sources(load_config(strands_config_path))
        except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
            raise typer.BadParameter(f"invalid Strands config: {exc}") from exc
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
        "sources": sources,
        "max_items": max_items,
        "openalex_api_key": "***" if openalex_api_key else None,
        "strands": strands,
        "strands_config": str(strands_config_path) if strands_config_path else None,
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
        fallback_command="python script/fetch.py",
    )

    if date_clause:
        logger.info(f"Date clause applied: {date_clause}")

    all_papers = []

    if "pubmed" in selected_sources:
        pmids = pubmed_search_all_pmids(
            query=final_query,
            email=email,
            api_key=api_key,
            sort="most+recent",
            batch=10000,
            sleep=sleep,
            logger=logger,
        )

        if pmids:
            pubmed_papers = pubmed_fetch_details(
                pmids=pmids,
                email=email,
                api_key=api_key,
                include_abstract=True,
                sleep=sleep,
                logger=logger,
            )
            all_papers.extend(pubmed_papers)
        else:
            logger.warning("No PubMed results.")

    if "crossref" in selected_sources:
        all_papers.extend(
            crossref_search_papers(
                query=query,
                user_agent=user_agent,
                max_items=max_items,
                from_date=since,
                until_date=until,
                include_abstract=True,
                excludes=exclude_terms,
                sleep=sleep,
                logger=logger,
            )
        )

    if "openalex" in selected_sources:
        all_papers.extend(
            openalex_search_papers(
                query=query,
                max_items=max_items,
                from_date=since,
                until_date=until,
                email=email,
                api_key=openalex_api_key,
                include_abstract=True,
                excludes=exclude_terms,
                sleep=sleep,
                logger=logger,
            )
        )

    for server in sorted(selected_sources & {"biorxiv", "medrxiv"}):
        assert since is not None
        all_papers.extend(biorxiv_search_papers(
            server=server, from_date=since, to_date=until,
            query=" OR ".join(_query_terms(query)), excludes=exclude_terms,
            sleep=sleep, logger=logger, include_abstract=True,
        ))

    if not all_papers:
        logger.error("No results from selected sources.")
        raise typer.Exit(code=1)

    all_papers = drop_repository_records(all_papers, logger=logger)
    papers = merge_papers_by_doi_title(all_papers, logger=logger)

    columns = list(Paper.__dataclass_fields__)
    papers = europepmc_fill_abstracts(papers, sleep=sleep, logger=logger)
    missing = [p for p in papers if not (p.abstract or "").strip()]
    missing_df = pd.DataFrame([asdict(p) for p in missing], columns=columns)
    missing_df.to_csv(out_dir / f"{out_prefix}.no_abstract.csv", index=False)
    logger.info("Records without abstract saved separately: %d", len(missing))
    papers = [p for p in papers if (p.abstract or "").strip()]

    df = pd.DataFrame([asdict(p) for p in papers], columns=columns)

    rejected_df: pd.DataFrame | None = None
    if strands:
        if strands_session is None:
            raise RuntimeError("Strands session was not initialized")

        kept_rows: list[dict[str, object]] = []
        rejected_rows: list[dict[str, object]] = []
        error_count = 0
        cache = ScoreCache(Path(strands_cfg.cache_csv)) if strands_cfg.cache_csv is not None else None

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
            screened_row: dict[str, object] = dict(row) | screen_row(strands_session, meta, strands_cfg, cache=cache)
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
        screened_columns = [output_column(col) for col in screened_columns]
        df = pd.DataFrame([output_row(row) for row in kept_rows], columns=screened_columns)
        rejected_df = pd.DataFrame([output_row(row) for row in rejected_rows], columns=screened_columns)
        logger.info(
            "Strands filter result: retained=%d rejected=%d process_error=%d",
            len(df),
            len(rejected_df),
            error_count,
        )

        if cache is not None:
            logger.info("Strands cache hits=%d misses=%d", cache.hits, cache.misses)

    csv_path = out_dir / f"{out_prefix}.csv"
    logger.info(f"Writing CSV: {csv_path}")
    df.to_csv(csv_path, index=False)

    if rejected_df is not None:
        rejected_csv_path = out_dir / f"{out_prefix}.rejected.csv"
        logger.info(f"Writing Strands rejected CSV: {rejected_csv_path}")
        rejected_df.to_csv(rejected_csv_path, index=False)

    if strands and rejected_df is not None:
        logger.info(
            "Done. Retained: %d, rejected out_of_scope: %d",
            len(df),
            len(rejected_df),
        )
    else:
        logger.info(f"Done. Count: {len(df)}")


if __name__ == "__main__":
    app()
