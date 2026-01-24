from __future__ import annotations

import html
import json
import logging
import platform
import re
import socket
import sys
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd
import requests
import typer
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

app = typer.Typer(add_completion=False)

API_BASE = "https://api.biorxiv.org/details"


# -------------------------
# Logging utilities
# -------------------------
def setup_logger(log_level: str = "INFO", log_file: Optional[Path] = None) -> logging.Logger:
    logger = logging.getLogger("biorxiv_search")
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
        header_lines.append(f"{k:18s}: {params[k]}")
    header_lines += [
        "-" * 80,
        "VERSIONS",
        f"typer       : {getattr(typer, '__version__', 'unknown')}",
        f"pandas      : {getattr(pd, '__version__', 'unknown')}",
        f"requests    : {getattr(requests, '__version__', 'unknown')}",
        "=" * 80,
    ]
    for line in header_lines:
        logger.info(line)


# -------------------------
# Robust requests session
# -------------------------
def make_retry_session(
    retries: int = 6,
    backoff_factor: float = 1.0,
    status_forcelist: tuple[int, ...] = (429, 500, 502, 503, 504),
) -> requests.Session:
    """
    Retry-friendly session to reduce RemoteDisconnected/connection resets.
    """
    sess = requests.Session()
    retry = Retry(
        total=retries,
        connect=retries,
        read=retries,
        status=retries,
        backoff_factor=backoff_factor,
        status_forcelist=status_forcelist,
        allowed_methods=frozenset(["GET", "HEAD"]),
        raise_on_status=False,
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)
    sess.mount("http://", adapter)
    sess.mount("https://", adapter)
    return sess


# -------------------------
# Data model
# -------------------------
@dataclass
class Preprint:
    server: str
    doi: str
    title: str
    authors: str
    date: str  # API "date" (posted date)
    category: str
    abstract: Optional[str]
    version: Optional[str]
    biorxiv_url: str

    posted_date: Optional[str] = None
    retrieved_at: Optional[str] = None


# -------------------------
# Helpers
# -------------------------
def _norm(s: str) -> str:
    cleaned = html.unescape(s or "")
    cleaned = re.sub(r"<[^>]+>", "", cleaned)
    return re.sub(r"\s+", " ", cleaned.strip())


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _fmt_date(d: date) -> str:
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
                _norm(i.get("title", "")),
                _norm(i.get("abstract", "")),
                _norm(i.get("category", "")),
                _norm(i.get("authors", "")),
            ]
        ).lower()

    q_tokens = [t.lower() for t in re.split(r"\s+", q) if t.strip()]

    out = []
    for it in items:
        h = hay(it)
        if any(e.lower() in h for e in ex):
            continue
        if q_tokens and not all(t in h for t in q_tokens):
            continue
        out.append(it)
    return out


def item_to_preprint(server: str, it: dict, retrieved_at: str) -> Preprint:
    doi = it.get("doi", "") or ""
    ver = it.get("version")
    ver_str = str(ver) if ver is not None else None
    url = (
        f"https://www.biorxiv.org/content/{doi}v{ver_str}"
        if doi and ver_str
        else (f"https://www.biorxiv.org/content/{doi}" if doi else "")
    )
    posted = _norm(it.get("date", "")) or None
    return Preprint(
        server=server,
        doi=doi,
        title=_norm(it.get("title", "")),
        authors=_norm(it.get("authors", "")),
        date=_norm(it.get("date", "")),
        category=_norm(it.get("category", "")),
        abstract=_norm(it.get("abstract", "")) or None,
        version=ver_str,
        biorxiv_url=url,
        posted_date=posted,
        retrieved_at=retrieved_at,
    )


def _to_int_version(v: Optional[object]) -> int:
    if v is None:
        return -1
    if hasattr(pd, "isna") and pd.isna(v):
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


def _date_key(d: Optional[str]) -> str:
    return d or "0000-00-00"


def keep_latest_version_per_doi(rows: list[Preprint]) -> list[Preprint]:
    """
    DOIごとに最新版(v最大)だけ残す。
    同率/欠損は posted_date/date が新しい方を採用。
    DOI無しは URL+title で擬似キー化（重複排除の最小限）。
    """
    best: dict[str, Preprint] = {}
    for r in rows:
        doi = (r.doi or "").strip().lower()
        if not doi:
            doi = f"__no_doi__::{r.biorxiv_url}::{r.title}".lower()

        cur = best.get(doi)
        if cur is None:
            best[doi] = r
            continue

        rv = _to_int_version(r.version)
        cv = _to_int_version(cur.version)

        if rv > cv:
            best[doi] = r
        elif rv == cv:
            if _date_key(r.posted_date or r.date) > _date_key(cur.posted_date or cur.date):
                best[doi] = r

    out = list(best.values())
    out.sort(key=lambda x: (_date_key(x.posted_date or x.date), x.doi), reverse=True)
    return out


def write_outputs_overwrite(out_dir: Path, out_prefix: str, rows: list[Preprint], logger: logging.Logger) -> None:
    df = pd.DataFrame([asdict(r) for r in rows])
    if not df.empty:
        df = df.sort_values(["posted_date", "doi"], ascending=[False, True])

    csv_path = out_dir / f"{out_prefix}.csv"
    json_path = out_dir / f"{out_prefix}.json"

    logger.debug(f"Overwriting outputs: {csv_path}, {json_path} (rows={len(df)})")
    df.to_csv(csv_path, index=False)
    json_path.write_text(json.dumps(df.to_dict(orient="records"), ensure_ascii=False, indent=2), encoding="utf-8")


def write_delta_outputs(out_dir: Path, out_prefix: str, delta_rows: list[Preprint], logger: logging.Logger) -> None:
    df = pd.DataFrame([asdict(r) for r in delta_rows])
    if not df.empty:
        df = df.sort_values(["posted_date", "doi"], ascending=[False, True])

    csv_path = out_dir / f"{out_prefix}.delta.csv"
    json_path = out_dir / f"{out_prefix}.delta.json"
    logger.info(f"Writing DELTA CSV: {csv_path}")
    df.to_csv(csv_path, index=False)
    logger.info(f"Writing DELTA JSON: {json_path}")
    json_path.write_text(json.dumps(df.to_dict(orient="records"), ensure_ascii=False, indent=2), encoding="utf-8")


def debug_log_hits(logger: logging.Logger, rows: list[Preprint], max_items: int, title: str = "HITS") -> None:
    if not logger.isEnabledFor(logging.DEBUG):
        return
    logger.debug(f"DEBUG {title} DUMP (showing up to {max_items} / total {len(rows)})")
    for i, r in enumerate(rows[:max_items], start=1):
        logger.debug(f"[{i}] {r.posted_date or r.date}\t{r.doi}\tv{r.version}\t{r.category}\t{r.title}")


def fetch_range_stream(
    server: str,
    from_date: str,
    to_date: str,
    sleep: float,
    logger: logging.Logger,
    sess: requests.Session,
):
    cursor = 0
    while True:
        url = f"{API_BASE}/{server}/{from_date}/{to_date}/{cursor}"
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
        yield cursor, col, js.get("messages", [])

        if len(col) < 100:
            break
        cursor += 100
        if sleep > 0:
            time.sleep(sleep)


# -------------------------
# Batch range splitters
# -------------------------
def iter_weekly_ranges(start: date, end: date) -> Iterable[tuple[date, date]]:
    cur = start
    while cur <= end:
        nxt = min(cur + timedelta(days=6), end)
        yield cur, nxt
        cur = nxt + timedelta(days=1)


def iter_monthly_ranges(start: date, end: date) -> Iterable[tuple[date, date]]:
    cur = start
    while cur <= end:
        # next month first day
        if cur.month == 12:
            nm = date(cur.year + 1, 1, 1)
        else:
            nm = date(cur.year, cur.month + 1, 1)
        last_day = nm - timedelta(days=1)
        nxt = min(last_day, end)
        yield cur, nxt
        cur = nxt + timedelta(days=1)


def iter_ranges_by_unit(start: date, end: date, unit: str) -> Iterable[tuple[date, date]]:
    unit = unit.lower()
    if unit == "none":
        yield start, end
    elif unit == "weekly":
        yield from iter_weekly_ranges(start, end)
    elif unit == "monthly":
        yield from iter_monthly_ranges(start, end)
    else:
        raise ValueError(f"Unknown batch unit: {unit}")


# -------------------------
# Existing results loading & delta detection
# -------------------------
def load_existing_index(csv_path: Path, json_path: Path, logger: logging.Logger) -> dict[str, tuple[int, str]]:
    idx: dict[str, tuple[int, str]] = {}
    df = None
    if csv_path.exists():
        logger.info(f"Loading existing CSV: {csv_path}")
        df = pd.read_csv(csv_path)
    elif json_path.exists():
        logger.info(f"Loading existing JSON: {json_path}")
        df = pd.DataFrame(json.loads(json_path.read_text(encoding="utf-8")))
    else:
        return idx

    if df is None or df.empty:
        return idx

    for _, row in df.iterrows():
        doi = str(row.get("doi", "") or "").strip().lower()
        if not doi:
            continue
        v = row.get("version", None)
        # pandas NA check
        if v is None or (hasattr(pd, "isna") and pd.isna(v)):
            v_int = -1
        else:
            v_int = _to_int_version(v)

        d = row.get("posted_date", None)
        if d is None or (hasattr(pd, "isna") and pd.isna(d)):
            d = row.get("date", None)
        d_key = str(d) if d is not None and not (hasattr(pd, "isna") and pd.isna(d)) else "0000-00-00"

        cur = idx.get(doi)
        if cur is None or (v_int, d_key) > cur:
            idx[doi] = (v_int, d_key)

    logger.info(f"Existing index DOIs: {len(idx)}")
    return idx


def compute_delta(rows: list[Preprint], existing_idx: dict[str, tuple[int, str]]) -> list[Preprint]:
    delta = []
    for r in rows:
        doi = (r.doi or "").strip().lower()
        if not doi:
            continue
        key = (_to_int_version(r.version), _date_key(r.posted_date or r.date))
        cur = existing_idx.get(doi)
        if cur is None or key > cur:
            delta.append(r)
    return delta


# -------------------------
# Typer command
# -------------------------
@app.command()
def search(
    server: str = typer.Option("biorxiv", help='Target server: "biorxiv" or "medrxiv".'),
    from_date: str = typer.Option(..., help="Start date YYYY-MM-DD"),
    to_date: str = typer.Option(..., help="End date YYYY-MM-DD"),
    query: str = typer.Option("", help="Local keyword filter over title/abstract/authors/category."),
    exclude: Optional[list[str]] = typer.Option(None, "--exclude", help="Exclude term(s). Can be repeated."),
    category: Optional[str] = typer.Option(None, help="Filter by category (exact match, e.g., 'ecology')."),
    sleep: float = typer.Option(0.5, min=0.0, help="Sleep seconds between API pages (recommended >=0.3)."),
    out_prefix: str = typer.Option("biorxiv_results", help="Output prefix"),
    out_dir: Path = typer.Option(Path("."), help="Output directory"),
    # batch processing
    batch_unit: str = typer.Option("none", help='Batch unit: "none", "weekly", "monthly".'),
    # overwrite behavior
    incremental: bool = typer.Option(True, "--incremental/--no-incremental", help="Overwrite outputs after each batch."),
    # debug
    debug_max_items: int = typer.Option(20, min=0, help="In DEBUG, dump up to this many hit rows."),
    # latest version selection
    latest_only: bool = typer.Option(True, "--latest-only/--all-versions", help="Keep only latest version per DOI."),
    # update/delta
    update_from_existing: bool = typer.Option(
        True, "--update/--no-update", help="Load existing output and compute updates (delta)."
    ),
    write_delta: bool = typer.Option(
        True, "--write-delta/--no-write-delta", help="Write out_prefix.delta.csv/json for updates."
    ),
    # logging
    log_level: str = typer.Option("INFO", help="Log level: DEBUG, INFO, WARNING, ERROR"),
    log_file: Optional[Path] = typer.Option(None, help="Write logs to this file as well."),
):
    """
    Download bioRxiv/medRxiv metadata by date range (official API) and optionally filter locally.

    Added specs:
      - --batch-unit weekly/monthly: split the date range and run batch processing for each unit.
      - --update: load existing output (CSV/JSON) and compute delta (new/updated DOIs).
        Delta written to out_prefix.delta.csv/json (default).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    exclude_terms = exclude or []

    logger = setup_logger(log_level=log_level, log_file=log_file)

    start = _parse_date(from_date)
    end = _parse_date(to_date)
    if start > end:
        start, end = end, start

    retrieved_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    params = {
        "server": server,
        "from_date": _fmt_date(start),
        "to_date": _fmt_date(end),
        "batch_unit": batch_unit,
        "query": query,
        "exclude": exclude_terms,
        "category": category,
        "sleep": sleep,
        "out_prefix": out_prefix,
        "out_dir": str(out_dir),
        "incremental": incremental,
        "debug_max_items": debug_max_items,
        "latest_only": latest_only,
        "update_from_existing": update_from_existing,
        "write_delta": write_delta,
        "log_level": log_level,
    }
    log_run_header(logger, params=params, log_file=log_file)

    csv_path = out_dir / f"{out_prefix}.csv"
    json_path = out_dir / f"{out_prefix}.json"
    existing_idx: dict[str, tuple[int, str]] = {}
    if update_from_existing:
        existing_idx = load_existing_index(csv_path, json_path, logger)

    sess = make_retry_session(retries=6, backoff_factor=1.0)

    global_map: dict[str, Preprint] = {}

    def merge_rows(rows: list[Preprint]):
        for r in rows:
            k = (r.doi or "").strip().lower()
            if not k:
                k = f"__no_doi__::{r.biorxiv_url}::{r.title}".lower()
            cur = global_map.get(k)
            if cur is None:
                global_map[k] = r
                continue
            rv = _to_int_version(r.version)
            cv = _to_int_version(cur.version)
            if rv > cv:
                global_map[k] = r
            elif rv == cv and _date_key(r.posted_date or r.date) > _date_key(cur.posted_date or cur.date):
                global_map[k] = r

    logger.info(f"Fetching {server} {_fmt_date(start)}..{_fmt_date(end)} batch_unit={batch_unit}")
    batch_no = 0
    for b_start, b_end in iter_ranges_by_unit(start, end, batch_unit):
        batch_no += 1
        b_from = _fmt_date(b_start)
        b_to = _fmt_date(b_end)
        logger.info(f"[BATCH {batch_no}] range {b_from}..{b_to}")

        raw_all: list[dict] = []
        for cursor, page_items, messages in fetch_range_stream(server, b_from, b_to, sleep=sleep, logger=logger, sess=sess):
            raw_all.extend(page_items)
            logger.info(f"[BATCH {batch_no}] cursor={cursor} items={len(page_items)} cumulative_raw={len(raw_all)}")
            if logger.isEnabledFor(logging.DEBUG) and messages:
                logger.debug(f"[BATCH {batch_no}] messages={messages}")

        working = raw_all
        if category:
            working = [x for x in working if (x.get("category") or "").lower() == category.lower()]
        if query.strip() or exclude_terms:
            working = keyword_filter(working, query=query, exclude=exclude_terms)

        batch_rows = [item_to_preprint(server, it, retrieved_at=retrieved_at) for it in working]
        if latest_only:
            before = len(batch_rows)
            batch_rows = keep_latest_version_per_doi(batch_rows)
            logger.info(f"[BATCH {batch_no}] Latest-version select: {before} -> {len(batch_rows)}")

        debug_log_hits(logger, batch_rows, max_items=debug_max_items, title=f"BATCH {batch_no} HITS")
        merge_rows(batch_rows)

        if incremental:
            merged = list(global_map.values())
            merged.sort(key=lambda x: (_date_key(x.posted_date or x.date), x.doi), reverse=True)
            logger.info(f"[BATCH {batch_no}] Incremental overwrite (rows={len(merged)})")
            write_outputs_overwrite(out_dir, out_prefix, merged, logger)

    merged = list(global_map.values())
    merged.sort(key=lambda x: (_date_key(x.posted_date or x.date), x.doi), reverse=True)
    logger.info(f"Final overwrite (rows={len(merged)})")
    write_outputs_overwrite(out_dir, out_prefix, merged, logger)

    if update_from_existing:
        delta_rows = compute_delta(merged, existing_idx)
        logger.info(f"Delta (new/updated DOIs): {len(delta_rows)}")
        debug_log_hits(logger, delta_rows, max_items=debug_max_items, title="DELTA")
        if write_delta:
            write_delta_outputs(out_dir, out_prefix, delta_rows, logger)

    logger.info("Done.")


if __name__ == "__main__":
    app()
