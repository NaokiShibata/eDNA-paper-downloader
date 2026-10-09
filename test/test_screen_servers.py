from __future__ import annotations

import logging
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "script"))

from libs.screen_servers import (
    STRANDS_MODEL,
    _free_url,
    _gpu,
    _gpu_status,
    _ready,
    _stop,
    _terminate,
    gpu_progress,
    screening_servers,
)
from libs.strands_screening import ScreeningConfig


class ScreenServersTests(unittest.TestCase):
    def setUp(self):
        self.cfg = ScreeningConfig(base_url="http://127.0.0.1:8014", expected_model="clef-27b-q8",
                                   first_stage_base_url="http://127.0.0.1:8012", first_stage_model=STRANDS_MODEL)
        self.logger = Mock(spec=logging.Logger)

    def test_gpu_status_reports_gpu_total_for_selected_device(self):
        with patch("libs.screen_servers.subprocess.run", return_value=Mock(stdout="0, GPU-A, RTX 8000, 24576, 49152, 72\n")):
            status = _gpu_status(self.cfg.base_url, 0)
        self.assertEqual(status, "gpu=0 gpu_name=RTX 8000 gpu_util=72% vram=24576/49152 MiB (50.0%, GPU total)")

    def test_reused_server_gpu_is_identified_by_process_port(self):
        results = [Mock(stdout="0, GPU-A, RTX 8000, 24576, 49152, 72\n1, GPU-B, RTX 5060 Ti, 2048, 16384, 5\n"),
                   Mock(stdout="123, GPU-A\n")]
        with patch("libs.screen_servers.subprocess.run", side_effect=results), \
                patch("libs.screen_servers.Path.read_bytes", return_value=b"llama-server\0--port\08014\0"):
            self.assertIn("gpu_name=RTX 8000", _gpu_status(self.cfg.base_url))

    def test_gpu_status_failure_does_not_break_screening(self):
        with patch("libs.screen_servers.subprocess.run", side_effect=FileNotFoundError):
            self.assertEqual(_gpu_status(self.cfg.base_url), "gpu=unknown vram=unavailable")
        with patch("libs.screen_servers.subprocess.run") as query:
            self.assertIn("remote server", _gpu_status("https://example.com:8014"))
            query.assert_not_called()

    def test_gpu_progress_updates_during_inference_and_stops(self):
        updated_twice = threading.Event()
        samples = []

        def update(value):
            samples.append(value)
            if len(samples) >= 2:
                updated_twice.set()

        with patch("libs.screen_servers._gpu_status", return_value="gpu_util=90%"):
            with gpu_progress([self.cfg.base_url], update):
                self.assertTrue(updated_twice.wait(3), "GPU status must update while the main thread waits")
        self.assertEqual(samples, ["gpu_util=90%", "gpu_util=90%"])
        self.assertFalse(any(t.name == "screen-gpu-status" for t in threading.enumerate()))

    def test_existing_servers_are_never_started_or_stopped(self):
        with patch("libs.screen_servers._ready", return_value=True), \
                patch("libs.screen_servers._gpu_status", return_value="gpu=0 vram=50%"), \
                patch("libs.screen_servers.subprocess.Popen") as start, \
                patch("libs.screen_servers._stop") as stop:
            with screening_servers(self.cfg, Mock(), self.logger, Path("logs")) as cfg:
                self.assertEqual(cfg, self.cfg)
            start.assert_not_called()
            stop.assert_not_called()
            self.assertTrue(all(call.args[-1] == "gpu=0 vram=50%" for call in self.logger.info.call_args_list))

    def test_disabled_does_not_probe_or_start(self):
        with patch("libs.screen_servers._ready") as probe, \
                patch("libs.screen_servers._gpu_status", return_value="gpu=unknown vram=unavailable"):
            with screening_servers(self.cfg, Mock(), self.logger, Path("logs"), enabled=False) as cfg:
                self.assertEqual(cfg, self.cfg)
            probe.assert_not_called()

    def test_owned_first_stage_stops_even_on_interrupt(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            stack.enter_context(patch("libs.screen_servers._gpu_status", return_value="gpu=0 vram=50%"))
            stack.enter_context(patch("libs.screen_servers._ready", side_effect=[True, False, True]))
            stack.enter_context(patch("libs.screen_servers._free_url", return_value="http://127.0.0.1:19012"))
            stack.enter_context(patch("libs.screen_servers._command", return_value=(["strands-decider"], 6144)))
            gpu = stack.enter_context(patch("libs.screen_servers._gpu", return_value=0))
            start = stack.enter_context(patch("libs.screen_servers.subprocess.Popen"))
            start.return_value.poll.return_value = None
            stop = stack.enter_context(patch("libs.screen_servers._stop"))
            with self.assertRaises(KeyboardInterrupt):
                with screening_servers(self.cfg, Mock(), self.logger, Path(directory)) as cfg:
                    self.assertEqual(cfg.base_url, self.cfg.base_url)
                    self.assertEqual(cfg.first_stage_base_url, "http://127.0.0.1:19012")
                    self.assertEqual(start.call_args.kwargs["env"]["CUDA_VISIBLE_DEVICES"], "0")
                    raise KeyboardInterrupt
            gpu.assert_called_once_with(6144, [self.cfg.base_url])
            stop.assert_called_once_with(start.return_value, self.logger)

    def test_partial_start_failure_cleans_up_previous_server(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            stack.enter_context(patch("libs.screen_servers._gpu_status", return_value="gpu=0 vram=50%"))
            stack.enter_context(patch("libs.screen_servers._ready", side_effect=[False, False, True]))
            stack.enter_context(patch("libs.screen_servers._free_url", side_effect=lambda url: url))
            stack.enter_context(patch("libs.screen_servers._command", return_value=(["server"], 100)))
            gpu = stack.enter_context(patch("libs.screen_servers._gpu", return_value=0))
            process = Mock()
            process.poll.return_value = None
            stack.enter_context(patch("libs.screen_servers.subprocess.Popen", side_effect=[process, OSError("failed start")]))
            stop = stack.enter_context(patch("libs.screen_servers._stop"))
            with self.assertRaisesRegex(RuntimeError, "could not start server"):
                with screening_servers(self.cfg, Mock(), self.logger, Path(directory)):
                    self.fail("startup should have failed")
            stop.assert_called_once_with(process, self.logger)
            gpu.assert_called_once_with(200, [])

    def test_startup_timeout_stops_owned_server(self):
        previous_handler = signal.getsignal(signal.SIGTERM)
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            stack.enter_context(patch("libs.screen_servers._gpu_status", return_value="gpu=0 vram=50%"))
            stack.enter_context(patch("libs.screen_servers._ready", side_effect=[True, False, False]))
            stack.enter_context(patch("libs.screen_servers._free_url", side_effect=lambda url: url))
            stack.enter_context(patch("libs.screen_servers._command", return_value=(["server"], 100)))
            stack.enter_context(patch("libs.screen_servers._gpu", return_value=0))
            stack.enter_context(patch("libs.screen_servers.time.monotonic", side_effect=[0, 2]))
            start = stack.enter_context(patch("libs.screen_servers.subprocess.Popen"))
            start.return_value.poll.return_value = None
            stop = stack.enter_context(patch("libs.screen_servers._stop"))
            with self.assertRaisesRegex(RuntimeError, "did not become ready"):
                with screening_servers(self.cfg, Mock(), self.logger, Path(directory), startup_timeout=1):
                    self.fail("should time out")
            stop.assert_called_once()
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous_handler)

    def test_sigterm_exits_through_context_cleanup(self):
        with self.assertRaises(SystemExit) as exit_status:
            _terminate(signal.SIGTERM, None)
        self.assertEqual(exit_status.exception.code, 143)

    def test_occupied_port_uses_another_without_touching_listener(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            selected = _free_url(f"http://127.0.0.1:{port}")
            self.assertNotEqual(selected, f"http://127.0.0.1:{port}")
            self.assertEqual(listener.getsockname()[1], port)
        with self.assertRaisesRegex(RuntimeError, "cannot automatically start"):
            _free_url("https://example.com:8012")

    def test_gpu_selection_respects_existing_stage_and_memory(self):
        query = [Mock(stdout="0, GPU-A, 20000\n1, GPU-B, 30000\n"), Mock(stdout="123, GPU-A\n")]
        with patch("libs.screen_servers.subprocess.run", side_effect=query), \
                patch("libs.screen_servers.Path.read_bytes", return_value=b"llama-server\0--port\08014\0"):
            self.assertEqual(_gpu(6144, [self.cfg.base_url]), 0)
        with patch("libs.screen_servers.subprocess.run", side_effect=query), \
                patch("libs.screen_servers.Path.read_bytes", return_value=b"llama-server\0--port\08014\0"):
            with self.assertRaisesRegex(RuntimeError, "no suitable GPU"):
                _gpu(25000, [self.cfg.base_url])
        with patch("libs.screen_servers.subprocess.run", side_effect=[query[0], Mock(stdout="")]):
            with self.assertRaisesRegex(RuntimeError, "cannot identify the GPU"):
                _gpu(6144, [self.cfg.base_url])

    def test_wrong_model_is_not_replaced(self):
        with patch("libs.screen_servers.check_health", return_value={"model": "another-model"}):
            with self.assertRaisesRegex(RuntimeError, "expected"):
                _ready(Mock(), self.cfg.base_url, self.cfg.expected_model)

    def test_stop_terminates_owned_process_group_and_escalates(self):
        process = Mock(pid=123)
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("server", 10), 0]
        with patch("libs.screen_servers.os.killpg") as kill:
            _stop(process, self.logger)
        self.assertEqual(kill.call_args_list[0].args, (123, signal.SIGTERM))
        self.assertEqual(kill.call_args_list[1].args, (123, signal.SIGKILL))


if __name__ == "__main__":
    unittest.main()
