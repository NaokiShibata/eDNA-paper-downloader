from __future__ import annotations

import html
import re


def clean_doi(value: str | None) -> str:
    v = (value or "").strip().lower()
    v = re.sub(r"^https?://(dx\.)?doi\.org/", "", v)
    v = re.sub(r"^doi:\s*", "", v)
    return v.strip()


def clean_text(text: str | None) -> str:
    s = html.unescape(text or "")
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    while len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        s = s[1:-1].strip()
    return s


def norm_title(title: str) -> str:
    t = title.lower().strip()
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"[^a-z0-9 ]+", "", t)
    return t


def clean_term(term: str) -> str:
    t = term.strip()
    while t.startswith("(") and t.endswith(")") and len(t) > 2:
        t = t[1:-1].strip()
    if (t.startswith('"') and t.endswith('"')) or (t.startswith("'") and t.endswith("'")):
        t = t[1:-1].strip()
    return t
