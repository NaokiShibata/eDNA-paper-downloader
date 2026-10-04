from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "script"))

from libs.strands_screening import (
    PROMPT_VERSION,
    QUESTIONS,
    SCORE_COLUMNS,
    ScoreCache,
    ScreeningConfig,
    _strip_jsonc,
    apply_thresholds,
    build_state,
    classify_abstract,
    decide_label,
    prepare_abstract,
    record_id,
    screen_row,
)


class StrandsScreeningTest(unittest.TestCase):
    def test_strip_jsonc(self) -> None:
        text = '{/* comment */"url": "https://example.org//path", // comment\n"values": [1, 2,],}'
        self.assertEqual(json.loads(_strip_jsonc(text)), {"url": "https://example.org//path", "values": [1, 2]})

    def test_score_cache_round_trip_and_thresholds(self) -> None:
        scores = {"strands_p_in_scope": 0.82, "strands_p_out_of_scope": 0.1, "strands_p_unsure": 0.08,
                  "strands_p_actual_use": 0.75, "strands_p_microbial_only": 0.2, "strands_p_method_relevance": 0.4}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scores.csv"
            path.touch()
            cache = ScoreCache(path)
            self.assertIsNone(cache.get("missing"))
            cache.put("doi:current", scores | {"flag_label": "out_of_scope"})
            with path.open("a", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=["flag_record_id", "flag_prompt_version", *SCORE_COLUMNS])
                writer.writerow({"flag_record_id": "doi:old", "flag_prompt_version": "old", **scores})
            loaded = ScoreCache(path)
            self.assertIsNone(loaded.get("doi:old"))
            hit = loaded.get("doi:current")
            assert hit is not None
            self.assertEqual(hit["strands_p_in_scope"], "0.82")
            self.assertNotIn("flag_label", hit)
            self.assertEqual(apply_thresholds(hit, ScreeningConfig())["flag_label"], "in_scope")
            self.assertEqual(apply_thresholds(hit, ScreeningConfig(include_threshold=0.9))["flag_label"], "unsure")
            with path.open(encoding="utf-8") as stream:
                self.assertEqual(next(csv.DictReader(stream))["flag_prompt_version"], PROMPT_VERSION)

    def test_screen_cache_hit_miss_and_failure(self) -> None:
        scores = {"strands_p_in_scope": 0.82, "strands_p_out_of_scope": 0.1, "strands_p_unsure": 0.08,
                  "strands_p_actual_use": 0.75, "strands_p_microbial_only": 0.2, "strands_p_method_relevance": 0.4}
        meta = {"doi": "10.1000/abc", "abstract": "Abstract"}
        with tempfile.TemporaryDirectory() as directory:
            cache = ScoreCache(Path(directory) / "cache.csv")
            with patch("libs.strands_screening.evaluate_abstract", return_value=scores) as evaluate:
                self.assertEqual(screen_row(Mock(), meta, ScreeningConfig(), cache=cache)["flag_label"], "in_scope")
                self.assertEqual(screen_row(Mock(), meta, ScreeningConfig(include_threshold=0.9), cache=cache)["flag_label"], "unsure")
                evaluate.assert_called_once()
            self.assertEqual((cache.hits, cache.misses), (1, 1))
            with patch("libs.strands_screening.evaluate_abstract", side_effect=RuntimeError("failed")):
                result = screen_row(Mock(), meta | {"doi": "10.1000/error"}, ScreeningConfig(), cache=cache)
            self.assertEqual(result["flag_label"], "process_error")
            self.assertIsNone(cache.get("doi:10.1000/error"))
            screen_row(Mock(), meta | {"doi": "10.1000/empty", "abstract": ""}, ScreeningConfig(), cache=cache)
            self.assertIsNone(cache.get("doi:10.1000/empty"))

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
            "flag_prompt_version": "strands-v3",
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
        result = screen_row(session, {"doi": "10.1000/abc", "title": "Title", "abstract": " "}, ScreeningConfig())
        self.assertEqual(result["flag_record_id"], "doi:10.1000/abc")
        self.assertEqual(result["flag_reason"], "unsure: abstract is empty")
        session.post.assert_not_called()
        with patch("libs.strands_screening.evaluate_abstract", side_effect=RuntimeError("failed")):
            result = screen_row(session, {"abstract": "abstract"}, ScreeningConfig())
        self.assertEqual(result["flag_label"], "process_error")
        self.assertEqual(result["flag_reason"], "failed")

    def test_in_scope_requires_scope_and_actual_use(self) -> None:
        label = decide_label(
            p_in_scope=0.82,
            p_out_of_scope=0.10,
            p_actual_use=0.75,
            p_method_relevance=0.40,
            p_microbial_only=0.2,
            include_threshold=0.45,
            include_microbial_only_max=0.35,
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
            p_microbial_only=0.2,
            include_threshold=0.45,
            include_microbial_only_max=0.35,
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
            p_microbial_only=0.2,
            include_threshold=0.45,
            include_microbial_only_max=0.35,
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
            p_microbial_only=0.2,
            include_threshold=0.45,
            include_microbial_only_max=0.35,
            actual_use_threshold=0.60,
            exclude_threshold=0.50,
            exclude_actual_use_max=0.50,
            exclude_method_relevance_max=0.60,
        )
        self.assertEqual(label, "unsure")

    def test_microbial_only_inclusion_guard(self) -> None:
        for probability, expected in [(0.349, "in_scope"), (0.35, "unsure"), (0.8, "unsure")]:
            with self.subTest(probability=probability):
                self.assertEqual(decide_label(
                    p_in_scope=0.45, p_out_of_scope=0.1, p_actual_use=0.60,
                    p_method_relevance=0.4, p_microbial_only=probability,
                    include_threshold=0.45, include_microbial_only_max=0.35,
                    actual_use_threshold=0.60, exclude_threshold=0.50,
                    exclude_actual_use_max=0.50, exclude_method_relevance_max=0.60,
                ), expected)

    def test_screen_row_short_abstract_guard(self) -> None:
        scores = {
            "strands_p_in_scope": 0.1, "strands_p_out_of_scope": 0.8,
            "strands_p_unsure": 0.1, "strands_p_actual_use": 0.2,
            "strands_p_microbial_only": 0.8, "strands_p_method_relevance": 0.2,
        }
        for length, expected in [(299, "unsure"), (300, "out_of_scope")]:
            with self.subTest(length=length):
                meta = {"title": "Title " * 100, "abstract": "a" * length}
                response = {"answers": {
                    "scope": {"probabilities": {"in_scope": 0.1, "out_of_scope": 0.8, "unsure": 0.1}},
                    "actual_use": {"noul": 0.2}, "microbial_only": {"noul": 0.8},
                    "method_relevance": {"noul": 0.2},
                }}
                cfg = ScreeningConfig(batch_questions=True)
                with patch("libs.strands_screening._post_with_retry", return_value=response) as post:
                    result = screen_row(Mock(), meta, cfg)
                self.assertEqual(post.call_args.args[2]["state"], build_state(meta, cfg))
                self.assertEqual(result["flag_label"], expected)
                for key, value in scores.items():
                    self.assertEqual(result[key], value)
                if expected == "unsure":
                    self.assertTrue(result["flag_reason"].startswith("unsure: abstract too short to exclude; "))

    def test_build_state_with_and_without_title(self) -> None:
        cfg = ScreeningConfig()
        self.assertEqual(build_state({"title": "A title", "abstract": "A\n  short abstract"}, cfg),
                         "Title: A title\nAbstract: A short abstract")
        for meta in [{"abstract": "Abstract"}, {"title": " ", "abstract": "Abstract"}]:
            self.assertEqual(build_state(meta, cfg), "Abstract")
        self.assertEqual(build_state({"title": "Title", "summary": "Summary"}, cfg, "summary"),
                         "Title: Title\nAbstract: Summary")

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
