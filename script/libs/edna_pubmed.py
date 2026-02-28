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
from .text_normalize import clean_doi


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


def _norm_title(title: str) -> str:
    t = title.lower().strip()
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"[^a-z0-9 ]+", "", t)
    return t


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


def _to_query_text(value: str) -> str:
    text = re.sub(r"\[[^\]]+\]", " ", value)
    text = text.replace('"', " ")
    text = re.sub(r"[()]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _to_dash_date(value: str | None) -> str | None:
    normalized = _normalize_date_str(value)
    return normalized.replace("/", "-") if normalized else None


def _paper_matches_excludes(paper: Paper, excludes: Sequence[str]) -> bool:
    terms = [t.strip().lower() for t in excludes if t and t.strip()]
    if not terms:
        return True
    hay = " ".join(
        [
            paper.title or "",
            paper.abstract or "",
            paper.journal or "",
            paper.authors or "",
        ]
    ).lower()
    return not any(term in hay for term in terms)


def crossref_search_papers(
    query: str,
    user_agent: str,
    max_items: int = 1000,
    from_date: str | None = None,
    until_date: str | None = None,
    include_abstract: bool = False,
    excludes: Sequence[str] = (),
    sleep: float = 0.2,
    logger: logging.Logger | None = None,
) -> list[Paper]:
    sess = requests.Session()
    headers = {"User-Agent": user_agent}
    out: list[Paper] = []

    params: dict[str, object] = {
        "query": _to_query_text(query),
        "rows": 200,
        "offset": 0,
    }
    filters: list[str] = []
    from_dash = _to_dash_date(from_date)
    until_dash = _to_dash_date(until_date)
    if from_dash:
        filters.append(f"from-pub-date:{from_dash}")
    if until_dash:
        filters.append(f"until-pub-date:{until_dash}")
    if filters:
        params["filter"] = ",".join(filters)

    while len(out) < max_items:
        try:
            r = sess.get(
                "https://api.crossref.org/works",
                params=params,
                headers=headers,
                timeout=30,
            )
            r.raise_for_status()
        except requests.RequestException as exc:
            if logger:
                logger.warning(f"Crossref fetch failed (offset={params['offset']}): {exc}")
            break

        msg = r.json().get("message", {})
        items = msg.get("items", []) or []
        if not items:
            break

        for item in items:
            title_list = item.get("title") or []
            title = _clean_text(str(title_list[0] if title_list else ""))
            doi = clean_doi(item.get("DOI"))
            year_parts = (
                _safe_get(item, "published-print", "date-parts", default=[])
                or _safe_get(item, "published-online", "date-parts", default=[])
                or _safe_get(item, "issued", "date-parts", default=[])
            )
            year = None
            if isinstance(year_parts, list) and year_parts and isinstance(year_parts[0], list) and year_parts[0]:
                try:
                    year = int(year_parts[0][0])
                except Exception:
                    year = None

            authors = []
            for a in item.get("author", []) or []:
                given = str(a.get("given") or "").strip()
                family = str(a.get("family") or "").strip()
                name = " ".join([given, family]).strip()
                if name:
                    authors.append(name)

            journal = ""
            container = item.get("container-title") or []
            if container:
                journal = _clean_text(str(container[0]))

            abstract = _clean_text(str(item.get("abstract") or "")) if include_abstract else None
            url = str(item.get("URL") or "")
            paper = Paper(
                pmid="",
                title=title,
                journal=journal,
                year=year,
                authors=", ".join(authors),
                doi=doi or None,
                abstract=abstract or None,
                pubmed_url=url,
            )
            if _paper_matches_excludes(paper, excludes):
                out.append(paper)
            if len(out) >= max_items:
                break

        if len(items) < int(params["rows"]):
            break
        params["offset"] = int(params["offset"]) + int(params["rows"])
        if sleep > 0:
            time.sleep(sleep)

    if logger:
        logger.info(f"Crossref collected: {len(out)}")
    return out


def _openalex_abstract(inv: dict[str, list[int]] | None) -> str | None:
    if not inv:
        return None
    pos_to_word: dict[int, str] = {}
    for word, positions in inv.items():
        for pos in positions:
            pos_to_word[pos] = word
    if not pos_to_word:
        return None
    text = " ".join(pos_to_word[i] for i in sorted(pos_to_word.keys()))
    normalized = re.sub(r"\s+", " ", text).strip()
    return normalized or None


def openalex_search_papers(
    query: str,
    max_items: int = 1000,
    from_date: str | None = None,
    until_date: str | None = None,
    include_abstract: bool = False,
    excludes: Sequence[str] = (),
    sleep: float = 0.2,
    logger: logging.Logger | None = None,
) -> list[Paper]:
    sess = requests.Session()
    out: list[Paper] = []
    per_page = 200
    page = 1

    filters: list[str] = []
    from_dash = _to_dash_date(from_date)
    until_dash = _to_dash_date(until_date)
    if from_dash:
        filters.append(f"from_publication_date:{from_dash}")
    if until_dash:
        filters.append(f"to_publication_date:{until_dash}")

    while len(out) < max_items:
        params: dict[str, object] = {
            "search": _to_query_text(query),
            "per-page": per_page,
            "page": page,
        }
        if filters:
            params["filter"] = ",".join(filters)
        try:
            r = sess.get("https://api.openalex.org/works", params=params, timeout=30)
            r.raise_for_status()
        except requests.RequestException as exc:
            if logger:
                logger.warning(f"OpenAlex fetch failed (page={page}): {exc}")
            break

        js = r.json()
        items = js.get("results", []) or []
        if not items:
            break

        for item in items:
            doi = clean_doi(item.get("doi"))
            title = _clean_text(str(item.get("display_name") or ""))
            year = item.get("publication_year")
            try:
                year = int(year) if year is not None else None
            except Exception:
                year = None

            source = _safe_get(item, "primary_location", "source", "display_name", default="")
            source_name = _clean_text(str(source or ""))

            authors = []
            for auth in item.get("authorships", []) or []:
                name = _safe_get(auth, "author", "display_name", default="")
                name = str(name).strip()
                if name:
                    authors.append(name)

            abstract = _openalex_abstract(item.get("abstract_inverted_index")) if include_abstract else None
            url = str(item.get("id") or "")
            paper = Paper(
                pmid="",
                title=title,
                journal=source_name,
                year=year,
                authors=", ".join(authors),
                doi=doi or None,
                abstract=abstract,
                pubmed_url=url,
            )
            if _paper_matches_excludes(paper, excludes):
                out.append(paper)
            if len(out) >= max_items:
                break

        if len(items) < per_page:
            break
        page += 1
        if sleep > 0:
            time.sleep(sleep)

    if logger:
        logger.info(f"OpenAlex collected: {len(out)}")
    return out


def merge_papers_by_doi_title(
    papers: Sequence[Paper],
    logger: logging.Logger | None = None,
) -> list[Paper]:
    def key_of(p: Paper) -> str:
        doi = clean_doi(p.doi)
        if doi:
            return f"doi:{doi}"
        return f"title:{_norm_title(p.title)}::{p.year or ''}"

    def score_of(p: Paper) -> tuple[int, int, int, int, int]:
        return (
            1 if p.pmid else 0,
            1 if p.abstract else 0,
            1 if p.authors else 0,
            1 if p.journal else 0,
            p.year or 0,
        )

    merged: dict[str, Paper] = {}
    for p in papers:
        k = key_of(p)
        cur = merged.get(k)
        if cur is None:
            merged[k] = p
            continue

        best = p if score_of(p) > score_of(cur) else cur
        other = cur if best is p else p
        merged[k] = Paper(
            pmid=best.pmid or other.pmid,
            title=best.title or other.title,
            journal=best.journal or other.journal,
            year=best.year or other.year,
            authors=best.authors or other.authors,
            doi=clean_doi(best.doi) or clean_doi(other.doi) or None,
            abstract=best.abstract or other.abstract,
            pubmed_url=best.pubmed_url or other.pubmed_url,
        )

    out = list(merged.values())
    out.sort(key=lambda x: (x.year or 0, _pmid_key(x.pmid)), reverse=True)
    if logger:
        logger.info(f"Merged papers (doi/title): {len(papers)} -> {len(out)}")
    return out
