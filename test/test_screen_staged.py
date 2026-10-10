from __future__ import annotations

import csv
import io
import sys
import tempfile
import unittest
from contextlib import contextmanager, nullcontext, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "script"))
import screen
from libs.screen_servers import OMNI_MODEL, STRANDS_MODEL
from libs.strands_screening import ScreeningConfig


def scores(label: str, model: str) -> dict:
    p_in, p_out, p_unsure, actual = {
        "in_scope": (.9, .05, .05, .9), "out_of_scope": (.05, .9, .05, .1),
        "unsure": (.2, .3, .5, .5), "invalid": (.9, .8, .1, .9),
    }[label]
    return {"strands_p_in_scope": p_in, "strands_p_out_of_scope": p_out, "strands_p_unsure": p_unsure,
            "strands_p_actual_use": actual, "strands_p_method_relevance": .1,
            "strands_p_microbial_only": .1, "strands_p_review": .1,
            "strands_scope_choice": label, "flag_model_path": model, "flag_confidence": .9,
            "strands_latency_ms": 10, "strands_input_tokens": 100}


class StagedScreenTests(unittest.TestCase):
    def test_routes_only_uncertain_short_invalid_and_error_rows_and_reuses_per_model_cache(self):
        labels = ["in_scope", "out_of_scope", "short", "unsure", "error", "empty", "invalid"]
        df = pd.DataFrame({"title": labels, "abstract": ["a" * 400, "a" * 400, "short", "a" * 400,
                                                          "a" * 400, "", "a" * 400]})
        events = []
        active: list[str] = []
        calls = []

        @contextmanager
        def servers(cfg, *args, **kwargs):
            self.assertFalse(active, "stage 1 must exit before stage 2 starts")
            self.assertEqual(kwargs["gpu_indices"], [0, 1])
            active.append(cfg.expected_model)
            events.append(("start", cfg.expected_model))
            try:
                yield cfg
            finally:
                events.append(("stop", active.pop()))

        def infer(session, state, cfg):
            title = state.split("\n", 1)[0].removeprefix("Title: ")
            calls.append((cfg.expected_model, title))
            if cfg.expected_model == STRANDS_MODEL:
                if title == "error":
                    raise RuntimeError("first-stage failed")
                return scores("out_of_scope" if title == "short" else title, STRANDS_MODEL)
            return scores("in_scope", OMNI_MODEL)

        with tempfile.TemporaryDirectory() as directory, \
                patch("screen.screening_servers", side_effect=servers), \
                patch("screen.gpu_progress", side_effect=lambda *a: nullcontext()), \
                patch("screen.check_health", return_value={"status": "ok"}), \
                patch("libs.strands_screening._evaluate_abstract", side_effect=infer), redirect_stdout(io.StringIO()):
            root = Path(directory)
            cfg = screen._select_models(ScreeningConfig(cache_csv=str(root / "cache.csv")), "strands", "clef_omni")
            out = root / "output.csv"
            screen._screen_staged(df, out, "abstract", cfg, Mock(), Mock(), enabled=True, startup_timeout=5, log_dir=root, gpu_indices=[0, 1])
            with out.open() as handle:
                first = list(csv.DictReader(handle))
            self.assertEqual(events, [("start", STRANDS_MODEL), ("stop", STRANDS_MODEL),
                                      ("start", OMNI_MODEL), ("stop", OMNI_MODEL)])
            self.assertEqual([title for model, title in calls if model == OMNI_MODEL], ["short", "unsure", "error", "invalid"])
            self.assertEqual([r["title"] for r in first], labels)
            self.assertEqual([r["evaluation_stage"] for r in first], ["first", "first", "second", "second", "second", "", "second"])
            self.assertEqual(first[4]["first_stage_error"], "first-stage failed")
            self.assertEqual(first[2]["latency_ms"], "20.0")
            self.assertFalse(any(h.startswith("strands_") for h in first[0]))
            calls.clear()
            screen._screen_staged(df, out, "abstract", cfg, Mock(), Mock(), enabled=True, startup_timeout=5, log_dir=root, gpu_indices=[0, 1])
            self.assertEqual(calls, [(STRANDS_MODEL, "error")], "successful per-model scores must be cached independently")
            with out.open() as handle:
                self.assertEqual(first, list(csv.DictReader(handle)))

    def test_second_startup_failure_preserves_first_stage_csv_and_no_second_when_all_decided(self):
        @contextmanager
        def servers(cfg, *args, **kwargs):
            if cfg.expected_model == OMNI_MODEL:
                raise RuntimeError("not enough VRAM")
            yield cfg

        with tempfile.TemporaryDirectory() as directory, \
                patch("screen.screening_servers", side_effect=servers) as startup, \
                patch("screen.gpu_progress", side_effect=lambda *a: nullcontext()), \
                patch("screen.check_health", return_value={"status": "ok"}), \
                patch("libs.strands_screening._evaluate_abstract", return_value=scores("unsure", STRANDS_MODEL)) as infer, \
                redirect_stdout(io.StringIO()):
            root = Path(directory)
            cfg = screen._select_models(ScreeningConfig(cache_csv=None), "strands", "clef_omni")
            df = pd.DataFrame({"title": ["study"], "abstract": ["a" * 400]})
            out = root / "output.csv"
            with self.assertRaisesRegex(RuntimeError, "VRAM"):
                screen._screen_staged(df, out, "abstract", cfg, Mock(), Mock(), enabled=True, startup_timeout=5, log_dir=root)
            with out.open() as handle:
                saved = next(csv.DictReader(handle))
            self.assertEqual(saved["flag_label"], "unsure")
            self.assertEqual(saved["evaluation_stage"], "pending")
            self.assertEqual(saved["first_stage_model"], STRANDS_MODEL)
            startup.reset_mock()
            infer.return_value = scores("in_scope", STRANDS_MODEL)
            screen._screen_staged(df, out, "abstract", cfg, Mock(), Mock(), enabled=True, startup_timeout=5, log_dir=root)
            startup.assert_called_once()


if __name__ == "__main__":
    unittest.main()
