from __future__ import annotations

import csv
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from typer.testing import CliRunner

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "script"))

import edna_literature_fetch
from libs.edna_models import Paper
from libs.sources import (
    _openalex_abstract,
    _parse_europepmc,
    _query_terms,
    _to_openalex_query,
    biorxiv_search_papers,
    crossref_search_papers,
    europepmc_fill_abstracts,
    fetch_range_stream,
    keyword_filter,
    merge_papers_by_doi_title,
)


class SourcesTest(unittest.TestCase):
    def test_query_terms(self) -> None:
        query = ('("environmental DNA"[Title/Abstract] OR eDNA[Title/Abstract] OR '
                 '"environmental RNA"[Title/Abstract] OR eRNA[Title/Abstract])')
        self.assertEqual(_query_terms(query), ["environmental dna", "edna", "environmental rna", "erna"])
        self.assertEqual(_query_terms(' eDNA and "environmental RNA" or '), ["edna", "environmental rna"])
        self.assertEqual(_query_terms(""), [])

    def test_crossref_term_filter(self) -> None:
        for include_abstract in (False, True):
            with self.subTest(include_abstract=include_abstract), patch("libs.sources.make_retry_session") as make_session:
                session = make_session.return_value
                session.get.return_value.json.return_value = {"message": {"items": [
                    {"title": ["Fish study"], "abstract": "<p>Environmental DNA in water.</p>", "DOI": "10.1000/match"},
                    {"title": ["DNA Binding Site prediction and eDNAnoise"], "DOI": "10.1000/noise"},
                ]}}
                logger = Mock()
                result = crossref_search_papers('"environmental DNA" OR eDNA', "test", sleep=0,
                                                include_abstract=include_abstract, logger=logger)
            self.assertEqual([paper.doi for paper in result], ["10.1000/match"])
            self.assertEqual(result[0].abstract, "Environmental DNA in water." if include_abstract else None)
            logger.info.assert_any_call("Crossref term filter dropped: 1")
            logger.warning.assert_not_called()
            session.get.assert_called_once()

    def test_crossref_stops_at_irrelevant_full_page(self) -> None:
        with patch("libs.sources.make_retry_session") as make_session:
            session = make_session.return_value
            session.get.return_value.json.return_value = {"message": {
                "items": [{"title": ["Noise"]}] * 200, "next-cursor": "next",
            }}
            logger = Mock()
            self.assertEqual(crossref_search_papers("eDNA", "test", sleep=0, logger=logger), [])
        session.get.assert_called_once()
        logger.info.assert_any_call("Crossref term filter dropped: 200")
        logger.warning.assert_not_called()

    def test_crossref_cursor_and_truncation(self) -> None:
        with patch("libs.sources.make_retry_session") as make_session:
            session = make_session.return_value
            cursors: list[str] = []

            def response(*args, **kwargs):
                cursors.append(kwargs["params"]["cursor"])
                result = Mock()
                result.json.return_value = {"message": {
                    "items": [{"title": ["eDNA Study"], "DOI": f"10.1000/{len(cursors)}"}],
                    "next-cursor": "next",
                }}
                return result

            session.get.side_effect = response
            logger = Mock()
            result = crossref_search_papers("eDNA", "test", max_items=2, sleep=0, logger=logger)
        self.assertEqual(cursors, ["*", "next"])
        self.assertEqual(len(result), 2)
        logger.warning.assert_called_once_with("Crossref results were truncated; raise --crossref-max-items")

    def test_fetch_merges_biorxiv_and_saves_missing_abstracts(self) -> None:
        papers = [Paper("", "Missing 要旨", "J", 2026, "", None, None, ""),
                  Paper("123", "Merged", "J", 2026, "", "10.1000/abc", None, "url")]
        bio = Paper("", "Merged", "bioRxiv", 2026, "Author", "10.1000/abc", "Abstract", "bio-url")
        with tempfile.TemporaryDirectory() as directory, \
                patch("edna_literature_fetch.crossref_search_papers", return_value=papers), \
                patch("edna_literature_fetch.biorxiv_search_papers", return_value=[bio]) as search, \
                patch("edna_literature_fetch.europepmc_fill_abstracts", side_effect=lambda papers, **kwargs: papers) as fill, \
                patch("edna_literature_fetch.check_health", return_value={}), \
                patch("edna_literature_fetch.screen_row", return_value={"flag_label": "in_scope"}) as screen:
            result = CliRunner().invoke(edna_literature_fetch.app, [
                "--email", "test@example.org", "--since", "2026/01/01", "--until", "2026/01/31",
                "--no-source-pubmed", "--no-source-openalex", "--run-biorxiv", "--strands-filter",
                "--out-dir", directory, "--out-prefix", "test",
            ])
            self.assertEqual(result.exit_code, 0, result.output)
            retained = list(csv.DictReader((Path(directory) / "test.csv").read_text().splitlines()))
            missing = list(csv.DictReader((Path(directory) / "test.no_abstract.csv").read_text().splitlines()))
            self.assertEqual(len(retained), 1)
            self.assertEqual(retained[0]["abstract"], "Abstract")
            self.assertEqual(retained[0]["pmid"], "123")
            self.assertEqual(missing[0]["title"], "Missing 要旨")
            self.assertTrue((Path(directory) / "test.no_abstract.csv").exists())
            self.assertEqual(list(csv.DictReader((Path(directory) / "test.rejected.csv").read_text().splitlines())), [])
            screen.assert_called_once()
            fill.assert_called_once()
            self.assertEqual(search.call_args.kwargs["from_date"], "2026/01/01")
            self.assertEqual(search.call_args.kwargs["to_date"], "2026/01/31")
            self.assertTrue(search.call_args.kwargs["include_abstract"])

    def test_parse_europepmc(self) -> None:
        self.assertEqual(_parse_europepmc({"resultList": {"result": [
            {"doi": "https://doi.org/10.1000/ABC", "abstractText": "<p>Abstract: Fish &amp; water.</p>", "pmid": "123"},
            {"doi": "10.1000/empty"}, {"abstractText": "No DOI"},
        ]}}), {"10.1000/abc": ("Fish & water.", "123")})
        self.assertEqual(_parse_europepmc({}), {})

    def test_europepmc_fills_only_missing_fields(self) -> None:
        papers = [Paper("", "Title", "J", 2026, "A", "10.1000/ABC", None, "url"),
                  Paper("456", "Other", "J", 2026, "A", "10.1000/other", "Existing", "url")]
        with patch("libs.sources.make_retry_session") as make_session:
            session = make_session.return_value.__enter__.return_value
            session.get.return_value.json.return_value = {"resultList": {"result": [
                {"doi": "10.1000/abc", "abstractText": "<p>Filled</p>", "pmid": "123"},
                {"doi": "10.1000/other", "abstractText": "Replacement", "pmid": "789"},
            ]}}
            result = europepmc_fill_abstracts(papers, sleep=0)
        self.assertEqual(result[0].abstract, "Filled")
        self.assertEqual(result[0].pmid, "123")
        self.assertEqual(result[0].title, papers[0].title)
        self.assertIs(result[1], papers[1])
        self.assertIsNone(papers[0].abstract)
        self.assertEqual(session.get.call_args.kwargs["params"]["query"], 'DOI:"10.1000/abc"')

    def test_openalex_query_and_abstract(self) -> None:
        self.assertEqual(_to_openalex_query('("environmental DNA"[Title/Abstract] OR eDNA[Title/Abstract])'),
                         '("environmental DNA" OR eDNA)')
        self.assertEqual(_openalex_abstract({"DNA": [1, 3], "Environmental": [0], "and": [2]}),
                         "Environmental DNA and DNA")
        self.assertIsNone(_openalex_abstract({}))
        self.assertIsNone(_openalex_abstract(None))

    def test_merge_doi_and_title_year(self) -> None:
        papers = [
            Paper("123", "Title", "J", 2026, "", "https://doi.org/10.1000/ABC", None, "url"),
            Paper("", "Other title", "", 2026, "Author", "10.1000/abc", "Abstract", ""),
            Paper("", "Fallback title!", "J", 2025, "", None, None, ""),
            Paper("", "FALLBACK TITLE", "", 2025, "Author", None, "Fallback abstract", ""),
            Paper("", "Fallback title", "J", 2024, "", None, None, ""),
        ]
        merged = merge_papers_by_doi_title(papers)
        self.assertEqual(len(merged), 3)
        self.assertEqual((merged[0].pmid, merged[0].doi, merged[0].abstract, merged[0].authors),
                         ("123", "10.1000/abc", "Abstract", "Author"))
        self.assertEqual(merged[1].abstract, "Fallback abstract")

    def test_biorxiv_keyword_modes(self) -> None:
        items = [{"title": "eDNA fish"}, {"title": "RNA fish"}, {"title": "eDNA soil"}]
        self.assertEqual(keyword_filter(items, "eDNA OR RNA", []), items)
        self.assertEqual(keyword_filter(items, "eDNA AND fish", []), items[:1])
        self.assertEqual(keyword_filter(items, "eDNA OR RNA", ["soil"]), items[:2])

    def test_biorxiv_pagination(self) -> None:
        for total in ("35", None, "invalid"):
            with self.subTest(total=total):
                session = Mock()
                collections = [[{"title": f"Study {i}"} for i in range(30)],
                               [{"title": f"Study {i}"} for i in range(30, 35)]]
                if total != "35":
                    collections.append([])
                responses = []
                for collection in collections:
                    response = Mock()
                    response.json.return_value = {"collection": collection,
                                                  "messages": [{"total": total}] if total is not None else []}
                    responses.append(response)
                session.get.side_effect = responses
                pages = list(fetch_range_stream("biorxiv", "2026-01-01", "2026-01-14", 0,
                                                logging.getLogger("test"), session))
                self.assertEqual([page[1] for page in pages], collections)
                self.assertEqual([page[0] for page in pages], [0, 30] if total == "35" else [0, 30, 35])
                self.assertEqual(session.get.call_count, len(collections))
                self.assertTrue(session.get.call_args_list[1].args[0].endswith("/30"))

    def test_biorxiv_adapter_latest_version_and_dates(self) -> None:
        items = [{"doi": "10.1000/abc", "title": "eDNA", "date": "2026-01-01", "version": "1"},
                 {"doi": "10.1000/abc", "title": "eDNA", "abstract": "Latest", "date": "2026-01-02", "version": "2"}]
        with patch("libs.sources.fetch_range_stream", return_value=iter([(0, items, [])])) as fetch:
            papers = biorxiv_search_papers("medrxiv", "2026/01/01", "2026-01-31", "eDNA", [], 0,
                                          logging.getLogger("test"), include_abstract=True)
        self.assertEqual(len(papers), 1)
        self.assertEqual((papers[0].journal, papers[0].year, papers[0].abstract), ("medRxiv", 2026, "Latest"))
        self.assertEqual(papers[0].pubmed_url, "https://www.medrxiv.org/content/10.1000/abcv2")
        self.assertEqual(fetch.call_args.args[1:3], ("2026-01-01", "2026-01-31"))



if __name__ == "__main__":
    unittest.main()
