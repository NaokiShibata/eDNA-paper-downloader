from __future__ import annotations

import io
import logging
import re
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import asdict
from datetime import datetime
from functools import partial
from typing import Any

import requests
import typer
from Bio import Entrez
from tqdm import tqdm

from .edna_models import Paper
from .http_retry import make_retry_session
from .text_normalize import clean_doi, clean_term, clean_text, norm_title


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


def _clean_abstract_text(text: str) -> str:
    s = clean_text(text)
    s = re.sub(r"^\s*abstract\s*[:\-]?\s*", "", s, flags=re.IGNORECASE).strip()
    return s


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
        normalized = _clean_abstract_text(joined)
        return normalized if normalized else None
    normalized = _clean_abstract_text(str(ab))
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
    start = since_norm or "0001/01/01"
    end = until_norm or "3000/12/31"
    return f'("{start}"[{field}] : "{end}"[{field}])'


def _entrez_read_handle(handle: Any, logger: logging.Logger | None, context: str) -> Any:
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


def _entrez_request(
    read_fn: Callable[[], Any],
    logger: logging.Logger | None,
    context: str,
    retries: int = 3,
) -> Any:
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


def pubmed_search_all_pmids(
    query: str,
    email: str,
    api_key: str | None = None,
    sort: str = "most+recent",
    batch: int = 10000,
    sleep: float = 0.34,
    logger: logging.Logger | None = None,
) -> list[str]:
    Entrez.email = email  # type: ignore[assignment]
    if api_key:
        Entrez.api_key = api_key  # type: ignore[assignment]

    kwargs = {
        "db": "pubmed",
        "term": query,
        "usehistory": "y",
        "retmode": "xml",
        "retmax": 0,
        "sort": sort,
    }
    if logger:
        logger.info(f"Entrez.esearch initial (retmax=0) sort={sort}")

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

        read_page: Callable[[], Any] = partial(
            Entrez.esearch,
            db="pubmed",
            term=query,
            retmode="xml",
            retstart=retstart,
            retmax=min(batch, count - retstart),
            webenv=webenv,
            query_key=query_key,
            sort=sort,
        )

        res2 = _entrez_request(
            read_page,
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
    Entrez.email = email  # type: ignore[assignment]
    if api_key:
        Entrez.api_key = api_key  # type: ignore[assignment]

    pmids = list(pmids)
    papers: list[Paper] = []
    chunk_size = 100

    if logger:
        logger.info(f"Entrez.efetch details for {len(pmids)} PMIDs (chunk_size={chunk_size})")

    for i in tqdm(range(0, len(pmids), chunk_size), desc="Fetching PubMed details"):
        chunk = pmids[i : i + chunk_size]
        read_chunk: Callable[[], Any] = partial(
            Entrez.efetch,
            db="pubmed",
            id=",".join(chunk),
            retmode="xml",
        )
        records = _entrez_request(
            read_chunk,
            logger,
            f"efetch chunk start={i}",
        )

        for art in records.get("PubmedArticle", []):
            pmid = str(_safe_get(art, "MedlineCitation", "PMID", default="")).strip()
            title = clean_text(str(_safe_get(art, "MedlineCitation", "Article", "ArticleTitle", default="")))
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


def _parse_europepmc(js: dict[str, Any]) -> dict[str, tuple[str, str]]:
    out: dict[str, tuple[str, str]] = {}
    for item in js.get("resultList", {}).get("result", []) or []:
        doi = clean_doi(item.get("doi"))
        abstract = _clean_abstract_text(str(item.get("abstractText") or ""))
        if doi and abstract:
            out[doi] = (abstract, str(item.get("pmid") or ""))
    return out


def europepmc_fill_abstracts(
    papers: list[Paper], sleep: float = 0.2, logger: logging.Logger | None = None,
) -> list[Paper]:
    dois = list(dict.fromkeys(clean_doi(p.doi) for p in papers if p.doi and not (p.abstract or "").strip()))
    found: dict[str, tuple[str, str]] = {}
    with make_retry_session() as sess:
        for start in range(0, len(dois), 25):
            params: dict[str, Any] = {
                "query": " OR ".join(f'DOI:"{doi}"' for doi in dois[start : start + 25]),
                "resultType": "core", "format": "json", "pageSize": 100,
            }
            try:
                response = sess.get("https://www.ebi.ac.uk/europepmc/webservices/rest/search", params=params, timeout=30)
                response.raise_for_status()
                found.update(_parse_europepmc(response.json()))
            except requests.RequestException as exc:
                if logger:
                    logger.warning("Europe PMC abstract fetch failed: %s", exc)
            if sleep > 0:
                time.sleep(sleep)
    out: list[Paper] = []
    filled = 0
    for paper in papers:
        hit = found.get(clean_doi(paper.doi))
        if hit and not (paper.abstract or "").strip():
            abstract, pmid = hit
            paper = Paper(**{**asdict(paper), "abstract": abstract, "pmid": paper.pmid or pmid})
            filled += 1
        out.append(paper)
    if logger:
        logger.info("Europe PMC abstracts filled: %d", filled)
    return out


def _to_openalex_query(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\[[^\]]+\]", "", value)).strip()


def _to_query_text(value: str) -> str:
    text = re.sub(r"\[[^\]]+\]", " ", value)
    text = text.replace('"', " ")
    text = re.sub(r"[()]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _query_terms(query: str) -> list[str]:
    text = re.sub(r"\[[^\]]+\]", "", query)
    parts = re.split(r"\s+(?:OR|AND)\s+", text, flags=re.IGNORECASE)
    terms = [clean_term(part.strip().strip("()")).lower() for part in parts]
    return [term for term in terms if term]


def _to_dash_date(value: str | None) -> str | None:
    normalized = _normalize_date_str(value)
    return normalized.replace("/", "-") if normalized else None


def _expand_exclude_terms(excludes: Sequence[str]) -> list[str]:
    terms: list[str] = []
    for raw in excludes:
        if not raw or not raw.strip():
            continue
        parts = re.split(r"\s+OR\s+", raw, flags=re.IGNORECASE)
        for part in parts:
            term = clean_term(part).strip().lower()
            if term:
                terms.append(term)
    return terms


def _paper_matches_excludes(paper: Paper, excludes: Sequence[str]) -> bool:
    terms = _expand_exclude_terms(excludes)
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
    sess = make_retry_session()
    headers = {"User-Agent": user_agent}
    out: list[Paper] = []
    preprint_skipped = 0
    term_skipped = 0
    terms = _query_terms(query)

    params: dict[str, Any] = {
        "query": _to_query_text(query),
        "rows": 200,
        "cursor": "*",
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
                logger.warning(f"Crossref fetch failed (cursor={params['cursor']}): {exc}")
            break

        msg = r.json().get("message", {})
        items = msg.get("items", []) or []
        if not items:
            break

        term_matches = 0
        for item in items:
            title_list = item.get("title") or []
            title = clean_text(str(title_list[0] if title_list else ""))
            abstract = _clean_abstract_text(str(item.get("abstract") or ""))
            text = f"{title} {abstract or ''}"
            if not any(re.search(rf"\b{re.escape(term)}\b", text, re.I) for term in terms):
                term_skipped += 1
                continue
            term_matches += 1
            work_type = str(item.get("type") or "").strip().lower()
            if work_type == "posted-content":
                preprint_skipped += 1
                continue

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
                journal = clean_text(str(container[0]))

            url = str(item.get("URL") or "")
            paper = Paper(
                pmid="",
                title=title,
                journal=journal,
                year=year,
                authors=", ".join(authors),
                doi=doi or None,
                abstract=(abstract or None) if include_abstract else None,
                pubmed_url=url,
            )
            if _paper_matches_excludes(paper, excludes):
                out.append(paper)
            if len(out) >= max_items:
                break

        # ponytail: relevance-tail cutoff; page further if Crossref ranking proves unreliable
        if len(items) == params["rows"] and term_matches == 0:
            break
        if len(out) >= max_items or not msg.get("next-cursor"):
            break
        params["cursor"] = msg["next-cursor"]
        if sleep > 0:
            time.sleep(sleep)

    if logger:
        logger.info(f"Crossref preprint skipped (type=posted-content): {preprint_skipped}")
        logger.info(f"Crossref term filter dropped: {term_skipped}")
        logger.info(f"Crossref collected: {len(out)}")
        if len(out) >= max_items:
            logger.warning("Crossref results were truncated; raise --crossref-max-items")
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
    email: str | None = None,
    api_key: str | None = None,
    include_abstract: bool = False,
    excludes: Sequence[str] = (),
    sleep: float = 0.2,
    logger: logging.Logger | None = None,
) -> list[Paper]:
    try:
        import pyalex
        from pyalex import Works
    except ImportError as exc:
        raise RuntimeError("pyalex is required for OpenAlex retrieval. Install it with: pip install pyalex") from exc

    if email:
        pyalex.config.email = email
    if api_key:
        pyalex.config.api_key = api_key

    out: list[Paper] = []
    per_page = 200
    preprint_skipped = 0

    works = Works().filter(title_and_abstract={"search": _to_openalex_query(query)}).filter(type="!preprint")
    from_dash = _to_dash_date(from_date)
    until_dash = _to_dash_date(until_date)
    if from_dash:
        works = works.filter(from_publication_date=from_dash)
    if until_dash:
        works = works.filter(to_publication_date=until_dash)

    try:
        pager = works.paginate(per_page=per_page, n_max=max_items)
        for items in pager:
            if not items:
                break

            for item in items:
                work_type = str(item.get("type") or "").strip().lower()
                if work_type == "preprint":
                    preprint_skipped += 1
                    continue

                doi = clean_doi(item.get("doi"))
                title = clean_text(str(item.get("display_name") or ""))
                year = item.get("publication_year")
                try:
                    year = int(year) if year is not None else None
                except Exception:
                    year = None

                source = _safe_get(item, "primary_location", "source", "display_name", default="")
                source_name = clean_text(str(source or ""))

                authors = []
                for auth in item.get("authorships", []) or []:
                    name = _safe_get(auth, "author", "display_name", default="")
                    name = str(name).strip()
                    if name:
                        authors.append(name)

                abstract = _openalex_abstract(item.get("abstract_inverted_index")) if include_abstract else None
                if abstract:
                    abstract = _clean_abstract_text(abstract)
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
            if len(out) >= max_items:
                break

            if sleep > 0:
                time.sleep(sleep)
    except Exception as exc:
        if logger:
            logger.warning(f"OpenAlex fetch failed (pyalex): {exc}")

    if logger:
        logger.info(f"OpenAlex preprint skipped (type=preprint): {preprint_skipped}")
        logger.info(f"OpenAlex collected: {len(out)}")
        if len(out) >= max_items:
            logger.warning("OpenAlex results were truncated; raise --openalex-max-items")
    return out


def merge_papers_by_doi_title(
    papers: Sequence[Paper],
    logger: logging.Logger | None = None,
) -> list[Paper]:
    def key_of(p: Paper) -> str:
        doi = clean_doi(p.doi)
        if doi:
            return f"doi:{doi}"
        return f"title:{norm_title(p.title)}::{p.year or ''}"

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


def _parse_date(s: str) -> datetime:
    s_norm = s.strip()
    if not s_norm:
        raise ValueError("date is required (YYYY/MM/DD)")
    return datetime.strptime(s_norm, "%Y/%m/%d")


def _fmt_date(d: datetime) -> str:
    return d.strftime("%Y-%m-%d")


def keyword_filter(items: list[dict], query: str, exclude: list[str]) -> list[dict]:
    """
    Local filter: require all tokens in query to appear in title/abstract/authors/category.
    Exclude if any exclude-term appears.
    """
    q = query.strip()
    ex = [e.strip() for e in exclude if e and e.strip()]

    def hay(i: dict) -> str:
        return " ".join(
            [
                clean_text(i.get("title", "")),
                clean_text(i.get("abstract", "")),
                clean_text(i.get("category", "")),
                clean_text(i.get("authors", "")),
            ]
        ).lower()

    or_terms = [t for t in re.split(r"\s+OR\s+", q, flags=re.IGNORECASE) if t.strip()]
    if len(or_terms) > 1:
        or_terms_clean = [clean_term(t).lower() for t in or_terms]
        or_terms_clean = [t for t in or_terms_clean if t]
        out = []
        for it in items:
            h = hay(it)
            if any(e.lower() in h for e in ex):
                continue
            if or_terms_clean and not any(t in h for t in or_terms_clean):
                continue
            out.append(it)
        return out

    q_tokens = [clean_term(t).lower() for t in re.split(r"\s+", q) if t.strip()]
    q_tokens = [t for t in q_tokens if t and t not in {"or", "and"}]

    out = []
    for it in items:
        h = hay(it)
        if any(e.lower() in h for e in ex):
            continue
        if q_tokens and not all(t in h for t in q_tokens):
            continue
        out.append(it)
    return out


def _to_int_version(v: object | None) -> int:
    if v is None:
        return -1
    if isinstance(v, bool):
        return -1
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v) if v.is_integer() else -1
    s = str(v).strip()
    if not s:
        return -1
    if s.isdigit():
        return int(s)
    if re.fullmatch(r"\d+\.0+", s):
        return int(float(s))
    return -1


def fetch_range_stream(
    server: str,
    from_date: str,
    to_date: str,
    sleep: float,
    logger: logging.Logger,
    sess: requests.Session,
) -> Iterator[tuple[int, list[dict[str, Any]], list[Any]]]:
    cursor = 0
    while True:
        url = f"https://api.biorxiv.org/details/{server}/{from_date}/{to_date}/{cursor}"
        logger.debug(f"GET {url}")
        try:
            r = sess.get(url, timeout=(10, 60))
            r.raise_for_status()
        except requests.RequestException as e:
            logger.warning(f"Request failed (cursor={cursor}, range={from_date}..{to_date}): {e}. Sleep 5s then retry.")
            time.sleep(5)
            continue

        js = r.json()
        col = js.get("collection", []) or []
        messages = js.get("messages", []) or []
        yield cursor, col, messages

        try:
            total = int(messages[0]["total"])
        except (IndexError, KeyError, TypeError, ValueError):
            total = None
        cursor += len(col)
        if not col or (total is not None and cursor >= total):
            break
        if sleep > 0:
            time.sleep(sleep)


def biorxiv_search_papers(
    server: str, from_date: str, to_date: str, query: str, excludes: list[str],
    sleep: float, logger: logging.Logger, include_abstract: bool = False,
) -> list[Paper]:
    if server not in {"biorxiv", "medrxiv"}:
        raise typer.BadParameter("server must be biorxiv or medrxiv")
    start = _parse_date(from_date.replace("-", "/"))
    end = _parse_date(to_date.replace("-", "/"))
    if start > end:
        raise typer.BadParameter("bioRxiv start date must not be after end date")
    best: dict[str, tuple[tuple[int, str], Paper]] = {}
    exclude_terms = [clean_term(term) for value in excludes for term in re.split(r"\s+OR\s+", value, flags=re.IGNORECASE)]
    with make_retry_session() as sess:
        for _, items, _ in fetch_range_stream(server, _fmt_date(start), _fmt_date(end), sleep, logger, sess):
            for item in keyword_filter(items, query, exclude_terms):
                doi = item.get("doi", "") or ""
                version = item.get("version")
                version_str = str(version) if version is not None else None
                url = f"https://www.{server}.org/content/{doi}" if doi else ""
                if doi and version_str:
                    url += f"v{version_str}"
                title = clean_text(item.get("title", ""))
                posted = clean_text(item.get("date", ""))
                key = doi.strip().lower() or f"__no_doi__::{url}::{title}".lower()
                rank = (_to_int_version(version_str), posted or "0000-00-00")
                if key in best and rank <= best[key][0]:
                    continue
                best[key] = (rank, Paper(
                    pmid="", title=title, journal="bioRxiv" if server == "biorxiv" else "medRxiv",
                    year=int(posted[:4]) if posted else None, authors=clean_text(item.get("authors", "")),
                    doi=doi or None,
                    abstract=(clean_text(item.get("abstract", "")) or None) if include_abstract else None,
                    pubmed_url=url,
                ))
    papers = [paper for rank, paper in sorted(
        best.values(), key=lambda row: (row[0][1], row[1].doi or ""), reverse=True,
    )]
    logger.info("%s collected: %d", server, len(papers))
    return papers
