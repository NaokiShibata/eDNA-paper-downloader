"""Own only the local servers started for one screen invocation."""

from __future__ import annotations

import csv
import logging
import os
import shutil
import signal
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import FrameType
from typing import Any
from urllib.parse import urlsplit

import requests

from libs.strands_screening import DEFAULT_BASE_URL, ScreeningConfig, check_health

ROOT = Path(__file__).resolve().parents[2]
STRANDS_MODEL = "strands-decider-2B-hobson-v19"


def _ready(session: requests.Session, url: str, model: str | None) -> bool:
    try:
        health = check_health(session, url, 2)
    except (RuntimeError, ValueError):
        return False
    actual = health.get("model")
    if actual is not None and model is not None and actual != model:
        raise RuntimeError(f"server at {url} has model {actual!r}; expected {model!r}")
    return True


def _free_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme != "http" or parts.hostname not in ("localhost", "127.0.0.1") or parts.path.strip("/"):
        raise RuntimeError(f"cannot automatically start a server at {url}; use --no-auto-server for remote servers")
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", parts.port or 80))
        except OSError:
            sock.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{sock.getsockname()[1]}"


def _command(model: str | None, url: str) -> tuple[list[str], int]:
    port = str(urlsplit(url).port)
    if model == STRANDS_MODEL:
        binary = shutil.which("strands-decider")
        if binary is None:
            raise RuntimeError("strands-decider is missing; run screen through pixi")
        return [binary, "serve", f"StrandsAgents/{model}", "--device", "cuda", "--host", "127.0.0.1", "--port", port], 6144
    files = {
        "clef-27b-q8": "Clef-Q8_0.gguf",
        "ggml-org/Clef-Flash-GGUF:Q8_0": "Clef-Flash-Q8_0.gguf",
    }
    if model not in files:
        raise RuntimeError(f"no automatic startup command for model {model!r}; start it manually or use --no-auto-server")
    weights = ROOT / ".cache" / "clef" / files[model]
    binary = shutil.which("llama-server")
    if binary is None or not weights.is_file():
        raise RuntimeError(f"automatic startup needs llama-server and {weights}; see docs/clef-default.md")
    command = [binary, "-m", str(weights), "--alias", model, "--host", "127.0.0.1", "--port", port,
               "-ngl", "99", "-c", "8192", "-b", "8192", "-ub", "8192", "-np", "1"]
    return command, (weights.stat().st_size + 1024**2 - 1) // 1024**2 + 3072


def _gpu(required_mib: int, reused_urls: list[str]) -> int:
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,uuid,memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, check=True, timeout=10,
        )
        devices = {row[1].strip(): (int(row[0]), int(row[2])) for row in csv.reader(result.stdout.splitlines())}
        # Keep newly started models on the GPU of an existing local stage.
        processes = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=pid,gpu_uuid", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, check=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise RuntimeError(f"cannot inspect GPU free memory: {exc}") from exc
    pinned = set()
    ports = {str(urlsplit(url).port) for url in reused_urls if urlsplit(url).hostname in ("localhost", "127.0.0.1")}
    for row in csv.reader(processes.stdout.splitlines()):
        if len(row) != 2 or row[1].strip() not in devices:
            continue
        try:
            argv = Path(f"/proc/{int(row[0])}/cmdline").read_bytes().decode().split("\0")
        except (OSError, ValueError):
            continue
        if any(arg == "--port" and following in ports for arg, following in zip(argv, argv[1:], strict=False)):
            pinned.add(row[1].strip())
    if len(pinned) > 1:
        raise RuntimeError("existing screening servers use different GPUs; automatic startup requires one GPU")
    if ports and not pinned:
        raise RuntimeError("cannot identify the GPU of an existing local screening server; start missing stages manually and use --no-auto-server")
    candidates = [value for uuid, value in devices.items() if not pinned or uuid in pinned]
    fitting = [(index, free) for index, free in candidates if free >= required_mib]
    if not fitting:
        raise RuntimeError(f"no suitable GPU has the estimated {required_mib} MiB free for missing servers; existing servers were left running")
    return max(fitting, key=lambda item: item[1])[0]


def _stop(process: subprocess.Popen[Any], logger: logging.Logger) -> None:
    if process.poll() is not None:
        return
    logger.info("stopping owned screening server pid=%s", process.pid)
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)
    except ProcessLookupError:
        process.wait(timeout=10)


def _terminate(signum: int, _frame: FrameType | None) -> None:
    raise SystemExit(128 + signum)


@contextmanager
def screening_servers(
    cfg: ScreeningConfig, session: requests.Session, logger: logging.Logger, log_dir: Path,
    *, enabled: bool = True, startup_timeout: float = 180,
) -> Iterator[ScreeningConfig]:
    if not enabled:
        yield cfg
        return
    model = cfg.expected_model or (STRANDS_MODEL if cfg.base_url == DEFAULT_BASE_URL else None)
    stages = [("base_url", cfg.base_url, model)]
    if cfg.first_stage_base_url is not None:
        stages.append(("first_stage_base_url", cfg.first_stage_base_url, cfg.first_stage_model))
    missing = []
    reused = []
    for field, url, model in stages:
        if _ready(session, url, model):
            reused.append(url)
            logger.info("reusing screening server model=%s url=%s", model, url)
        else:
            selected = _free_url(url)
            command, memory = _command(model, selected)
            missing.append((field, selected, model, command, memory))
    if not missing:
        yield cfg
        return
    gpu = _gpu(sum(item[4] for item in missing), reused)
    owned: list[subprocess.Popen[Any]] = []
    log_dir.mkdir(parents=True, exist_ok=True)
    previous_handler = signal.getsignal(signal.SIGTERM)
    manage_sigterm = threading.current_thread() is threading.main_thread()
    try:
        if manage_sigterm:
            signal.signal(signal.SIGTERM, _terminate)
        for field, url, model, command, _ in missing:
            env = os.environ.copy()
            env["CUDA_VISIBLE_DEVICES"] = str(gpu)
            env["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"
            # Conda's ptxas can compile Blackwell kernels even without this alias.
            ptxas = shutil.which("ptxas")
            if ptxas is not None:
                env.setdefault("TRITON_PTXAS_BLACKWELL_PATH", ptxas)
            log = log_dir / f"screen-server-{os.getpid()}-{urlsplit(url).port}.log"
            logger.info("starting model=%s gpu=%s url=%s log=%s", model, gpu, url, log)
            with log.open("w") as handle:
                try:
                    process = subprocess.Popen(command, env=env, stdout=handle, stderr=subprocess.STDOUT, start_new_session=True)
                except OSError as exc:
                    raise RuntimeError(f"could not start server for {model}: {exc}; see {log}") from exc
            owned.append(process)
            cfg = replace(cfg, base_url=url) if field == "base_url" else replace(cfg, first_stage_base_url=url)
            deadline = time.monotonic() + startup_timeout
            while True:
                if process.poll() is not None:
                    raise RuntimeError(f"server for {model} exited with code {process.returncode}; see {log}")
                if _ready(session, url, model):
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"server for {model} did not become ready within {startup_timeout}s; see {log}")
                time.sleep(0.5)
        yield cfg
    finally:
        for process in reversed(owned):
            try:
                _stop(process, logger)
            except (OSError, subprocess.SubprocessError):
                logger.exception("could not stop owned screening server pid=%s", process.pid)
        if manage_sigterm:
            signal.signal(signal.SIGTERM, previous_handler)
