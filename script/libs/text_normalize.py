from __future__ import annotations

import re


def clean_doi(value: str | None) -> str:
    v = (value or "").strip().lower()
    v = re.sub(r"^https?://(dx\.)?doi\.org/", "", v)
    v = re.sub(r"^doi:\s*", "", v)
    return v.strip()
