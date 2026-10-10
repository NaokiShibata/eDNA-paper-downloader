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
from libs.screen_servers import MODEL_CHOICES, OMNI_MODEL, STRANDS_MODEL
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

    def test_model_selection_keeps_matching_urls_and_can_reverse_stages(self):
        cfg = screen._select_models(self.cfg, "strands", "clef")
        self.assertEqual(cfg, self.cfg)
        reverse = screen._select_models(self.cfg, "clef", "strands")
        self.assertEqual(reverse.first_stage_base_url, self.cfg.base_url)
        self.assertEqual(reverse.first_stage_model, self.cfg.expected_model)
        self.assertEqual(reverse.base_url, self.cfg.first_stage_base_url)
        self.assertFalse(reverse.batch_questions)
        self.assertTrue(reverse.first_stage_batch_questions)
        omni = screen._select_models(self.cfg, "strands", "clef_omni")
        self.assertEqual(omni.expected_model, OMNI_MODEL)
        self.assertEqual(omni.base_url, "http://127.0.0.1:8016")
        self.assertEqual(omni.first_stage_base_url, self.cfg.first_stage_base_url)
        self.assertEqual(omni.exclude_microbial_only_min, self.cfg.exclude_microbial_only_min)
        for name, (model, _, batch) in MODEL_CHOICES.items():
            single = screen._select_models(self.cfg, name)
            self.assertEqual(single.expected_model, model)
            self.assertEqual(single.batch_questions, batch)
            self.assertIsNone(single.first_stage_base_url)

    def test_model_cli_routes_omni_pairs_to_staged_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input.csv"
            source.write_text("title,abstract\nStudy,Abstract\n")
            config = root / "config.json"
            config.write_text("{}")
            args = [str(source), "--config", str(config), "--log-file", str(root / "screen.log")]
            for names in [["strands"], ["clef"], ["clef_flash"], ["clef_omni"], ["clef", "strands"]]:
                selected = ["--model1", names[0]] + (["--model2", names[1]] if len(names) == 2 else [])
                with self.subTest(names=names), \
                        patch("screen.screening_servers", side_effect=lambda cfg, *a, **kw: nullcontext(cfg)), \
                        patch("screen._screen") as execute:
                    result = CliRunner().invoke(screen.app, args + selected)
                    self.assertEqual(result.exit_code, 0, result.output)
                    cfg = execute.call_args.args[3]
                    self.assertEqual(cfg.expected_model, MODEL_CHOICES[names[-1]][0])
                    self.assertEqual(cfg.first_stage_base_url is not None, len(names) == 2)
            with patch("screen._screen_staged") as staged, patch("screen.screening_servers") as servers:
                result = CliRunner().invoke(screen.app, args + ["--model1", "strands", "--model2", "clef_omni"])
                self.assertEqual(result.exit_code, 0, result.output)
                cfg = staged.call_args.args[3]
                self.assertEqual(cfg.expected_model, OMNI_MODEL)
                self.assertEqual(cfg.first_stage_model, STRANDS_MODEL)
                servers.assert_not_called()
            for invalid in [["--model2", "clef"], ["--model1", "unknown"],
                            ["--model1", "strands", "--both"], ["--model1", "clef", "--model2", "clef"]]:
                with self.subTest(invalid=invalid), patch("screen.screening_servers") as servers:
                    result = CliRunner().invoke(screen.app, args + invalid)
                    self.assertNotEqual(result.exit_code, 0, result.output)
                    servers.assert_not_called()

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
