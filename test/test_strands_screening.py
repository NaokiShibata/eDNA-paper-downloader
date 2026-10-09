from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from typer.testing import CliRunner

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "script"))

import screen
from libs.strands_screening import (
    PROMPT_VERSION,
    QUESTIONS,
    SCORE_COLUMNS,
    ScoreCache,
    ScreeningConfig,
    _strip_jsonc,
    apply_thresholds,
    build_state,
    check_health,
    classify_abstract,
    decide_label,
    default_config_path,
    extract_scores,
    output_column,
    output_row,
    prepare_abstract,
    record_id,
    screen_row,
)


class HealthCheckTests(unittest.TestCase):
    def test_first_stage_failure_reports_actual_server(self):
        session = Mock()
        session.get.side_effect = requests.ConnectionError("connection refused")
        with self.assertRaisesRegex(RuntimeError, "http://127.0.0.1:8012/health"):
            check_health(session, "http://127.0.0.1:8014", 30, "http://127.0.0.1:8012")
        session.get.assert_called_once_with("http://127.0.0.1:8012/health", timeout=15.0)


class StrandsScreeningTest(unittest.TestCase):
    def test_strip_jsonc(self) -> None:
        text = '{/* comment */"url": "https://example.org//path", // comment\n"values": [1, 2,],}'
        self.assertEqual(json.loads(_strip_jsonc(text)), {"url": "https://example.org//path", "values": [1, 2]})

    def test_score_cache_round_trip_and_thresholds(self) -> None:
        scores = {"strands_p_in_scope": 0.82, "strands_p_out_of_scope": 0.1, "strands_p_unsure": 0.08,
                  "strands_p_actual_use": 0.75, "strands_p_microbial_only": 0.2, "strands_p_method_relevance": 0.4, "strands_p_review": 0.1}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scores.csv"
            path.touch()
            cache = ScoreCache(path)
            self.assertEqual(cache.path, path.with_name(f"scores.{PROMPT_VERSION}.csv"))
            self.assertIsNone(cache.get("missing"))
            cache.put("doi:current", scores | {"flag_label": "out_of_scope"})
            with cache.path.open("a", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=["flag_record_id", "flag_prompt_version", *(output_column(col) for col in SCORE_COLUMNS)])
                writer.writerow(output_row({"flag_record_id": "doi:old", "flag_prompt_version": "old", **scores}))
            loaded = ScoreCache(path)
            self.assertIsNone(loaded.get("doi:old"))
            hit = loaded.get("doi:current")
            assert hit is not None
            self.assertEqual(hit["strands_p_in_scope"], "0.82")
            self.assertNotIn("flag_label", hit)
            self.assertEqual(apply_thresholds(hit, ScreeningConfig())["flag_label"], "in_scope")
            self.assertEqual(apply_thresholds(hit, ScreeningConfig(include_threshold=0.9))["flag_label"], "unsure")
            with cache.path.open(encoding="utf-8") as stream:
                self.assertEqual(next(csv.DictReader(stream))["flag_prompt_version"], PROMPT_VERSION)

    def test_score_cache_replaces_incompatible_header(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scores.csv"
            versioned_path = path.with_name(f"scores.{PROMPT_VERSION}.csv")
            columns = ["flag_record_id", "flag_prompt_version", *SCORE_COLUMNS]
            columns.remove("strands_p_review")
            with versioned_path.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=columns)
                writer.writeheader()
                writer.writerow({"flag_record_id": "doi:old", "flag_prompt_version": PROMPT_VERSION})
            cache = ScoreCache(path)
            self.assertEqual(cache.rows, {})
            cache.put("doi:current", {"strands_p_review": 0.1})
            cache.put("doi:next", {"strands_p_review": 0.2})
            loaded = ScoreCache(path)
            self.assertIsNone(loaded.get("doi:old"))
            self.assertEqual(loaded.rows["doi:current"]["strands_p_review"], "0.1")
            self.assertEqual(loaded.rows["doi:next"]["strands_p_review"], "0.2")

    def test_screen_cache_hit_miss_and_failure(self) -> None:
        scores = {"strands_p_in_scope": 0.82, "strands_p_out_of_scope": 0.1, "strands_p_unsure": 0.08,
                  "strands_p_actual_use": 0.75, "strands_p_microbial_only": 0.2, "strands_p_method_relevance": 0.4, "strands_p_review": 0.1}
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

    def test_cache_invalidates_changed_input_and_model(self) -> None:
        scores = {"strands_p_in_scope": 0.82, "strands_p_out_of_scope": 0.1, "strands_p_unsure": 0.08,
                  "strands_p_actual_use": 0.75, "strands_p_microbial_only": 0.2,
                  "strands_p_method_relevance": 0.4, "strands_p_review": 0.1}
        meta = {"doi": "10.1000/abc", "abstract": "Original abstract"}
        with tempfile.TemporaryDirectory() as directory:
            cache = ScoreCache(Path(directory) / "scores.csv")
            with patch("libs.strands_screening.evaluate_abstract", return_value=scores) as evaluate:
                screen_row(Mock(), meta, ScreeningConfig(), cache=cache)
                screen_row(Mock(), meta | {"abstract": "Changed abstract"}, ScreeningConfig(), cache=cache)
                screen_row(Mock(), meta, ScreeningConfig(expected_model="different-model"), cache=cache)
                screen_row(Mock(), meta, ScreeningConfig(), cache=cache)
                self.assertEqual(evaluate.call_count, 3)

    def test_cascade_routes_uncertain_results_and_first_stage_failure(self) -> None:
        from libs.strands_screening import evaluate_abstract
        cfg = ScreeningConfig(first_stage_base_url="http://fast", first_stage_model="fast")
        fast = {"flag_model_path": "fast", "strands_latency_ms": 1,
                "strands_p_in_scope": 0.96, "strands_p_out_of_scope": 0.02, "strands_p_unsure": 0.02,
                "strands_p_actual_use": 0.8, "strands_p_microbial_only": 0.1,
                "strands_p_method_relevance": 0.4, "strands_p_review": 0.1}
        for probability, actual_use, expected in [(0.96, 0.8, "first"), (0.94, 0.8, "first"),
                                                 (0.96, 0.2, "second")]:
            candidate = fast | {"strands_p_in_scope": probability, "strands_p_unsure": 0.98 - probability,
                                "strands_p_actual_use": actual_use}
            with patch("libs.strands_screening._evaluate_abstract", side_effect=[candidate, fast]) as infer:
                result = evaluate_abstract(Mock(), "abstract", cfg)
                self.assertEqual(result["evaluation_stage"], expected)
                self.assertEqual(infer.call_count, 1 if expected == "first" else 2)
        with patch("libs.strands_screening._evaluate_abstract", side_effect=[RuntimeError("offline"), fast]):
            result = evaluate_abstract(Mock(), "abstract", cfg)
            self.assertEqual(result["evaluation_stage"], "second")
            self.assertEqual(result["first_stage_error"], "offline")

    def test_cascade_accepts_exclusion_and_routes_short_abstract_to_27b(self) -> None:
        from libs.strands_screening import evaluate_abstract
        cfg = ScreeningConfig(first_stage_base_url="http://strands")
        scores = {"flag_model_path": "strands", "strands_latency_ms": 1,
                  "strands_p_in_scope": 0.2, "strands_p_out_of_scope": 0.7, "strands_p_unsure": 0.1,
                  "strands_p_actual_use": 0.2, "strands_p_microbial_only": 0.1,
                  "strands_p_method_relevance": 0.1, "strands_p_review": 0.1}
        for text, stage in [("Abstract " * 50, "first"), ("Title: Study\nAbstract: Too short", "second")]:
            with patch("libs.strands_screening._evaluate_abstract", side_effect=[scores.copy(), scores.copy()]) as infer:
                result = evaluate_abstract(Mock(), text, cfg)
                self.assertEqual(result["evaluation_stage"], stage)
                self.assertEqual(infer.call_count, 1 if stage == "first" else 2)

    def test_cascade_strands_uses_sequential_requests(self) -> None:
        from libs.strands_screening import evaluate_abstract
        cfg = ScreeningConfig(first_stage_base_url="http://strands", first_stage_batch_questions=False)
        fast = {"flag_model_path": "strands", "strands_latency_ms": 1,
                "strands_p_in_scope": 0.96, "strands_p_out_of_scope": 0.02, "strands_p_unsure": 0.02,
                "strands_p_actual_use": 0.8, "strands_p_microbial_only": 0.1,
                "strands_p_method_relevance": 0.4, "strands_p_review": 0.1}
        with patch("libs.strands_screening._evaluate_abstract", return_value=fast) as infer:
            self.assertEqual(evaluate_abstract(Mock(), "abstract", cfg)["evaluation_stage"], "first")
            self.assertEqual(infer.call_args.args[2].base_url, "http://strands")
            self.assertFalse(infer.call_args.args[2].batch_questions)

    def test_default_config_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_dir = root / "config"
            config_dir.mkdir()
            with patch("libs.strands_screening.__file__", str(root / "script/libs/strands_screening.py")):
                (config_dir / "strands_flagger.jsonc").touch()
                self.assertEqual(default_config_path(), config_dir / "clef_flagger.example.jsonc")
                local = config_dir / "clef_flagger.jsonc"
                local.touch()
                self.assertEqual(default_config_path(), local)

    def test_flagger_rewrites_output_using_cached_scores(self) -> None:
        scores = {"strands_p_in_scope": 0.82, "strands_p_out_of_scope": 0.1, "strands_p_unsure": 0.08,
                  "strands_p_actual_use": 0.75, "strands_p_microbial_only": 0.2, "strands_p_method_relevance": 0.4, "strands_p_review": 0.1}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_csv = root / "papers.csv"
            input_csv.write_text("doi,title,abstract\n10.1000/abc,Study,Abstract\n", encoding="utf-8")
            config = root / "config.json"
            config.write_text(json.dumps({"cache_csv": str(root / "cache/scores.csv")}), encoding="utf-8")
            output = root / "papers.strands.csv"
            args = [str(input_csv), "--log-file", str(root / "flagger.log")]
            with patch("screen.default_config_path", return_value=config), \
                    patch("screen.check_health", return_value={}), \
                    patch("libs.strands_screening.evaluate_abstract", return_value=scores) as evaluate:
                for threshold, label in ((0.45, "in_scope"), (0.9, "unsure")):
                    config.write_text(json.dumps({"cache_csv": str(root / "cache/scores.csv"),
                                                  "include_threshold": threshold}), encoding="utf-8")
                    result = CliRunner().invoke(screen.app, args)
                    self.assertEqual(result.exit_code, 0, result.output)
                    with output.open(encoding="utf-8") as stream:
                        rows = list(csv.DictReader(stream))
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0]["flag_label"], label)
                    self.assertIn("p_in_scope", rows[0])
                    self.assertNotIn("strands_p_in_scope", rows[0])
                evaluate.assert_called_once()
                input_csv.write_text("doi,title,abstract\n", encoding="utf-8")
                result = CliRunner().invoke(screen.app, args)
                self.assertEqual(result.exit_code, 0, result.output)
                with output.open(encoding="utf-8") as stream:
                    self.assertEqual(list(csv.DictReader(stream)), [])
            self.assertIn("Strands cache hits=1 misses=0", (root / "flagger.log").read_text())

    def test_unknown_config_keys(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unknown screening config keys: abstract_column, resume"):
            ScreeningConfig.from_sources({"resume": True, "abstract_column": "abstract"})
        self.assertEqual(ScreeningConfig.from_sources({"cache_csv": "scores.csv"}).cache_csv, "scores.csv")
        self.assertIsNone(ScreeningConfig.from_sources({"cache_csv": None}).cache_csv)

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
            "strands_p_review": "0.1",
        }
        result = apply_thresholds(scores, ScreeningConfig())
        self.assertEqual(result, {
            "flag_label": "in_scope",
            "flag_reason": "in_scope: scope=in_scope; p_in_scope=0.820; p_out_of_scope=0.100; "
            "p_unsure=0.080; actual_use=0.750; microbial_only=0.200; method_relevance=0.400; review=0.100",
            "flag_prompt_version": PROMPT_VERSION,
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
                "study_type": {"probabilities": {"review": 0.1}},
            },
        }
        result = classify_abstract(session, "abstract", ScreeningConfig(batch_questions=True))
        session.post.assert_called_once_with(
            "http://127.0.0.1:8012/v1/systemone",
            json={"state": "abstract", "questions": QUESTIONS}, timeout=120.0,
        )
        self.assertEqual(result["flag_label"], "in_scope")

    def test_expected_model_is_checked_before_reusing_scores(self) -> None:
        from libs.strands_screening import evaluate_abstract
        with patch("libs.strands_screening._post_with_retry", return_value={"model": "wrong-model"}):
            with self.assertRaisesRegex(RuntimeError, "expected model"):
                evaluate_abstract(Mock(), "abstract", ScreeningConfig(batch_questions=True, expected_model="correct-model"))

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
            p_review=0.1,
            include_threshold=0.45,
            include_microbial_only_max=0.35,
            actual_use_threshold=0.60,
            exclude_threshold=0.50,
            exclude_actual_use_max=0.50,
            exclude_method_relevance_max=0.60,
            exclude_review_min=0.60, exclude_review_out_min=0.40,
        )
        self.assertEqual(label, "in_scope")

    def test_out_of_scope_requires_all_exclusion_guards(self) -> None:
        label = decide_label(
            p_in_scope=0.10,
            p_out_of_scope=0.82,
            p_actual_use=0.20,
            p_method_relevance=0.25,
            p_microbial_only=0.2,
            p_review=0.1,
            include_threshold=0.45,
            include_microbial_only_max=0.35,
            actual_use_threshold=0.60,
            exclude_threshold=0.50,
            exclude_actual_use_max=0.50,
            exclude_method_relevance_max=0.60,
            exclude_review_min=0.60, exclude_review_out_min=0.40,
        )
        self.assertEqual(label, "out_of_scope")

    def test_method_relevant_paper_is_not_auto_excluded(self) -> None:
        label = decide_label(
            p_in_scope=0.20,
            p_out_of_scope=0.78,
            p_actual_use=0.25,
            p_method_relevance=0.80,
            p_microbial_only=0.2,
            p_review=0.1,
            include_threshold=0.45,
            include_microbial_only_max=0.35,
            actual_use_threshold=0.60,
            exclude_threshold=0.50,
            exclude_actual_use_max=0.50,
            exclude_method_relevance_max=0.60,
            exclude_review_min=0.60, exclude_review_out_min=0.40,
        )
        self.assertEqual(label, "unsure")

    def test_high_scope_without_actual_use_is_unsure(self) -> None:
        label = decide_label(
            p_in_scope=0.80,
            p_out_of_scope=0.15,
            p_actual_use=0.40,
            p_method_relevance=0.40,
            p_microbial_only=0.2,
            p_review=0.1,
            include_threshold=0.45,
            include_microbial_only_max=0.35,
            actual_use_threshold=0.60,
            exclude_threshold=0.50,
            exclude_actual_use_max=0.50,
            exclude_method_relevance_max=0.60,
            exclude_review_min=0.60, exclude_review_out_min=0.40,
        )
        self.assertEqual(label, "unsure")

    def test_microbial_only_inclusion_guard(self) -> None:
        for probability, method, expected in [(0.349, 0.4, "in_scope"), (0.35, 0.4, "unsure"),
                                             (0.799, 0.4, "unsure"), (0.8, 0.4, "out_of_scope"),
                                             (0.8, 0.8, "unsure")]:
            with self.subTest(probability=probability):
                self.assertEqual(decide_label(
                    p_in_scope=0.45, p_out_of_scope=0.1, p_actual_use=0.60,
                    p_method_relevance=method, p_microbial_only=probability, p_review=0.1,
                    include_threshold=0.45, include_microbial_only_max=0.35,
                    actual_use_threshold=0.60, exclude_threshold=0.50,
                    exclude_actual_use_max=0.50, exclude_method_relevance_max=0.60,
                    exclude_review_min=0.60, exclude_review_out_min=0.40,
                ), expected)

    def test_screen_row_short_abstract_guard(self) -> None:
        scores = {
            "strands_p_in_scope": 0.1, "strands_p_out_of_scope": 0.8,
            "strands_p_unsure": 0.1, "strands_p_actual_use": 0.2,
            "strands_p_microbial_only": 0.8, "strands_p_method_relevance": 0.2,
            "strands_p_review": 0.1,
        }
        for length, expected in [(299, "unsure"), (300, "out_of_scope")]:
            with self.subTest(length=length):
                meta = {"title": "Title " * 100, "abstract": "a" * length}
                response = {"answers": {
                    "scope": {"probabilities": {"in_scope": 0.1, "out_of_scope": 0.8, "unsure": 0.1}},
                    "actual_use": {"noul": 0.2}, "microbial_only": {"noul": 0.8},
                    "method_relevance": {"noul": 0.2},
                    "study_type": {"probabilities": {"review": 0.1}},
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

    def test_review_exclusion_rule(self) -> None:
        for p_in, p_out, p_review, expected in [
            (0.2, 0.40, 0.60, "out_of_scope"),
            (0.45, 0.40, 0.60, "in_scope"),
            (0.2, 0.399, 0.9, "unsure"),
            (0.2, 0.40, 0.599, "unsure"),
        ]:
            with self.subTest(p_in=p_in, p_out=p_out, p_review=p_review):
                scores = {
                    "strands_p_in_scope": p_in, "strands_p_out_of_scope": p_out,
                    "strands_p_unsure": 0.1, "strands_p_actual_use": 0.7,
                    "strands_p_microbial_only": 0.1, "strands_p_method_relevance": 0.8,
                    "strands_p_review": p_review,
                }
                self.assertEqual(apply_thresholds(scores, ScreeningConfig())["flag_label"], expected)
                if expected == "out_of_scope":
                    with patch("libs.strands_screening.evaluate_abstract", return_value=scores):
                        result = screen_row(Mock(), {"abstract": "short"}, ScreeningConfig())
                    self.assertEqual(result["flag_label"], "unsure")
                    cfg = ScreeningConfig(exclude_review_min=0.7, exclude_review_out_min=0.5)
                    self.assertEqual(apply_thresholds(scores, cfg)["flag_label"], "unsure")

    def test_extract_scores_review_probability(self) -> None:
        scores = extract_scores({"answers": {
            "scope": {"probabilities": {"out_of_scope": 0.4}},
            "actual_use": {"noul": 0.7}, "microbial_only": {"noul": 0.1},
            "method_relevance": {"noul": 0.8},
            "study_type": {"probabilities": {"review": 0.61234567}},
        }})
        self.assertEqual(scores["strands_p_review"], 0.612346)

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
