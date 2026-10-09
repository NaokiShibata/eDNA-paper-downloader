import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "script"))

from benchmark_clef import MODELS, infer, validate_answers  # noqa: E402
from libs.strands_screening import QUESTIONS  # noqa: E402


class ClefValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.data: dict[str, Any] = {"answers": {
            key: ({"noul": 0.5} if question["type"] == "noul" else
                  {"probabilities": {option: 1 / len(question["criteria"])
                                     for option in question["criteria"]}})
            for key, question in QUESTIONS.items()
        }}

    def test_valid(self) -> None:
        validate_answers(self.data)

    def test_invalid_boolean(self) -> None:
        for value in [float("nan"), float("inf"), -0.1, 1.1]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                data = copy.deepcopy(self.data)
                data["answers"]["actual_use"]["noul"] = value
                validate_answers(data)

    def test_missing_option(self) -> None:
        del self.data["answers"]["scope"]["probabilities"]["in_scope"]
        with self.assertRaises(KeyError):
            validate_answers(self.data)

    def test_unnormalized(self) -> None:
        self.data["answers"]["scope"]["probabilities"]["in_scope"] = 0.8
        with self.assertRaises(ValueError):
            validate_answers(self.data)

    def test_wrong_server_does_not_write_predictions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, model, output = root / "input.csv", root / "model.gguf", root / "out.csv"
            source.write_text("benchmark_record_id,abstract\nB1,River water sampled for fish DNA\n")
            model.write_bytes(b"model")
            with (patch.dict(MODELS, {"flash": MODELS["flash"] | {"sha256": hashlib.sha256(b"model").hexdigest()}}),
                  patch("benchmark_clef.check_health"),
                  patch("benchmark_clef.requests.Session") as session):
                response = session.return_value.__enter__.return_value.post.return_value
                response.json.return_value = self.data | {"model": "strands-decider"}
                with self.assertRaisesRegex(ValueError, "model alias"):
                    infer(source, output, "http://localhost:8014", model)
            self.assertFalse(output.exists())

    def test_27b_identity_and_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, model, output = root / "input.csv", root / "model.gguf", root / "out.csv"
            source.write_text("benchmark_record_id,abstract\nB1,River water sampled for fish DNA\n")
            model.write_bytes(b"model")
            with (patch.dict(MODELS, {"27b": MODELS["27b"] | {"sha256": hashlib.sha256(b"model").hexdigest()}}),
                  patch("benchmark_clef.check_health"),
                  patch("benchmark_clef.requests.Session") as session):
                post = session.return_value.__enter__.return_value.post
                post.return_value.json.return_value = self.data | {"model": "clef-27b-q8"}
                infer(source, output, "http://localhost:8014", model, "27b")
                self.assertEqual(post.call_args.kwargs["json"]["model"], "clef-27b-q8")
            provenance = json.loads(output.with_suffix(".run.json").read_text())
            self.assertEqual(provenance["model"], "ggml-org/Clef-GGUF:Q8_0")
            self.assertEqual(provenance["revision"], MODELS["27b"]["revision"])
