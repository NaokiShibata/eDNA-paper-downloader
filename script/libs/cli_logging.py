from __future__ import annotations

import logging
import platform
import shlex
import socket
import sys
from datetime import datetime
from pathlib import Path

import typer

try:
    from rich.console import Console
    from rich.logging import RichHandler
except Exception:  # pragma: no cover
    Console = None
    RichHandler = None


def setup_logger(
    logger_name: str,
    log_level: str = "INFO",
    log_file: Path | None = None,
    stream: str = "stdout",
) -> logging.Logger:
    logger = logging.getLogger(logger_name)
    logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))
    logger.propagate = False

    if logger.handlers:
        logger.handlers.clear()

    fmt = logging.Formatter(
        fmt="%(asctime)s\t%(levelname)s\t%(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    output_stream = sys.stderr if stream == "stderr" else sys.stdout
    if RichHandler is not None and Console is not None:
        console = Console(file=output_stream)
        sh = RichHandler(
            console=console,
            show_time=True,
            show_level=True,
            show_path=False,
            rich_tracebacks=True,
            markup=False,
        )
        sh.setFormatter(logging.Formatter("%(message)s"))
    else:
        sh = logging.StreamHandler(output_stream)
        sh.setFormatter(fmt)
    sh.setLevel(logger.level)
    logger.addHandler(sh)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        fh.setLevel(logger.level)
        logger.addHandler(fh)

    return logger


def log_run_header(
    logger: logging.Logger,
    params: dict,
    log_file: Path | None = None,
    param_width: int = 16,
    versions: dict[str, str] | None = None,
    fallback_command: str = "python script",
) -> None:
    try:
        import getpass

        user = getpass.getuser()
    except Exception:
        user = "unknown"

    try:
        ctx = typer.get_current_context()
        command_path = ctx.command_path
    except Exception:
        command_path = fallback_command

    redacted_cli_parts: list[str] = [command_path]
    for key in sorted(params.keys()):
        value = params[key]
        if value is None or value == []:
            continue
        option = f"--{key.replace('_', '-')}"
        if isinstance(value, bool):
            if value:
                redacted_cli_parts.append(option)
            continue
        if isinstance(value, list):
            for item in value:
                redacted_cli_parts.append(f"{option} {shlex.quote(str(item))}")
            continue
        redacted_cli_parts.append(f"{option} {shlex.quote(str(value))}")
    redacted_command = " ".join(redacted_cli_parts)

    header_lines = [
        "=" * 80,
        "RUN HEADER",
        f"timestamp   : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"user        : {user}",
        f"hostname    : {socket.gethostname()}",
        f"platform    : {platform.platform()}",
        f"python      : {sys.version.split()[0]}",
        f"cwd         : {Path.cwd()}",
        f"command     : {redacted_command}",
        f"log_file    : {str(log_file) if log_file else '(console only)'}",
        "-" * 80,
        "PARAMETERS",
    ]
    for k in sorted(params.keys()):
        header_lines.append(f"{k:{param_width}s}: {params[k]}")

    if versions:
        header_lines += ["-" * 80, "VERSIONS"]
        for k in sorted(versions.keys()):
            header_lines.append(f"{k:12s}: {versions[k]}")

    header_lines.append("=" * 80)
    for line in header_lines:
        logger.info(line)
