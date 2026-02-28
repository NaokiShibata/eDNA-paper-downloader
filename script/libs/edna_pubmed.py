from __future__ import annotations

import html
import io
import logging
import re
import time
from collections.abc import Iterable, Sequence
from dataclasses import asdict

import requests
from Bio import Entrez
from tqdm import tqdm

from .edna_models import Paper


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


def _clean_text(text: str) -> str:
    s = html.unescape(text or "")
    s = re.sub(r"<[^>]+>", "", s)
    return s.strip()


def _extract_doi(article: dict) -> str | None:
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


def _extract_year(article: dict) -> int | None:
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


def _extract_abstract(article: dict) -> str | None:
    ab = _safe_get(article, "MedlineCitation", "Article", "Abstract", "AbstractText", default=None)
    if ab is None:
        return None
    if isinstance(ab, list):
        parts = [str(x).strip() for x in ab if str(x).strip()]
        joined = " ".join(parts).strip()
        normalized = re.sub(r"\s+", " ", joined).strip()
        return normalized if normalized else None
    s = str(ab).strip()
    normalized = re.sub(r"\s+", " ", s).strip()
    return normalized if normalized else None


def build_query_with_excludes(base_query: str, excludes: Sequence[str]) -> str:
    ex = [e.strip() for e in excludes if e and e.strip()]
    if not ex:
        return base_query.strip()
    ex_clause = " OR ".join(ex)
    return f"({base_query.strip()}) NOT ({ex_clause})"


def _normalize_date_str(value: str | None) -> str | None:
    if value is None:
        return None
    s = value.strip()
    if not s:
        return None
    return s.replace("-", "/")


def _date_range_clause(since: str | None, until: str | None, datetype: str) -> str | None:
    since_norm = _normalize_date_str(since)
    until_norm = _normalize_date_str(until)
    if not since_norm and not until_norm:
        return None
    field = datetype.upper()
    if field not in ("PDAT", "EDAT"):
        field = field.upper()
    start = since_norm or "0001/01/01"
    end = until_norm or "3000/12/31"
    return f'("{start}"[{field}] : "{end}"[{field}])'


def _entrez_read_handle(handle, logger: logging.Logger | None, context: str):
    raw = b""
    try:
        raw = handle.read()
    finally:
        handle.close()
    if isinstance(raw, str):
        raw_bytes = raw.encode("utf-8", errors="replace")
    elif isinstance(raw, (bytes, bytearray, memoryview)):
        raw_bytes = bytes(raw)
    else:
        raw_bytes = str(raw).encode("utf-8", errors="replace")
    try:
        return Entrez.read(io.BytesIO(raw_bytes))
    except Exception as exc:
        if logger:
            logger.warning(f"Entrez parse failed ({context}): {exc}")
            if logger.isEnabledFor(logging.DEBUG):
                snippet = raw_bytes[:500].decode("utf-8", errors="replace").replace("\n", "\\n")
                logger.debug(f"Entrez raw head ({context}): {snippet}")
        raise


def _entrez_request(read_fn, logger: logging.Logger | None, context: str, retries: int = 3):
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            handle = read_fn()
            return _entrez_read_handle(handle, logger, context)
        except Exception as exc:
            last_exc = exc
            if logger and attempt < retries:
                logger.warning(f"Retrying Entrez request ({context}) attempt {attempt}/{retries}")
            if attempt < retries:
                time.sleep(0.5 * attempt)
                continue
            raise
    if last_exc:
        raise last_exc


def _pmid_key(pmid: str) -> int:
    try:
        return int(pmid)
    except Exception:
        return 0


def keep_latest_per_doi_pubmed(papers: list[Paper], logger: logging.Logger | None = None) -> list[Paper]:
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
    out.sort(
        key=lambda x: (x.year or 0, _pmid_key(x.pmid)),
        reverse=True,
    )
    if logger:
        logger.info(f"DOI latest-select (PubMed): {len(papers)} -> {len(out)}")
    return out


def pubmed_search_all_pmids(
    query: str,
    email: str,
    api_key: str | None = None,
    mindate: str | None = None,
    maxdate: str | None = None,
    datetype: str = "pdat",
    sort: str = "most+recent",
    batch: int = 10000,
    sleep: float = 0.34,
    logger: logging.Logger | None = None,
) -> list[str]:
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

    res = _entrez_request(lambda: Entrez.esearch(**kwargs), logger, "esearch initial")

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

        kwargs_page = {}
        if mindate or maxdate:
            kwargs_page.update({"datetype": datetype})
            if mindate:
                kwargs_page["mindate"] = mindate
            if maxdate:
                kwargs_page["maxdate"] = maxdate

        res2 = _entrez_request(
            lambda: Entrez.esearch(
                db="pubmed",
                term=query,
                retmode="xml",
                retstart=retstart,
                retmax=min(batch, count - retstart),
                webenv=webenv,
                query_key=query_key,
                sort=sort,
                **kwargs_page,
            ),
            logger,
            f"esearch page retstart={retstart}",
        )
        pmids.extend(list(res2.get("IdList", [])))

        if sleep > 0:
            time.sleep(sleep)

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
    api_key: str | None = None,
    include_abstract: bool = False,
    sleep: float = 0.34,
    logger: logging.Logger | None = None,
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
        records = _entrez_request(
            lambda: Entrez.efetch(db="pubmed", id=",".join(chunk), retmode="xml"),
            logger,
            f"efetch chunk start={i}",
        )

        for art in records.get("PubmedArticle", []):
            pmid = str(_safe_get(art, "MedlineCitation", "PMID", default="")).strip()
            title = _clean_text(str(_safe_get(art, "MedlineCitation", "Article", "ArticleTitle", default="")))
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
    logger: logging.Logger | None = None,
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
