from __future__ import annotations

import json
import logging
import platform
import re
import socket
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional, Sequence

import pandas as pd
import requests
import typer
from Bio import Entrez
from tqdm import tqdm

app = typer.Typer(add_completion=False)


# -------------------------
# Logging utilities
# -------------------------
def setup_logger(log_level: str = "INFO", log_file: Optional[Path] = None) -> logging.Logger:
    logger = logging.getLogger("edna_literature_fetch")
    logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))
    logger.propagate = False

    if logger.handlers:
        logger.handlers.clear()

    fmt = logging.Formatter(
        fmt="%(asctime)s\t%(levelname)s\t%(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    sh.setLevel(logger.level)
    logger.addHandler(sh)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        fh.setLevel(logger.level)
        logger.addHandler(fh)

    return logger


def log_run_header(logger: logging.Logger, params: dict, log_file: Optional[Path] = None) -> None:
    try:
        import getpass

        user = getpass.getuser()
    except Exception:
        user = "unknown"

    header_lines = [
        "=" * 80,
        "RUN HEADER",
        f"timestamp   : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"user        : {user}",
        f"hostname    : {socket.gethostname()}",
        f"platform    : {platform.platform()}",
        f"python      : {sys.version.split()[0]}",
        f"cwd         : {Path.cwd()}",
        f"command     : {' '.join(sys.argv)}",
        f"log_file    : {str(log_file) if log_file else '(console only)'}",
        "-" * 80,
        "PARAMETERS",
    ]
    for k in sorted(params.keys()):
        header_lines.append(f"{k:16s}: {params[k]}")
    header_lines += [
        "-" * 80,
        "VERSIONS",
        f"typer       : {getattr(typer, '__version__', 'unknown')}",
        f"pandas      : {getattr(pd, '__version__', 'unknown')}",
        f"requests    : {getattr(requests, '__version__', 'unknown')}",
        f"biopython   : {getattr(sys.modules.get('Bio'), '__version__', 'unknown')}",
        f"tqdm        : {getattr(tqdm, '__version__', 'unknown')}",
        "=" * 80,
    ]
    for line in header_lines:
        logger.info(line)


# -------------------------
# Data model
# -------------------------
@dataclass
class Paper:
    pmid: str
    title: str
    journal: str
    year: Optional[int]
    authors: str
    doi: Optional[str]
    abstract: Optional[str]
    pubmed_url: str


# -------------------------
# Helpers
# -------------------------
def _safe_get(dct, *keys, default=None):
    cur = dct
    for k in keys:
        if cur is None:
            return default
        if isinstance(cur, dict):
            cur = cur.get(k)
        else:
            return default
    return cur if cur is not None else default


def _norm_title(title: str) -> str:
    t = title.lower().strip()
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"[^a-z0-9 ]+", "", t)
    return t


def _extract_doi(article: dict) -> Optional[str]:
    eloc = _safe_get(article, "MedlineCitation", "Article", "ELocationID", default=None)
    if isinstance(eloc, list):
        for e in eloc:
            if getattr(e, "attributes", {}).get("EIdType") == "doi":
                return str(e)
    elif eloc is not None:
        if getattr(eloc, "attributes", {}).get("EIdType") == "doi":
            return str(eloc)

    aid_list = _safe_get(article, "PubmedData", "ArticleIdList", default=None)
    if isinstance(aid_list, list):
        for aid in aid_list:
            if getattr(aid, "attributes", {}).get("IdType") == "doi":
                return str(aid)

    return None


def _extract_year(article: dict) -> Optional[int]:
    ad = _safe_get(article, "MedlineCitation", "Article", "ArticleDate", default=None)
    if isinstance(ad, list) and ad:
        y = _safe_get(ad[0], "Year", default=None)
        try:
            return int(y)
        except Exception:
            pass

    pub_date = _safe_get(article, "MedlineCitation", "Article", "Journal", "JournalIssue", "PubDate", default=None)
    for key in ("Year", "MedlineDate"):
        v = _safe_get(pub_date, key, default=None)
        if not v:
            continue
        m = re.search(r"(19|20)\d{2}", str(v))
        if m:
            return int(m.group(0))
    return None


def _extract_authors(article: dict) -> str:
    al = _safe_get(article, "MedlineCitation", "Article", "AuthorList", default=[])
    authors = []
    if isinstance(al, list):
        for a in al:
            last = _safe_get(a, "LastName", default="")
            fore = _safe_get(a, "ForeName", default="")
            coll = _safe_get(a, "CollectiveName", default="")
            if coll:
                authors.append(str(coll))
            else:
                name = " ".join([str(fore).strip(), str(last).strip()]).strip()
                if name:
                    authors.append(name)
    return ", ".join(authors)


def _extract_abstract(article: dict) -> Optional[str]:
    ab = _safe_get(article, "MedlineCitation", "Article", "Abstract", "AbstractText", default=None)
    if ab is None:
        return None
    if isinstance(ab, list):
        parts = [str(x).strip() for x in ab if str(x).strip()]
        joined = "\n".join(parts).strip()
        return joined if joined else None
    s = str(ab).strip()
    return s if s else None




def build_query_with_excludes(base_query: str, excludes: Sequence[str]) -> str:
    ex = [e.strip() for e in excludes if e and e.strip()]
    if not ex:
        return base_query.strip()
    ex_clause = " OR ".join(ex)
    return f"({base_query.strip()}) NOT ({ex_clause})"


def _pmid_key(pmid: str) -> int:
    try:
        return int(pmid)
    except Exception:
        return 0


def keep_latest_per_doi_pubmed(papers: list[Paper], logger: Optional[logging.Logger] = None) -> list[Paper]:
    """
    PubMedは“バージョン”概念が薄いので、同一DOIが複数件ある場合だけ
    (year, PMID) が新しい方を採用する。
    DOIが無いものは PMID 単位で残す。
    """
    best: dict[str, Paper] = {}
    no_doi: list[Paper] = []

    for p in papers:
        doi = (p.doi or "").strip().lower()
        if not doi:
            no_doi.append(p)
            continue

        cur = best.get(doi)
        if cur is None:
            best[doi] = p
            continue

        p_key = (p.year or 0, _pmid_key(p.pmid))
        c_key = (cur.year or 0, _pmid_key(cur.pmid))
        if p_key > c_key:
            best[doi] = p

    out = list(best.values()) + no_doi
    # 並べ替え：年・PMIDの新しい順
    out.sort(
        key=lambda x: (x.year or 0, _pmid_key(x.pmid)),
        reverse=True,
    )
    if logger:
        logger.info(f"DOI latest-select (PubMed): {len(papers)} -> {len(out)}")
    return out


# -------------------------
# PubMed retrieval
# -------------------------
def pubmed_search_all_pmids(
    query: str,
    email: str,
    api_key: Optional[str] = None,
    mindate: Optional[str] = None,
    maxdate: Optional[str] = None,
    datetype: str = "pdat",
    sort: str = "most+recent",
    batch: int = 10000,
    sleep: float = 0.34,
    logger: Optional[logging.Logger] = None,
) -> list[str]:
    """
    Get PMIDs for query by using Entrez history (usehistory=y) and retstart pagination.
    """
    Entrez.email = email
    if api_key:
        Entrez.api_key = api_key

    kwargs = {
        "db": "pubmed",
        "term": query,
        "usehistory": "y",
        "retmode": "xml",
        "retmax": 0,
        "sort": sort,
    }
    if mindate or maxdate:
        kwargs.update({"datetype": datetype})
        if mindate:
            kwargs["mindate"] = mindate
        if maxdate:
            kwargs["maxdate"] = maxdate

    if logger:
        logger.info(f"Entrez.esearch initial (retmax=0) sort={sort} datetype={datetype}")

    h = Entrez.esearch(**kwargs)
    res = Entrez.read(h)
    h.close()

    count = int(res.get("Count", "0"))
    if logger:
        logger.info(f"PubMed hit count: {count}")

    if count == 0:
        return []

    webenv = res["WebEnv"]
    query_key = res["QueryKey"]

    pmids: list[str] = []
    for retstart in tqdm(range(0, count, batch), desc=f"Collecting PMIDs (total={count})"):
        if logger and logger.isEnabledFor(logging.DEBUG):
            logger.debug(f"Paging PMIDs retstart={retstart} retmax={min(batch, count - retstart)}")

        h2 = Entrez.esearch(
            db="pubmed",
            term=query,
            usehistory="y",
            retmode="xml",
            retstart=retstart,
            retmax=min(batch, count - retstart),
            webenv=webenv,
            query_key=query_key,
            sort=sort,
        )
        res2 = Entrez.read(h2)
        h2.close()
        pmids.extend(list(res2.get("IdList", [])))

        if sleep > 0:
            time.sleep(sleep)

    # de-dup preserve order
    seen = set()
    uniq = []
    for p in pmids:
        if p in seen:
            continue
        seen.add(p)
        uniq.append(p)

    if logger:
        logger.info(f"PMIDs collected: {len(uniq)} (deduplicated)")
    return uniq


def pubmed_fetch_details(
    pmids: Iterable[str],
    email: str,
    api_key: Optional[str] = None,
    include_abstract: bool = False,
    sleep: float = 0.34,
    logger: Optional[logging.Logger] = None,
) -> list[Paper]:
    Entrez.email = email
    if api_key:
        Entrez.api_key = api_key

    pmids = list(pmids)
    papers: list[Paper] = []
    chunk_size = 100

    if logger:
        logger.info(f"Entrez.efetch details for {len(pmids)} PMIDs (chunk_size={chunk_size})")

    for i in tqdm(range(0, len(pmids), chunk_size), desc="Fetching PubMed details"):
        chunk = pmids[i : i + chunk_size]
        h = Entrez.efetch(db="pubmed", id=",".join(chunk), retmode="xml")
        records = Entrez.read(h)
        h.close()

        for art in records.get("PubmedArticle", []):
            pmid = str(_safe_get(art, "MedlineCitation", "PMID", default="")).strip()
            title = str(_safe_get(art, "MedlineCitation", "Article", "ArticleTitle", default="")).strip()
            journal = str(_safe_get(art, "MedlineCitation", "Article", "Journal", "Title", default="")).strip()
            year = _extract_year(art)
            authors = _extract_authors(art)
            doi = _extract_doi(art)
            abstract = _extract_abstract(art) if include_abstract else None
            url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else ""

            papers.append(
                Paper(
                    pmid=pmid,
                    title=title,
                    journal=journal,
                    year=year,
                    authors=authors,
                    doi=doi,
                    abstract=abstract,
                    pubmed_url=url,
                )
            )

        if sleep > 0:
            time.sleep(sleep)

    if logger:
        logger.info(f"Records fetched: {len(papers)}")
    return papers


def crossref_fill_missing_doi(
    papers: list[Paper],
    user_agent: str,
    sleep: float = 0.2,
    logger: Optional[logging.Logger] = None,
) -> list[Paper]:
    sess = requests.Session()
    headers = {"User-Agent": user_agent}
    out: list[Paper] = []

    if logger:
        logger.info("Crossref DOI fill enabled (heuristic)")

    for p in tqdm(papers, desc="Crossref DOI fill"):
        if p.doi or not p.title:
            out.append(p)
            continue
        params = {"query.title": p.title, "rows": 1}
        try:
            r = sess.get("https://api.crossref.org/works", params=params, headers=headers, timeout=20)
            r.raise_for_status()
            js = r.json()
            items = js.get("message", {}).get("items", [])
            doi = items[0].get("DOI") if items else None
            out.append(Paper(**{**asdict(p), "doi": doi}))
        except Exception as e:
            if logger and logger.isEnabledFor(logging.DEBUG):
                logger.debug(f"Crossref lookup failed for title={p.title!r}: {e}")
            out.append(p)

        if sleep > 0:
            time.sleep(sleep)

    return out


# -------------------------
# Typer command
# -------------------------
@app.command()
def fetch(
    email: str = typer.Option(..., help="Your email for NCBI Entrez."),
    api_key: Optional[str] = typer.Option(None, help="NCBI API key (optional)."),
    query: str = typer.Option(
        '("environmental DNA"[Title/Abstract] OR eDNA[Title/Abstract])',
        help="Base PubMed query string.",
    ),
    exclude: Optional[list[str]] = typer.Option(
        None,
        "--exclude",
        help="Exclude term(s). Can be repeated. Example: --exclude review --exclude '\"meta-analysis\"'",
    ),
    since: Optional[str] = typer.Option(
        None,
        "--since",
        help="Start date (YYYY/MM/DD) for date filter.",
    ),
    until: Optional[str] = typer.Option(
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
    log_file: Optional[Path] = typer.Option(None, help="Write logs to this file as well."),
):
    """
    Fetch paper metadata from PubMed and export CSV/JSON.
    If DOI duplicates occur, keeps the record with newest (year, PMID).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    logger = setup_logger(log_level=log_level, log_file=log_file)

    exclude_terms = exclude or []
    final_query = build_query_with_excludes(query, exclude_terms)

    params = {
        "email": email,
        "api_key": "***" if api_key else None,
        "query": query,
        "exclude": exclude_terms,
        "final_query": final_query,
        "since": since,
        "until": until,
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
    log_run_header(logger, params=params, log_file=log_file)

    pmids = pubmed_search_all_pmids(
        query=final_query,
        email=email,
        api_key=api_key,
        mindate=since,
        maxdate=until,
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

    # DOI duplicates -> keep newest record
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
