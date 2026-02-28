from __future__ import annotations

import html
import json
import logging
import re
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path

import pandas as pd
import requests
import typer

from libs.cli_logging import log_run_header, setup_logger
from libs.http_retry import make_retry_session
from libs.text_normalize import clean_doi

app = typer.Typer(add_completion=False)

CROSSREF_API = "https://api.crossref.org/works"
SEMANTIC_API = "https://api.semanticscholar.org/graph/v1/paper/search"


# -------------------------
# Data model
# -------------------------
@dataclass
class PaperInfo:
    source: str
    doi: str | None
    title: str
    authors: str
    year: int | None
    venue: str | None
    abstract: str | None
    url: str | None
    published_date: str | None
    crossref_type: str | None = None
    s2_paper_id: str | None = None
    citation_count: int | None = None
    reference_count: int | None = None
    fields_of_study: str | None = None


# -------------------------
# Helpers
# -------------------------
def _norm_title(title: str) -> str:
    t = title.lower().strip()
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"[^a-z0-9 ]+", "", t)
    return t


def _clean_text(text: str) -> str:
    s = html.unescape(text or "")
    s = re.sub(r"<[^>]+>", "", s)
    return s.strip()


def _date_from_parts(parts: Iterable[int]) -> str | None:
    parts = list(parts)
    if not parts:
        return None
    try:
        y = int(parts[0])
    except Exception:
        return None
    m = int(parts[1]) if len(parts) > 1 else 1
    d = int(parts[2]) if len(parts) > 2 else 1
    return f"{y:04d}-{m:02d}-{d:02d}"


def _authors_from_crossref(item: dict) -> str:
    authors = item.get("author", []) or []
    names = []
    for a in authors:
        given = (a.get("given") or "").strip()
        family = (a.get("family") or "").strip()
        name = " ".join([given, family]).strip()
        if name:
            names.append(name)
    return ", ".join(names)


def _authors_from_semantic(item: dict) -> str:
    authors = item.get("authors", []) or []
    names = [a.get("name", "").strip() for a in authors if a.get("name")]
    return ", ".join(names)


def _pick_title(item: dict) -> str:
    title = item.get("title", "")
    if isinstance(title, list):
        return _clean_text(str(title[0]) if title else "")
    return _clean_text(str(title or ""))


def _norm_year_value(value: object) -> str:
    if value is None:
        return ""
    if hasattr(pd, "isna") and pd.isna(value):
        return ""
    if isinstance(value, bool):
        return ""
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else ""
    s = str(value).strip()
    if not s or s.lower() == "nan":
        return ""
    if s.isdigit():
        return s
    if re.fullmatch(r"\d+\.0+", s):
        return s.split(".")[0]
    return s


def _merge_sources(existing: PaperInfo, incoming: PaperInfo) -> PaperInfo:
    sources = {s.strip() for s in (existing.source + "," + incoming.source).split(",") if s.strip()}
    merged = asdict(existing)
    for k, v in asdict(incoming).items():
        if k == "source":
            continue
        if merged.get(k) in (None, "", []):
            merged[k] = v
    merged["source"] = ",".join(sorted(sources))
    return PaperInfo(**merged)


def dedupe_and_merge(papers: list[PaperInfo]) -> list[PaperInfo]:
    merged: dict[str, PaperInfo] = {}
    for p in papers:
        doi = clean_doi(p.doi)
        key = doi if doi else f"{_norm_title(p.title)}::{p.year or ''}"
        if key in merged:
            merged[key] = _merge_sources(merged[key], p)
        else:
            merged[key] = p
    out = list(merged.values())
    out.sort(key=lambda x: (x.published_date or "0000-00-00", x.title), reverse=True)
    return out


def load_pubmed_index(csv_path: Path, logger: logging.Logger) -> tuple[set[str], set[str]]:
    df = pd.read_csv(csv_path)
    doi_set: set[str] = set()
    title_year_set: set[str] = set()

    if "doi" in df.columns:
        for val in df["doi"].dropna().astype(str):
            doi = clean_doi(val)
            if doi:
                doi_set.add(doi)

    if "title" in df.columns:
        if "year" in df.columns:
            years = df["year"].fillna("")
        else:
            years = ["" for _ in range(len(df))]
        for title, year in zip(df["title"].fillna("").astype(str), years):
            y = _norm_year_value(year)
            key = f"{_norm_title(title)}::{y}"
            if key != "::":
                title_year_set.add(key)

    logger.info(f"Loaded PubMed index: DOI={len(doi_set)}, title-year={len(title_year_set)}")
    return doi_set, title_year_set


# -------------------------
# Crossref
# -------------------------
def crossref_fetch(
    query: str,
    user_agent: str,
    max_items: int,
    from_date: str | None,
    until_date: str | None,
    sleep: float,
    logger: logging.Logger,
    sess: requests.Session,
    include_abstract: bool = False,
) -> list[PaperInfo]:
    params = {"query.bibliographic": query, "rows": 100, "cursor": "*"}
    filters = []
    if from_date:
        filters.append(f"from-pub-date:{from_date}")
    if until_date:
        filters.append(f"until-pub-date:{until_date}")
    if filters:
        params["filter"] = ",".join(filters)

    headers = {"User-Agent": user_agent}
    out: list[PaperInfo] = []
    total = 0

    while True:
        r = sess.get(CROSSREF_API, params=params, headers=headers, timeout=(10, 60))
        if r.status_code >= 400:
            logger.warning(f"Crossref error: {r.status_code} {r.text[:200]}")
            break

        js = r.json()
        message = js.get("message", {}) or {}
        items = message.get("items", []) or []
        next_cursor = message.get("next-cursor")

        if not items:
            break

        for it in items:
            title = _pick_title(it).strip()
            doi = (it.get("DOI") or "").strip() or None
            venue = _pick_title({"title": it.get("container-title", [])}).strip() or None
            authors = _authors_from_crossref(it)
            issued = it.get("issued", {}).get("date-parts", [])
            published_date = _date_from_parts(issued[0]) if issued else None
            year = None
            if published_date:
                try:
                    year = int(published_date.split("-")[0])
                except Exception:
                    year = None
            abstract = it.get("abstract") if include_abstract else None
            url = it.get("URL")
            out.append(
                PaperInfo(
                    source="crossref",
                    doi=doi,
                    title=title,
                    authors=authors,
                    year=year,
                    venue=venue,
                    abstract=abstract,
                    url=url,
                    published_date=published_date,
                    crossref_type=it.get("type"),
                )
            )
            total += 1
            if total >= max_items:
                return out

        if not next_cursor:
            break
        params["cursor"] = next_cursor
        if sleep > 0:
            time.sleep(sleep)

    return out


# -------------------------
# Semantic Scholar
# -------------------------
def semantic_fetch(
    query: str,
    api_key: str | None,
    max_items: int,
    sleep: float,
    logger: logging.Logger,
    sess: requests.Session,
    include_abstract: bool = False,
) -> list[PaperInfo]:
    headers = {}
    if api_key:
        headers["x-api-key"] = api_key

    fields = [
        "title",
        "authors",
        "year",
        "venue",
        "doi",
        "url",
        "paperId",
        "publicationDate",
        "abstract",
        "citationCount",
        "referenceCount",
        "fieldsOfStudy",
    ]
    params = {"query": query, "limit": 100, "offset": 0, "fields": ",".join(fields)}

    out: list[PaperInfo] = []
    total = 0

    while True:
        r = sess.get(SEMANTIC_API, params=params, headers=headers, timeout=(10, 60))
        if r.status_code >= 400:
            logger.warning(f"Semantic Scholar error: {r.status_code} {r.text[:200]}")
            break
        js = r.json()
        items = js.get("data", []) or []
        if not items:
            break

        for it in items:
            title = _clean_text(it.get("title") or "")
            doi = (it.get("doi") or "").strip() or None
            authors = _authors_from_semantic(it)
            year = it.get("year")
            venue = it.get("venue")
            abstract = it.get("abstract") if include_abstract else None
            url = it.get("url")
            pub_date = it.get("publicationDate")
            fields = it.get("fieldsOfStudy")
            fields_str = ", ".join(fields) if isinstance(fields, list) else None
            out.append(
                PaperInfo(
                    source="semanticscholar",
                    doi=doi,
                    title=title,
                    authors=authors,
                    year=int(year) if year is not None else None,
                    venue=venue,
                    abstract=abstract,
                    url=url,
                    published_date=pub_date,
                    s2_paper_id=it.get("paperId"),
                    citation_count=it.get("citationCount"),
                    reference_count=it.get("referenceCount"),
                    fields_of_study=fields_str,
                )
            )
            total += 1
            if total >= max_items:
                return out

        params["offset"] += params["limit"]
        if sleep > 0:
            time.sleep(sleep)

    return out


# -------------------------
# Typer command
# -------------------------
@app.command()
def fetch(
    query: str = typer.Option(..., help="Search query for both Crossref and Semantic Scholar."),
    max_items: int = typer.Option(1000, min=1, help="Maximum items per source."),
    crossref: bool = typer.Option(True, "--crossref/--no-crossref", help="Enable Crossref search."),
    semantic: bool = typer.Option(True, "--semantic/--no-semantic", help="Enable Semantic Scholar search."),
    crossref_from: str | None = typer.Option(None, help="Crossref from-pub-date (YYYY-MM-DD)."),
    crossref_until: str | None = typer.Option(None, help="Crossref until-pub-date (YYYY-MM-DD)."),
    exclude_pubmed_csv: Path | None = typer.Option(
        None, help="Exclude records already present in a PubMed CSV (by DOI or title+year)."
    ),
    semantic_api_key: str | None = typer.Option(None, help="Semantic Scholar API key (optional)."),
    include_abstract: bool = typer.Option(False, help="Include abstracts where available."),
    user_agent: str = typer.Option(
        "edna-literature-fetch/1.0 (mailto:your_email@example.com)",
        help="User-Agent header for Crossref requests.",
    ),
    sleep: float = typer.Option(0.5, min=0.0, help="Sleep seconds between API pages."),
    out_prefix: str = typer.Option("crossref_semantic_results", help="Output prefix (CSV/JSON)."),
    out_dir: Path = typer.Option(Path("."), help="Output directory."),
    log_level: str = typer.Option("INFO", help="Log level: DEBUG, INFO, WARNING, ERROR"),
    log_file: Path | None = typer.Option(None, help="Write logs to this file as well."),
):
    """
    Collect paper metadata from Crossref REST API and Semantic Scholar API.
    Outputs CSV/JSON with a merged view (DOI/title-year de-dup).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    logger = setup_logger("crossref_semantic_fetch", log_level=log_level, log_file=log_file)

    params = {
        "query": query,
        "max_items": max_items,
        "crossref": crossref,
        "semantic": semantic,
        "crossref_from": crossref_from,
        "crossref_until": crossref_until,
        "exclude_pubmed_csv": str(exclude_pubmed_csv) if exclude_pubmed_csv else None,
        "semantic_api_key": "***" if semantic_api_key else None,
        "include_abstract": include_abstract,
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
        param_width=18,
        versions={
            "pandas": getattr(pd, "__version__", "unknown"),
            "requests": getattr(requests, "__version__", "unknown"),
            "typer": getattr(typer, "__version__", "unknown"),
        },
        fallback_command="python script/crossref_semantic_fetch.py",
    )

    sess = make_retry_session(retries=6, backoff_factor=1.0)
    results: list[PaperInfo] = []

    if crossref:
        logger.info("Fetching Crossref results...")
        results.extend(
            crossref_fetch(
                query=query,
                user_agent=user_agent,
                max_items=max_items,
                from_date=crossref_from,
                until_date=crossref_until,
                sleep=sleep,
                logger=logger,
                sess=sess,
                include_abstract=include_abstract,
            )
        )

    if semantic:
        logger.info("Fetching Semantic Scholar results...")
        results.extend(
            semantic_fetch(
                query=query,
                api_key=semantic_api_key,
                max_items=max_items,
                sleep=sleep,
                logger=logger,
                sess=sess,
                include_abstract=include_abstract,
            )
        )

    merged = dedupe_and_merge(results)

    if exclude_pubmed_csv:
        if not exclude_pubmed_csv.exists():
            raise typer.BadParameter(f"PubMed CSV not found: {exclude_pubmed_csv}")
        doi_set, title_year_set = load_pubmed_index(exclude_pubmed_csv, logger)
        before = len(merged)
        filtered: list[PaperInfo] = []
        for p in merged:
            doi = clean_doi(p.doi)
            key = f"{_norm_title(p.title)}::{p.year or ''}"
            if doi and doi in doi_set:
                continue
            if key in title_year_set:
                continue
            filtered.append(p)
        merged = filtered
        logger.info(f"Excluded PubMed matches: {before} -> {len(merged)}")

    df = pd.DataFrame([asdict(p) for p in merged])
    csv_path = out_dir / f"{out_prefix}.csv"
    json_path = out_dir / f"{out_prefix}.json"

    logger.info(f"Writing CSV: {csv_path}")
    df.to_csv(csv_path, index=False)
    logger.info(f"Writing JSON: {json_path}")
    json_path.write_text(json.dumps(df.to_dict(orient="records"), ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"Done. Count: {len(df)}")


if __name__ == "__main__":
    app()
