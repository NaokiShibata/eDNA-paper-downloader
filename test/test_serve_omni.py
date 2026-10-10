from __future__ import annotations

import sys
import threading
import unittest
from http.server import HTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "script"))
from libs.screen_servers import OMNI_MODEL
from libs.strands_screening import QUESTIONS, extract_scores
from serve_omni import handler_for


class OmniAdapterTests(unittest.TestCase):
    def setUp(self):
        self.release = Mock()
        self.release.systemone.return_value = {
            "model": OMNI_MODEL, "answers": {
                "scope": {"choice": "in_scope", "confidence": .9, "probabilities": {"in_scope": .9, "out_of_scope": .05, "unsure": .05}},
                "actual_use": {"noul": .9}, "microbial_only": {"noul": .1}, "method_relevance": {"noul": .8},
                "study_type": {"probabilities": {"primary_research": .9, "review": .05, "other": .05}},
            }, "usage": {"input_tokens": 100, "output_tokens": 0},
        }
        handler = handler_for(Mock(), Mock(), self.release)
        quiet = patch.object(handler, "log_message")
        quiet.start()
        self.addCleanup(quiet.stop)
        self.server = HTTPServer(("127.0.0.1", 0), handler)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=5)

    def test_model_is_injected_and_response_matches_screen_contract(self):
        health = requests.get(self.url + "/health", timeout=5).json()
        self.assertEqual(health["model"], OMNI_MODEL)
        self.assertEqual(health["status"], "ok")
        response = requests.post(self.url + "/v1/systemone", json={"state": "abstract", "questions": QUESTIONS}, timeout=5)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.release.systemone.call_args.args[2]["model"], OMNI_MODEL)
        parsed = extract_scores(response.json())
        self.assertEqual(parsed["flag_model_path"], OMNI_MODEL)
        self.assertEqual(parsed["strands_p_in_scope"], .9)

    def test_wrong_model_nonobject_and_media_are_rejected_before_inference(self):
        cases: list[Any] = [{"model": "different"}, [], {"images": ["https://example.com/image"]}]
        for request in cases:
            with self.subTest(request=request):
                response = requests.post(self.url + "/v1/systemone", json=request, timeout=5)
                self.assertEqual(response.status_code, 400)
        self.release.systemone.assert_not_called()

    def test_invalid_request_and_inference_failure_return_json_without_stopping_server(self):
        self.release.systemone.side_effect = ValueError("state missing")
        self.assertEqual(requests.post(self.url + "/v1/systemone", json={}, timeout=5).status_code, 400)
        self.release.systemone.side_effect = RuntimeError("inference failed")
        with self.assertLogs(level="ERROR"):
            response = requests.post(self.url + "/v1/systemone", json={}, timeout=5)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["error"], "inference failed")
        self.assertEqual(requests.get(self.url + "/health", timeout=5).status_code, 200)


if __name__ == "__main__":
    unittest.main()
