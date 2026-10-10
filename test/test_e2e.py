from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import typer
from typer.testing import CliRunner

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "script"))

import e2e
from fetch import DEFAULT_QUERY


class E2ETest(unittest.TestCase):
    def test_fetch_call_and_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def fetch_outputs(**kwargs: object) -> None:
                columns = ["title", "abstract", "flag_label", "p_in_scope",
                           "p_out_of_scope", "p_actual_use",
                           "p_method_relevance"]
                pd.DataFrame([["title", "abstract", "in_scope", 1, 0, 1, 1]],
                             columns=columns).to_csv(root / "demo.csv", index=False)
                pd.DataFrame(columns=columns).to_csv(root / "demo.rejected.csv", index=False)

            with patch("e2e.check_health", return_value={}), \
                    patch("e2e.fetch", side_effect=fetch_outputs) as fetch:
                result = CliRunner().invoke(e2e.app, ["--email", "a@example.com",
                    "--out-dir", str(root), "--prefix", "demo", "--days", "3",
                    "--until-date", "2026-10-04"])
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual(fetch.call_args.kwargs["query"], DEFAULT_QUERY)
            self.assertEqual(fetch.call_args.kwargs["max_items"], 1000)
            self.assertTrue(fetch.call_args.kwargs["strands"])
            summary = json.loads((root / "demo_summary.json").read_text())
            self.assertEqual(summary["window"]["since"], "2026-10-02")
            self.assertEqual(summary["labels"]["in_scope"], 1)

    def test_fetch_failures_exit_nonzero(self) -> None:
        for failure in (typer.Exit(0), typer.Exit(2), RuntimeError("failed")):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                with patch("e2e.check_health", return_value={}), \
                        patch("e2e.fetch", side_effect=failure):
                    result = CliRunner().invoke(e2e.app, ["--email", "a@example.com",
                        "--out-dir", directory])
                self.assertNotEqual(result.exit_code, 0)
