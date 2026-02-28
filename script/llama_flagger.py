from __future__ import annotations

import csv
import fcntl
import json
import os
import pty
import re
import select
import shlex
import shutil
import subprocess
import termios
import time
from dataclasses import dataclass
from pathlib import Path
from string import Template
from typing import Any
from collections.abc import Sequence

import pandas as pd
import typer
from tqdm import tqdm

from libs.cli_logging import setup_logger
from libs.text_normalize import clean_doi

app = typer.Typer(add_completion=False)

PROMPT_VERSION = "v1"
PROMPT_TAILS = ("> ", ">")
IDLE_DONE_SECONDS = 0.5
DEFAULT_SAMPLING_TEMPERATURE = 0.1


def _strip_jsonc(text: str) -> str:
    out: list[str] = []
    in_str = False
    escape = False
    in_line_comment = False
    in_block_comment = False
    i = 0
    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""

        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
                out.append(ch)
            i += 1
            continue

        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                i += 2
            else:
                i += 1
            continue

        if in_str:
            out.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            i += 1
            continue

        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and nxt == "/":
            in_line_comment = True
            i += 2
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            i += 2
            continue

        out.append(ch)
        i += 1

    return _remove_trailing_commas("".join(out))


def _remove_trailing_commas(text: str) -> str:
    out: list[str] = []
    in_str = False
    escape = False
    for i, ch in enumerate(text):
        if in_str:
            out.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            continue
        if ch == ",":
            j = i + 1
            while j < len(text) and text[j] in " \t\r\n":
                j += 1
            if j < len(text) and text[j] in "]}":
                continue
        out.append(ch)
    return "".join(out)


def _load_config(path: Path | None) -> dict[str, Any]:
    if not path:
        return {}
    if not path.exists():
        raise FileNotFoundError(f"config not found: {path}")
    ext = path.suffix.lower()
    raw = path.read_text(encoding="utf-8")
    if ext in (".json", ".jsonc"):
        return json.loads(_strip_jsonc(raw))
    if ext in (".yaml", ".yml"):
        try:
            import yaml
        except Exception as exc:  # pragma: no cover
            raise RuntimeError("PyYAML is not installed; use .jsonc or install pyyaml") from exc
        data = yaml.safe_load(raw)
        return data or {}
    raise ValueError(f"unsupported config format: {ext}")


def _coalesce(val: Any, cfg: dict[str, Any], key: str, default: Any) -> Any:
    if val is not None:
        return val
    if key in cfg:
        return cfg[key]
    return default


def _normalize_hint(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        items = [str(v).strip() for v in value if str(v).strip()]
        return "; ".join(items) if items else None
    s = str(value).strip()
    return s or None


def _normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def _abstract_excerpt(
    value: str | None,
    max_chars: int = 1200,
    head_ratio: float = 0.7,
) -> str | None:
    if not value:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    if not text:
        return None
    if len(text) <= max_chars:
        return text
    head_len = max(1, int(max_chars * head_ratio))
    tail_len = max_chars - head_len
    if tail_len <= 0:
        return text[:max_chars]
    head = text[:head_len].rstrip()
    tail = text[-tail_len:].lstrip()
    if not tail:
        return head
    return f"{head} ... {tail}"


def _tokenize(value: str, min_len: int) -> list[str]:
    tokens = re.split(r"[^a-z0-9]+", (value or "").lower())
    return [t for t in tokens if len(t) >= min_len]


def _row_to_meta(row: pd.Series) -> dict[str, str]:
    meta: dict[str, str] = {}
    for col in row.index:
        val = row[col]
        if pd.isna(val):
            continue
        meta[str(col)] = str(val)
    return meta


def _record_id(meta: dict[str, str]) -> str:
    doi = clean_doi(meta.get("doi") or "")
    if doi:
        return f"doi:{doi}"
    title = (meta.get("title") or "").strip().lower()
    year = (meta.get("year") or "").strip()
    if title or year:
        return f"title:{title}|year:{year}"
    return json.dumps(meta, sort_keys=True)


def _resolve_llama_bin(llama_bin: str) -> Path:
    path = Path(llama_bin)
    if path.exists():
        return path
    resolved = shutil.which(llama_bin)
    if resolved:
        return Path(resolved)
    raise FileNotFoundError(f"llama.cpp binary not found: {llama_bin}")


def _build_prompt(
    scope: str,
    include_hint: str | None,
    exclude_hint: str | None,
    meta: dict[str, str],
    prompt_template: str | None,
) -> str:
    abstract_excerpt = _abstract_excerpt(meta.get("abstract"))
    if prompt_template:
        template = Template(str(prompt_template))
        return template.safe_substitute(
            scope=scope,
            include_hint=include_hint or "",
            exclude_hint=exclude_hint or "",
            title=meta.get("title", ""),
            journal=meta.get("journal", ""),
            year=meta.get("year", ""),
            authors=meta.get("authors", ""),
            doi=meta.get("doi", ""),
            abstract_excerpt=abstract_excerpt or "",
        ).strip()
    lines = [
        "You are a strict relevance screener for papers.",
        f"Project scope: {scope}",
    ]
    if include_hint:
        lines.append(f"In-scope hints: {include_hint}")
    if exclude_hint:
        lines.append(f"Out-of-scope hints: {exclude_hint}")
    lines += [
        "",
        "Instructions:",
        "- Use ONLY the provided metadata (title + abstract etc.). Do NOT assume missing info.",
        "- Output MUST be a single JSON object on ONE line. No markdown. No code fences.",
        "- Allowed keys: label, confidence, reason. Do not add any other keys.",
        "- label must be exactly one of: in_scope, out_of_scope, unsure",
        "- confidence must be a number from 0 to 1.",
        "- If evidence is insufficient, label = unsure.",
        "Decision rules (priority order):",
        "1) If it is host-associated microbiome-only (gut/skin/oral) and not environmental DNA/RNA monitoring, label = out_of_scope.",
        "2) Include only if eDNA/eRNA from environmental samples is used for ecology/monitoring/detection/surveillance.",
        "3) Tool development / CRISPR / pure genomics without environmental monitoring => out_of_scope.",
        "",
        "Paper metadata:",
        f"Title: {meta.get('title', '')}",
        f"Journal: {meta.get('journal', '')}",
        f"Year: {meta.get('year', '')}",
        f"Authors: {meta.get('authors', '')}",
        f"DOI: {meta.get('doi', '')}",
        "",
        "Abstract excerpt:",
        abstract_excerpt if abstract_excerpt else "NO ABSTRACT AVAILABLE",
    ]
    return "\n".join(lines).strip()


def _run_llama_cli(
    llama_bin: Path,
    model_path: Path,
    prompt: str,
    max_tokens: int,
    sampling_temperature: float,
    ctx_size: int | None,
    threads: int | None,
    extra_args: str | None,
    timeout: float,
) -> str:
    cmd = [
        str(llama_bin),
        "-m",
        str(model_path),
        "-p",
        prompt,
        "-n",
        str(max_tokens),
        "--temp",
        str(sampling_temperature),
    ]
    if ctx_size:
        cmd += ["-c", str(ctx_size)]
    if threads:
        cmd += ["-t", str(threads)]
    extra = shlex.split(extra_args) if extra_args else []
    if not _has_flag(extra, ["--color", "-co"]):
        extra += ["--color", "off"]
    if not _has_flag(extra, ["--no-show-timings", "--show-timings"]):
        extra.append("--no-show-timings")
    if not _has_flag(extra, ["--no-display-prompt", "--display-prompt"]):
        extra.append("--no-display-prompt")
    cmd += extra
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        err = (result.stderr or "").strip()
        raise RuntimeError(f"llama.cpp failed: {err}")
    return (result.stdout or "").strip()


def _run_llama_cli_safe(
    llama_bin: Path,
    model_path: Path,
    prompt: str,
    max_tokens: int,
    sampling_temperature: float,
    ctx_size: int | None,
    threads: int | None,
    extra_args: str | None,
    timeout: float,
) -> tuple[str, str | None]:
    try:
        return (
            _run_llama_cli(
                llama_bin,
                model_path,
                prompt,
                max_tokens,
                sampling_temperature,
                ctx_size,
                threads,
                extra_args,
                timeout,
            ),
            None,
        )
    except subprocess.TimeoutExpired:
        return "", f"llama-cli timed out after {timeout:.0f}s (prompt may be too long)"
    except Exception as exc:
        return "", f"llama-cli failed: {exc}"


def _parse_json(text: str) -> dict[str, Any] | None:
    cleaned = _sanitize_output(text)
    decoder = json.JSONDecoder()
    idx = 0
    found: dict[str, Any] | None = None
    while idx < len(cleaned):
        if cleaned[idx] != "{":
            next_idx = cleaned.find("{", idx + 1)
            if next_idx == -1:
                break
            idx = next_idx
            continue
        try:
            obj, end = decoder.raw_decode(cleaned, idx)
        except json.JSONDecodeError:
            idx += 1
            continue
        if isinstance(obj, dict) and "label" in obj:
            found = obj
        idx = end if end > idx else idx + 1
    return found


def _sanitize_output(text: str) -> str:
    # Remove ANSI escape sequences.
    cleaned = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    # Remove backspaces and their effects.
    out: list[str] = []
    for ch in cleaned:
        if ch == "\b":
            if out:
                out.pop()
            continue
        out.append(ch)
    cleaned = "".join(out)
    # Drop common special tokens like <|channel|>...<|message|>.
    cleaned = re.sub(r"<\|[^>]+?\|>", "", cleaned)
    # Strip non-printable control chars except newlines and tabs.
    cleaned = "".join(ch for ch in cleaned if ch == "\n" or ch == "\t" or ord(ch) >= 32)
    return cleaned


def _normalize_label(value: str) -> str | None:
    if not value:
        return None
    cleaned = re.sub(r"[^a-z]+", " ", value.lower()).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        return None
    if cleaned in ("in scope", "in_scope", "inscope", "include", "relevant"):
        return "in_scope"
    if cleaned in ("out of scope", "out_of_scope", "outofscope", "exclude", "irrelevant"):
        return "out_of_scope"
    if cleaned in ("unsure", "uncertain", "unknown", "maybe"):
        return "unsure"
    cleaned_key = cleaned.replace(" ", "_")
    if cleaned_key in ("in_scope", "out_of_scope", "unsure"):
        return cleaned_key
    return None


def _infer_label_from_text(text: str) -> str | None:
    if not text:
        return None
    lowered = text.lower()
    explicit = re.search(
        r"\b(in[_\- ]?scope|out[_\- ]?of[_\- ]?scope|unsure|uncertain)\b",
        text,
        re.IGNORECASE,
    )
    if explicit:
        return _normalize_label(explicit.group(1))

    if re.search(r"\b(no|not|without)\b.{0,12}\b(e?dna|erna)\b", lowered):
        return "out_of_scope"
    if re.search(r"\b(out of scope|outside scope|not in scope|not within scope)\b", lowered):
        return "out_of_scope"
    if re.search(r"\b(does not align|doesn't align|not aligned)\b", lowered) and "scope" in lowered:
        return "out_of_scope"
    if re.search(r"\b(unrelated|irrelevant)\b", lowered):
        return "out_of_scope"
    if re.search(r"\b(microbiome|metagenomics|shotgun)\b", lowered):
        if not re.search(r"\b(exclude|excluding|not)\b.{0,20}\b(microbiome|metagenomics|shotgun)\b", lowered):
            return "out_of_scope"

    if re.search(r"\b(edna|erna|environmental dna|environmental rna)\b", lowered):
        return "in_scope"
    if re.search(r"\b(in scope|within scope|fits scope)\b", lowered):
        return "in_scope"
    if re.search(r"\balign\w*\s+with\s+(the\s+)?scope\b", lowered):
        return "in_scope"
    if re.search(r"\balign\w*\s+with\b", lowered):
        return "in_scope"
    if re.search(r"\b(monitoring|sampling|metabarcoding|biodiversity)\b", lowered):
        return "in_scope"

    if re.search(r"\b(unclear|insufficient|ambiguous|unsure|uncertain)\b", lowered):
        return "unsure"
    if "relevant" in lowered and "not relevant" not in lowered:
        return "in_scope"
    if "not relevant" in lowered or "irrelevant" in lowered:
        return "out_of_scope"
    return None


def _normalize_confidence(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value).strip()
    if not text:
        return ""
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)", text)
    if not match:
        return ""
    num = float(match.group(1))
    if "%" in text and 0 <= num <= 100:
        num = num / 100.0
    return str(num)


def _parse_fallback(text: str) -> dict[str, Any] | None:
    cleaned = _sanitize_output(text)
    label = ""
    confidence: Any = ""
    reason = ""

    label_match = re.search(
        r"(?:^|\b)(label|decision|classification)\s*[:=]\s*([^\r\n]+)",
        cleaned,
        re.IGNORECASE | re.MULTILINE,
    )
    if label_match:
        label = label_match.group(2).strip()
    else:
        keyword_match = re.search(
            r"\b(in[_\- ]?scope|out[_\- ]?of[_\- ]?scope|unsure|uncertain)\b",
            cleaned,
            re.IGNORECASE,
        )
        if keyword_match:
            label = keyword_match.group(1).strip()

    conf_match = re.search(
        r"(?:^|\b)(confidence|score|probability)\s*[:=]\s*([0-9]+(?:\.[0-9]+)?%?)",
        cleaned,
        re.IGNORECASE | re.MULTILINE,
    )
    if conf_match:
        confidence = conf_match.group(2).strip()

    reason_match = re.search(
        r"(?:^|\b)(reason|rationale|explanation)\s*[:=]\s*(.+)$",
        cleaned,
        re.IGNORECASE | re.MULTILINE,
    )
    if reason_match:
        reason = reason_match.group(2).strip()

    if not label:
        return None
    return {"label": label, "confidence": confidence, "reason": reason}


def _truncate(text: str, limit: int = 400) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "...(truncated)"


def _flatten_cell(value: Any) -> Any:
    if value is None:
        return value
    if isinstance(value, str):
        return re.sub(r"\s+", " ", value).strip()
    return value


def _strip_prompt_echo(text: str, prompt: str) -> str:
    if not text or not prompt:
        return text
    cleaned = text
    if prompt in cleaned:
        cleaned = cleaned.replace(prompt, "")
    escaped = _escape_prompt(prompt)
    if escaped in cleaned:
        cleaned = cleaned.replace(escaped, "")
    prompt_lines = {line.strip() for line in prompt.splitlines() if line.strip()}
    if prompt_lines:
        cleaned_lines = [line for line in cleaned.splitlines() if line.strip() and line.strip() not in prompt_lines]
        cleaned = "\n".join(cleaned_lines)
    return cleaned.strip()


def _expand_path(value: str | Path | None) -> Path | None:
    if value is None:
        return None
    expanded = os.path.expandvars(str(value))
    return Path(expanded).expanduser()


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    try:
        if pd.isna(value):
            return False
    except Exception:
        pass
    if isinstance(value, str):
        return bool(value.strip())
    return True


def _row_has_output(row: pd.Series, columns: Sequence[str]) -> bool:
    for col in columns:
        if col in row and _has_value(row[col]):
            return True
    return False


def _escape_prompt(prompt: str) -> str:
    # llama-cli processes escape sequences; keep prompt on one line.
    return prompt.replace("\\", "\\\\").replace("\n", "\\n")


def _has_flag(args: Sequence[str], flags: Sequence[str]) -> bool:
    for arg in args:
        base = arg.split("=", 1)[0]
        if base in flags:
            return True
    return False


def _append_if_missing(args: list[str], flag: str, value: str | None = None) -> None:
    if flag in args:
        return
    args.append(flag)
    if value is not None:
        args.append(value)


@dataclass
class GenerationResult:
    text: str
    error: str | None = None
    timed_out: bool = False


@dataclass
class LlamaReuseProcess:
    cmd: list[str]
    timeout: float
    master_fd: int
    proc: subprocess.Popen

    @classmethod
    def start(cls, cmd: list[str], timeout: float) -> "LlamaReuseProcess":
        master_fd, slave_fd = pty.openpty()
        attrs = termios.tcgetattr(slave_fd)
        attrs[3] = attrs[3] & ~termios.ECHO
        termios.tcsetattr(slave_fd, termios.TCSANOW, attrs)

        def _preexec() -> None:
            os.setsid()
            fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)

        proc = subprocess.Popen(
            cmd,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
            preexec_fn=_preexec,
        )
        os.close(slave_fd)

        runner = cls(cmd=cmd, timeout=timeout, master_fd=master_fd, proc=proc)
        runner._drain_to_prompt()
        return runner

    def close(self) -> None:
        try:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except Exception:
                    self.proc.kill()
        finally:
            try:
                os.close(self.master_fd)
            except Exception:
                pass

    def _drain_to_prompt(self) -> None:
        try:
            self._read_until_prompt(self.timeout)
        except TimeoutError:
            # Nudge the CLI to show a prompt if it hasn't yet.
            try:
                self._send_line("")
                self._read_until_prompt(min(self.timeout, 10))
            except TimeoutError:
                # Some modes suppress prompts; continue without blocking.
                return

    def _read_until_prompt(self, timeout: float) -> str:
        buf = ""
        start = time.time()
        while True:
            trimmed = buf.rstrip("\r\n")
            if trimmed:
                lines = trimmed.splitlines()
                if lines and lines[-1].strip() == ">":
                    return "\n".join(lines[:-1])
            if time.time() - start > timeout:
                raise TimeoutError("llama-cli timed out waiting for prompt")
            rlist, _, _ = select.select([self.master_fd], [], [], 0.1)
            if not rlist:
                continue
            data = os.read(self.master_fd, 4096)
            if not data:
                raise RuntimeError("llama-cli exited unexpectedly")
            buf += data.decode(errors="ignore")

    def _read_until_done(self, timeout: float) -> tuple[str, bool]:
        buf = ""
        start = time.time()
        last_data = time.time()
        seen_json = False
        while True:
            trimmed = buf.rstrip("\r\n")
            if trimmed:
                lines = trimmed.splitlines()
                if lines and lines[-1].strip() == ">":
                    return "\n".join(lines[:-1]), False
            if time.time() - start > timeout:
                return buf, True
            rlist, _, _ = select.select([self.master_fd], [], [], 0.1)
            if not rlist:
                if seen_json and (time.time() - last_data) > IDLE_DONE_SECONDS:
                    return buf, False
                continue
            data = os.read(self.master_fd, 4096)
            if not data:
                return buf, False
            buf += data.decode(errors="ignore")
            last_data = time.time()
            if not seen_json and _parse_json(buf):
                seen_json = True

    def _send_line(self, line: str) -> None:
        os.write(self.master_fd, (line + "\n").encode())

    def reset(self) -> None:
        self._send_line("/clear")
        self._drain_to_prompt()

    def generate(self, prompt: str) -> GenerationResult:
        escaped = _escape_prompt(prompt)
        self._send_line(escaped)
        out, timed_out = self._read_until_done(self.timeout)
        error = None
        if timed_out:
            error = f"llama-cli timed out after {self.timeout:.0f}s (prompt may be too long)"
        elif self.proc.poll() is not None and self.proc.returncode not in (0, None):
            error = f"llama-cli exited with code {self.proc.returncode}"
        return GenerationResult(text=out.strip(), error=error, timed_out=timed_out)


@app.command()
def flag(
    csv_path: Path = typer.Argument(..., exists=True, dir_okay=False),
    out_csv: Path | None = typer.Option(None, "--out-csv"),
    config: Path | None = typer.Option(None, "--config"),
    log_file: Path | None = typer.Option(None, "--log-file"),
    log_level: str | None = typer.Option(None, "--log-level"),
    model_path: Path | None = typer.Option(None, "--model"),
    llama_bin: str | None = typer.Option(None, "--llama-bin"),
    llama_args: str | None = typer.Option(None, "--llama-args"),
    ctx_size: int | None = typer.Option(None, "--ctx-size"),
    threads: int | None = typer.Option(None, "--threads"),
    scope: str | None = typer.Option(None, "--scope"),
    include_hint: str | None = typer.Option(None, "--include-hint"),
    exclude_hint: str | None = typer.Option(None, "--exclude-hint"),
    max_tokens: int | None = typer.Option(None, "--max-tokens"),
    sampling_temperature: float | None = typer.Option(None, "--sampling-temperature"),
    timeout: float | None = typer.Option(None, "--timeout"),
    reuse_process: bool | None = typer.Option(None, "--reuse-process/--no-reuse-process"),
    batch_size: int | None = typer.Option(None, "--batch-size"),
    batch_index: int | None = typer.Option(None, "--batch-index"),
    resume: bool | None = typer.Option(
        None,
        "--resume/--no-resume",
        help=(
            "Skip rows already flagged in the input CSV or present in the output CSV. Use --no-resume to reprocess everything."
        ),
    ),
    limit: int | None = typer.Option(None, "--limit"),
    dry_run: bool | None = typer.Option(None, "--dry-run"),
) -> None:
    """Flag out-of-scope papers from metadata with llama.cpp CLI."""
    cfg = _load_config(config)

    out_csv = _coalesce(out_csv, cfg, "out_csv", "results/flagged.csv")
    log_file = _coalesce(log_file, cfg, "log_file", None)
    log_level = _coalesce(log_level, cfg, "log_level", "INFO")
    if isinstance(log_level, str) and not log_level.strip():
        log_level = "INFO"
    prompt_template = _coalesce(None, cfg, "prompt_template", None)
    model_path = _coalesce(model_path, cfg, "model_path", None)
    llama_bin = _coalesce(llama_bin, cfg, "llama_bin", "llama-cli")
    llama_args = _coalesce(llama_args, cfg, "llama_args", None)
    ctx_size = _coalesce(ctx_size, cfg, "ctx_size", None)
    threads = _coalesce(threads, cfg, "threads", None)
    scope = _normalize_hint(_coalesce(scope, cfg, "scope", None))
    include_hint = _normalize_hint(_coalesce(include_hint, cfg, "include_hint", None))
    exclude_hint = _normalize_hint(_coalesce(exclude_hint, cfg, "exclude_hint", None))
    max_tokens = _coalesce(max_tokens, cfg, "max_tokens", 256)
    sampling_temperature = _coalesce(sampling_temperature, cfg, "sampling_temperature", None)
    timeout = _coalesce(timeout, cfg, "timeout", 300.0)
    reuse_process = _coalesce(reuse_process, cfg, "reuse_process", False)
    batch_size = _coalesce(batch_size, cfg, "batch_size", None)
    batch_index = _coalesce(batch_index, cfg, "batch_index", None)
    resume = _coalesce(resume, cfg, "resume", True)
    limit = _coalesce(limit, cfg, "limit", None)
    dry_run = _coalesce(dry_run, cfg, "dry_run", False)

    if not scope:
        raise typer.BadParameter("scope is required (use --scope or config)")
    if not model_path:
        raise typer.BadParameter("model_path is required (use --model or config)")

    model_path = _expand_path(model_path)
    if not model_path:
        raise typer.BadParameter("model_path is required (use --model or config)")
    if not model_path.exists():
        raise typer.BadParameter(f"model not found: {model_path}")

    out_csv = _expand_path(out_csv) or Path("results/flagged.csv")
    llama_bin = str(_expand_path(llama_bin) or llama_bin)
    llama_path = _resolve_llama_bin(llama_bin)
    log_file_path = _expand_path(log_file) if log_file else None
    logger = setup_logger("llama_flagger", str(log_level), log_file_path, stream="stderr")
    logger.info("Starting flagger: csv=%s out=%s model=%s", csv_path, out_csv, model_path)
    logger.info(
        "Options: ctx=%s threads=%s temp=%s max_tokens=%s reuse=%s",
        ctx_size,
        threads,
        sampling_temperature,
        max_tokens,
        reuse_process,
    )

    llama_args_list = shlex.split(llama_args) if llama_args else []
    llama_args = shlex.join(llama_args_list) if llama_args_list else None
    if reuse_process and (_has_flag(llama_args_list, ["--single-turn", "-st"])):
        raise typer.BadParameter("--single-turn cannot be used with --reuse-process")
    if sampling_temperature is None:
        sampling_temperature = float(DEFAULT_SAMPLING_TEMPERATURE)
    logger.info("Resolved: ctx=%s temp=%s", ctx_size, sampling_temperature)

    df = pd.read_csv(csv_path)
    df = df.drop(columns=["flag_file_path", "flag_file_match", "flag_content_source"], errors="ignore")
    logger.info("Loaded rows: %d", len(df))
    if limit and batch_size:
        raise typer.BadParameter("--limit cannot be used with --batch-size")
    if batch_size:
        if batch_size <= 0:
            raise typer.BadParameter("--batch-size must be > 0")
        if batch_index is None:
            batch_index = 0
        if batch_index < 0:
            raise typer.BadParameter("--batch-index must be >= 0")
        start = batch_index * batch_size
        end = start + batch_size
        df = df.iloc[start:end]
    if limit:
        df = df.head(limit)
        logger.info("Applied limit: %d", len(df))

    new_columns = [
        "flag_record_id",
        "flag_label",
        "flag_confidence",
        "flag_reason",
        "flag_model_path",
        "flag_prompt_version",
    ]
    output_check_columns = [c for c in new_columns if c != "flag_record_id"]
    fieldnames = list(df.columns) + [c for c in new_columns if c not in df.columns]
    write_header = not out_csv.exists()
    processed: set[str] = set()
    if out_csv.exists() and resume:
        existing = pd.read_csv(out_csv)
        if "flag_record_id" in existing.columns:
            for _, row in existing.iterrows():
                record_id = row.get("flag_record_id")
                if not _has_value(record_id):
                    continue
                if not _row_has_output(row, output_check_columns):
                    continue
                processed.add(str(record_id))

    out_csv.parent.mkdir(parents=True, exist_ok=True)
    if out_csv.exists() and not resume:
        out_csv.unlink()

    runner: LlamaReuseProcess | None = None
    if reuse_process and not dry_run:
        cmd = [
            str(llama_path),
            "-m",
            str(model_path),
            "-n",
            str(max_tokens),
            "--temp",
            str(sampling_temperature),
        ]
        if ctx_size:
            cmd += ["-c", str(ctx_size)]
        if threads:
            cmd += ["-t", str(threads)]
        extra = list(llama_args_list)
        if not _has_flag(extra, ["--color", "-co"]):
            cmd += ["--color", "off"]
        if not _has_flag(extra, ["--no-show-timings", "--show-timings"]):
            cmd.append("--no-show-timings")
        if not _has_flag(extra, ["--no-display-prompt", "--display-prompt"]):
            cmd.append("--no-display-prompt")
        cmd += extra
        runner = LlamaReuseProcess.start(cmd, timeout=timeout)

    try:
        with out_csv.open("a", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if write_header:
                writer.writeheader()

            for _, row in tqdm(df.iterrows(), total=len(df), desc="flag"):
                if resume and _row_has_output(row, output_check_columns):
                    continue
                meta = _row_to_meta(row)
                record_id = _record_id(meta)
                if resume and record_id in processed:
                    continue
                prompt = _build_prompt(scope, include_hint, exclude_hint, meta, prompt_template)
                logger.debug("record=%s prompt_chars=%d", record_id, len(prompt))

                if dry_run:
                    typer.echo(prompt)
                    return

                raw = ""
                error: str | None = None
                if runner:
                    runner.reset()
                    result = runner.generate(prompt)
                    raw = result.text
                    error = result.error
                else:
                    raw, error = _run_llama_cli_safe(
                        llama_path,
                        model_path,
                        prompt,
                        max_tokens,
                        sampling_temperature,
                        ctx_size,
                        threads,
                        llama_args,
                        timeout,
                    )

                cleaned = _sanitize_output(raw)
                cleaned_no_prompt = _strip_prompt_echo(cleaned, prompt)
                parsed = _parse_json(cleaned)
                parse_source = "json"
                if not parsed:
                    parsed = _parse_json(cleaned_no_prompt)
                    parse_source = "json_no_prompt"
                if not parsed:
                    parsed = _parse_fallback(cleaned_no_prompt)
                    parse_source = "fallback" if parsed else "none"
                label = "parse_error"
                confidence = ""
                reason = ""
                if error:
                    label = "process_error"
                    confidence = ""
                    reason = error
                elif parsed:
                    label_raw = str(parsed.get("label", "")).strip()
                    label_norm = _normalize_label(label_raw)
                    template_label = "|" in label_raw
                    label = label_norm or "parse_error"
                    confidence = _normalize_confidence(parsed.get("confidence", ""))
                    reason = str(parsed.get("reason", "")).strip() or ""
                    if label == "parse_error":
                        label_hint = _infer_label_from_text(reason)
                        if not label_hint and not template_label:
                            label_hint = _infer_label_from_text(cleaned_no_prompt)
                        if label_hint:
                            label = label_hint
                            parse_source = f"{parse_source}_label_infer"
                        else:
                            reason = f"parse_error: invalid label {label_raw!r}"
                if not reason:
                    if label == "parse_error":
                        reason = "parse_error: could not parse output"
                    else:
                        reason = "no reason provided"

                if label == "parse_error":
                    logger.warning(
                        "parse_error record=%s source=%s output=%s",
                        record_id,
                        parse_source,
                        _truncate(cleaned_no_prompt),
                    )
                elif label == "process_error":
                    logger.warning("process_error record=%s error=%s", record_id, reason)
                else:
                    logger.debug(
                        "parsed record=%s label=%s confidence=%s source=%s",
                        record_id,
                        label,
                        confidence,
                        parse_source,
                    )

                out_row = meta.copy()
                out_row.update(
                    {
                        "flag_record_id": record_id,
                        "flag_label": label,
                        "flag_confidence": confidence,
                        "flag_reason": reason,
                        "flag_model_path": model_path.name,
                        "flag_prompt_version": PROMPT_VERSION,
                    }
                )
                out_row = {key: _flatten_cell(value) for key, value in out_row.items()}
                writer.writerow(out_row)
                f.flush()
    finally:
        if runner:
            runner.close()

    typer.echo(f"Wrote flagged CSV: {out_csv}")


if __name__ == "__main__":
    app()
