from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "script"))

import pandas as pd
from typer.testing import CliRunner

from benchmark import _binary_metrics, app, evaluate


class BenchmarkScriptsTest(unittest.TestCase):
    def test_binary_metrics_review_and_zero_denominator(self) -> None:
        metrics = _binary_metrics(
            pd.Series(["in_scope", "in_scope", "out_of_scope", "out_of_scope"]),
            pd.Series(["out_of_scope", "process_error", "in_scope", "unsure"]),
        )
        self.assertEqual(metrics["hard_false_negative"], 1)
        self.assertEqual(metrics["manual_review"], 2)
        self.assertEqual(metrics["auto_coverage"], 0.5)
        self.assertEqual(_binary_metrics(pd.Series(dtype=str), pd.Series(dtype=str))["auto_accuracy"], 0)

    def test_tune_uses_config_inclusion_thresholds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gold = root / "gold.csv"
            pred = root / "pred.csv"
            manifest = root / "manifest.csv"
            config = root / "config.json"
            output = root / "result.json"
            gold.write_text("benchmark_record_id,gold_label\na,in_scope\n")
            pred.write_text(
                "flag_record_id,strands_p_in_scope,strands_p_out_of_scope,"
                "strands_p_actual_use,strands_p_microbial_only,strands_p_method_relevance\n"
                "a,0.5,0.1,0.7,0.1,0.9\n"
            )
            manifest.write_text("benchmark_record_id,benchmark_partition\na,calibration\n")
            config.write_text(json.dumps({"include_threshold": 0.8,
                                         "include_microbial_only_max": 0.2,
                                         "actual_use_threshold": 0.9}))
            result = CliRunner().invoke(app, ["tune", str(gold), str(pred),
                "--manifest", str(manifest), "--config", str(config), "--out-json", str(output)])
            self.assertEqual(result.exit_code, 0, result.output)
            payload = json.loads(output.read_text())
            self.assertEqual(payload["fixed_include_threshold"], 0.8)
            self.assertEqual(payload["fixed_include_microbial_only_max"], 0.2)
            self.assertEqual(payload["fixed_actual_use_threshold"], 0.9)
            self.assertEqual(payload["best"]["pos_review"], 1)

    def test_evaluate_counts_only_last_prediction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            gold_csv = Path(directory) / "gold.csv"
            predictions_csv = Path(directory) / "predictions.csv"
            out_json = Path(directory) / "metrics.json"
            gold_csv.write_text(
                "benchmark_record_id,gold_label\ndoi:10.1000/abc,in_scope\n",
                encoding="utf-8",
            )
            predictions_csv.write_text(
                "flag_record_id,flag_label\n"
                "doi:10.1000/abc,process_error\n"
                "doi:10.1000/abc,in_scope\n",
                encoding="utf-8",
            )
            output = io.StringIO()
            with redirect_stdout(output):
                evaluate(
                    gold_csv=gold_csv,
                    predictions_csv=predictions_csv,
                    manifest_csv=None,
                    partition="all",
                    out_errors=None,
                    out_json=out_json,
                )
            metrics = json.loads(out_json.read_text(encoding="utf-8"))["metrics"]
            self.assertEqual(metrics["n_binary_gold"], 1)
            self.assertEqual(metrics["tp"], 1)
            self.assertEqual(metrics["manual_review"], 0)
            self.assertIn("Dropped 1 duplicate prediction rows", output.getvalue())


if __name__ == "__main__":
    unittest.main()
