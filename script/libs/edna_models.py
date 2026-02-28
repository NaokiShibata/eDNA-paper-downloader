from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Paper:
    pmid: str
    title: str
    journal: str
    year: int | None
    authors: str
    doi: str | None
    abstract: str | None
    pubmed_url: str
