from __future__ import annotations

import json
import sys
import tempfile
from contextlib import nullcontext
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "script"))
import screen
from libs.screen_servers import STRANDS_MODEL
from libs.strands_screening import ScreeningConfig


class ScreenModesTests(TestCase):
    def setUp(self):
        self.cfg = ScreeningConfig(base_url="http://127.0.0.1:19014", expected_model="clef-27b-q8",
                                   batch_questions=True, first_stage_base_url="http://127.0.0.1:19012",
                                   first_stage_model=STRANDS_MODEL, first_stage_batch_questions=False)

    def test_modes_preserve_configured_endpoints_and_thresholds(self):
        strands = screen._select_mode(self.cfg, "strands")
        self.assertEqual(strands.base_url, self.cfg.first_stage_base_url)
        self.assertEqual(strands.expected_model, STRANDS_MODEL)
        self.assertFalse(strands.batch_questions)
        self.assertIsNone(strands.first_stage_base_url)
        clef = screen._select_mode(self.cfg, "clef")
        self.assertEqual(clef.base_url, self.cfg.base_url)
        self.assertEqual(clef.expected_model, "clef-27b-q8")
        self.assertIsNone(clef.first_stage_base_url)
        self.assertTrue(clef.batch_questions)
        self.assertEqual(screen._select_mode(self.cfg, "both"), self.cfg)
        self.assertEqual(screen._select_mode(self.cfg, None), self.cfg)
        self.assertEqual(strands.exclude_microbial_only_min, self.cfg.exclude_microbial_only_min)

    def test_both_can_be_selected_from_strands_configuration(self):
        cfg = screen._select_mode(ScreeningConfig(), "both")
        self.assertEqual(cfg.base_url, "http://127.0.0.1:8014")
        self.assertEqual(cfg.first_stage_base_url, "http://127.0.0.1:8012")
        self.assertFalse(cfg.first_stage_batch_questions)

    def test_cli_modes_and_alias_reach_only_selected_servers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input.csv"
            source.write_text("title,abstract\nStudy,Abstract\n")
            config = root / "config.json"
            config.write_text(json.dumps({"base_url": self.cfg.base_url, "expected_model": self.cfg.expected_model,
                                          "first_stage_base_url": self.cfg.first_stage_base_url,
                                          "first_stage_model": self.cfg.first_stage_model}))
            args = [str(source), "--config", str(config), "--log-file", str(root / "screen.log")]
            for flag, model, cascade in [("--strands", STRANDS_MODEL, False), ("--strandes", STRANDS_MODEL, False),
                                         ("--clef", "clef-27b-q8", False), ("--both", "clef-27b-q8", True)]:
                with self.subTest(flag=flag), \
                        patch("screen.screening_servers", side_effect=lambda cfg, *a, **kw: nullcontext(cfg)), \
                        patch("screen._screen") as execute:
                    result = CliRunner().invoke(screen.app, args + [flag])
                    self.assertEqual(result.exit_code, 0, result.output)
                    cfg = execute.call_args.args[3]
                    self.assertEqual(cfg.expected_model, model)
                    self.assertEqual(cfg.first_stage_base_url is not None, cascade)
            result = CliRunner().invoke(screen.app, args + ["--strands", "--clef"])
            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("choose only one", result.output)
