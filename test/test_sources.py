from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "script"))

from libs.edna_models import Paper
from libs.sources import crossref_fill_missing_doi


class SourcesTest(unittest.TestCase):
    def test_crossref_fill_accepts_normalized_title_match(self) -> None:
        paper = Paper("123", "Environmental DNA: A study", "Journal", 2026, "Author", None, None, "")
        with patch("libs.sources.make_retry_session") as make_session:
            make_session.return_value.get.return_value.json.return_value = {
                "message": {"items": [{
                    "title": ["ENVIRONMENTAL DNA: A study!"],
                    "DOI": "https://doi.org/10.1000/ABC",
                }]},
            }
            result = crossref_fill_missing_doi([paper], user_agent="test", sleep=0)
        self.assertEqual(result, [Paper(**{**paper.__dict__, "doi": "10.1000/abc"})])
        self.assertIsNone(paper.doi)
        make_session.assert_called_once_with()

    def test_crossref_fill_rejects_different_title(self) -> None:
        paper = Paper("123", "Environmental DNA: A study", "Journal", 2026, "Author", None, None, "")
        with patch("libs.sources.make_retry_session") as make_session:
            make_session.return_value.get.return_value.json.return_value = {
                "message": {"items": [{"title": ["A different study"], "DOI": "10.1000/abc"}]},
            }
            result = crossref_fill_missing_doi([paper], user_agent="test", sleep=0)
        self.assertIs(result[0], paper)
        self.assertIsNone(result[0].doi)


if __name__ == "__main__":
    unittest.main()
