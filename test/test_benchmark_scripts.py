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

from evaluate_strands_benchmark import evaluate


class BenchmarkScriptsTest(unittest.TestCase):
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
