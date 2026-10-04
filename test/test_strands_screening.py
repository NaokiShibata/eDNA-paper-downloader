from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "script"))

from libs.strands_screening import (
    QUESTIONS,
    ScreeningConfig,
    apply_thresholds,
    classify_abstract,
    decide_label,
    prepare_abstract,
    record_id,
    screen_row,
)


class StrandsScreeningTest(unittest.TestCase):
    def test_config_source_precedence(self) -> None:
        cfg = ScreeningConfig.from_sources(
            {"timeout": 30, "retries": 5, "include_threshold": 0.8},
            timeout=45, retries=None,
        )
        self.assertEqual(cfg.timeout, 45.0)
        self.assertEqual(cfg.retries, 5)
        self.assertEqual(cfg.include_threshold, 0.8)
        self.assertEqual(cfg.actual_use_threshold, 0.6)
        self.assertEqual(ScreeningConfig.from_sources({}), ScreeningConfig())

    def test_config_casts_values(self) -> None:
        cfg = ScreeningConfig.from_sources({
            "base_url": "http://localhost:8012///",
            "timeout": "45.5",
            "retries": "2",
            "include_threshold": "0.8",
            "actual_use_threshold": "0.7",
            "exclude_threshold": "0.4",
            "exclude_actual_use_max": "0.3",
            "exclude_method_relevance_max": "0.2",
            "max_abstract_chars": "1000",
            "batch_questions": 1,
        })
        self.assertEqual(cfg, ScreeningConfig(
            base_url="http://localhost:8012", timeout=45.5, retries=2,
            include_threshold=0.8, actual_use_threshold=0.7,
            exclude_threshold=0.4, exclude_actual_use_max=0.3,
            exclude_method_relevance_max=0.2, max_abstract_chars=1000,
            batch_questions=True,
        ))
        self.assertIsNone(ScreeningConfig.from_sources({"max_abstract_chars": None}).max_abstract_chars)
        self.assertFalse(ScreeningConfig.from_sources({"batch_questions": True}, batch_questions=False).batch_questions)

    def test_apply_thresholds_with_csv_strings(self) -> None:
        scores = {
            "strands_scope_choice": "in_scope",
            "strands_p_in_scope": "0.82",
            "strands_p_out_of_scope": "0.1",
            "strands_p_unsure": "0.08",
            "strands_p_actual_use": "0.75",
            "strands_p_microbial_only": "0.2",
            "strands_p_method_relevance": "0.4",
        }
        result = apply_thresholds(scores, ScreeningConfig())
        self.assertEqual(result, {
            "flag_label": "in_scope",
            "flag_reason": "in_scope: scope=in_scope; p_in_scope=0.820; p_out_of_scope=0.100; "
            "p_unsure=0.080; actual_use=0.750; microbial_only=0.200; method_relevance=0.400",
            "flag_prompt_version": "strands-v2",
        })
        self.assertEqual(
            apply_thresholds(scores, ScreeningConfig(include_threshold=0.9))["flag_label"], "unsure",
        )

    def test_batch_questions_uses_one_request(self) -> None:
        session = Mock()
        response = session.post.return_value
        response.ok = True
        response.json.return_value = {
            "answers": {
                "scope": {"probabilities": {"in_scope": 0.8}, "choice": "in_scope", "confidence": 0.8},
                "actual_use": {"noul": 0.7},
                "microbial_only": {"noul": 0.2},
                "method_relevance": {"noul": 0.4},
            },
        }
        result = classify_abstract(session, "abstract", ScreeningConfig(batch_questions=True))
        session.post.assert_called_once_with(
            "http://127.0.0.1:8012/v1/systemone",
            json={"state": "abstract", "questions": QUESTIONS}, timeout=120.0,
        )
        self.assertEqual(result["flag_label"], "in_scope")

    def test_screen_row_empty_and_error(self) -> None:
        session = Mock()
        result = screen_row(session, {"doi": "10.1000/abc", "abstract": " "}, ScreeningConfig())
        self.assertEqual(result["flag_record_id"], "doi:10.1000/abc")
        self.assertEqual(result["flag_reason"], "unsure: abstract is empty")
        session.post.assert_not_called()
        with patch("libs.strands_screening.classify_abstract", side_effect=RuntimeError("failed")):
            result = screen_row(session, {"abstract": "abstract"}, ScreeningConfig())
        self.assertEqual(result["flag_label"], "process_error")
        self.assertEqual(result["flag_reason"], "failed")

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
