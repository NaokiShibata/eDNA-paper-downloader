from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "script"))

from libs.strands_screening import decide_label, prepare_abstract, record_id


class StrandsScreeningTest(unittest.TestCase):
    def test_in_scope_requires_scope_and_actual_use(self) -> None:
        label = decide_label(
            p_in_scope=0.82,
            p_out_of_scope=0.10,
            p_actual_use=0.75,
            p_method_relevance=0.40,
            include_threshold=0.70,
            actual_use_threshold=0.60,
            exclude_threshold=0.50,
            exclude_actual_use_max=0.50,
            exclude_method_relevance_max=0.60,
        )
        self.assertEqual(label, "in_scope")

    def test_out_of_scope_requires_all_exclusion_guards(self) -> None:
        label = decide_label(
            p_in_scope=0.10,
            p_out_of_scope=0.82,
            p_actual_use=0.20,
            p_method_relevance=0.25,
            include_threshold=0.70,
            actual_use_threshold=0.60,
            exclude_threshold=0.50,
            exclude_actual_use_max=0.50,
            exclude_method_relevance_max=0.60,
        )
        self.assertEqual(label, "out_of_scope")

    def test_method_relevant_paper_is_not_auto_excluded(self) -> None:
        label = decide_label(
            p_in_scope=0.20,
            p_out_of_scope=0.78,
            p_actual_use=0.25,
            p_method_relevance=0.80,
            include_threshold=0.70,
            actual_use_threshold=0.60,
            exclude_threshold=0.50,
            exclude_actual_use_max=0.50,
            exclude_method_relevance_max=0.60,
        )
        self.assertEqual(label, "unsure")

    def test_high_scope_without_actual_use_is_unsure(self) -> None:
        label = decide_label(
            p_in_scope=0.80,
            p_out_of_scope=0.15,
            p_actual_use=0.40,
            p_method_relevance=0.40,
            include_threshold=0.70,
            actual_use_threshold=0.60,
            exclude_threshold=0.50,
            exclude_actual_use_max=0.50,
            exclude_method_relevance_max=0.60,
        )
        self.assertEqual(label, "unsure")

    def test_record_id_prefers_doi(self) -> None:
        self.assertEqual(
            record_id(
                {
                    "doi": "https://doi.org/10.1000/ABC",
                    "title": "Ignored title",
                    "year": "2026",
                }
            ),
            "doi:10.1000/abc",
        )

    def test_prepare_abstract_normalizes_whitespace(self) -> None:
        self.assertEqual(
            prepare_abstract("A\n\n  short\tabstract.", None),
            "A short abstract.",
        )


if __name__ == "__main__":
    unittest.main()
