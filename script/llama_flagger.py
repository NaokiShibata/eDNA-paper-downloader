from __future__ import annotations

import csv
import json
import os
import pty
import re
import fcntl
import select
import shlex
import shutil
import subprocess
import termios
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd
import typer
from tqdm import tqdm

app = typer.Typer(add_completion=False)

PROMPT_VERSION = "v1"
DEFAULT_FILE_EXTS = ".pdf,.txt"
PROMPT_TAILS = ("> ", ">")
IDLE_DONE_SECONDS = 0.5


@dataclass
class FileEntry:
    path: Path
    name_norm: str
    tokens: set[str]


def _strip_jsonc(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r"//.*?$", "", text, flags=re.MULTILINE)
    return text


def _load_config(path: Optional[Path]) -> Dict[str, Any]:
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


def _coalesce(val: Any, cfg: Dict[str, Any], key: str, default: Any) -> Any:
    if val is not None:
        return val
    if key in cfg:
        return cfg[key]
    return default


def _normalize_hint(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        items = [str(v).strip() for v in value if str(v).strip()]
        return "; ".join(items) if items else None
    s = str(value).strip()
    return s or None


def _normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def _clean_doi(value: str) -> str:
    v = (value or "").strip().lower()
    v = re.sub(r"^https?://(dx\.)?doi\.org/", "", v)
    v = re.sub(r"^doi:\s*", "", v)
    return v.strip()


def _tokenize(value: str, min_len: int) -> List[str]:
    tokens = re.split(r"[^a-z0-9]+", (value or "").lower())
    return [t for t in tokens if len(t) >= min_len]


def _row_to_meta(row: pd.Series) -> Dict[str, str]:
    meta: Dict[str, str] = {}
    for col in row.index:
        val = row[col]
        if pd.isna(val):
            continue
        meta[str(col)] = str(val)
    return meta


def _record_id(meta: Dict[str, str]) -> str:
    doi = _clean_doi(meta.get("doi") or "")
    if doi:
        return f"doi:{doi}"
    title = (meta.get("title") or "").strip().lower()
    year = (meta.get("year") or "").strip()
    if title or year:
        return f"title:{title}|year:{year}"
    return json.dumps(meta, sort_keys=True)


def _collect_files(files_dir: Path, file_exts: Sequence[str], min_token_len: int) -> List[FileEntry]:
    entries: List[FileEntry] = []
    for path in files_dir.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in file_exts:
            continue
        name_norm = _normalize_key(path.stem)
        tokens = set(_tokenize(path.stem, min_token_len))
        entries.append(FileEntry(path=path, name_norm=name_norm, tokens=tokens))
    return entries


def _match_file(
    meta: Dict[str, str],
    entries: Sequence[FileEntry],
    file_path_col: Optional[str],
    min_token_len: int,
    min_token_matches: int,
    files_dir: Optional[Path],
) -> Tuple[Optional[Path], str]:
    if file_path_col:
        raw = meta.get(file_path_col) or ""
        if raw:
            path = Path(raw)
            if files_dir and not path.is_absolute():
                path = files_dir / path
            if path.exists():
                return path, "file_path_col"

    doi = _clean_doi(meta.get("doi") or "")
    doi_key = _normalize_key(doi)
    if doi_key:
        for entry in entries:
            if doi_key in entry.name_norm:
                return entry.path, "doi_in_filename"

    title = meta.get("title") or ""
    title_tokens = _tokenize(title, min_token_len)
    if title_tokens:
        best_score = 0
        best_path: Optional[Path] = None
        for entry in entries:
            score = sum(1 for t in title_tokens if t in entry.tokens)
            if score > best_score:
                best_score = score
                best_path = entry.path
        if best_path and best_score >= min_token_matches:
            return best_path, f"title_tokens:{best_score}"

    return None, "none"


def _resolve_llama_bin(llama_bin: str) -> Path:
    path = Path(llama_bin)
    if path.exists():
        return path
    resolved = shutil.which(llama_bin)
    if resolved:
        return Path(resolved)
    raise FileNotFoundError(f"llama.cpp binary not found: {llama_bin}")


def _extract_text_from_pdf(path: Path, pdftotext_path: Optional[str], max_chars: int) -> Tuple[str, str]:
    if not pdftotext_path:
        return "", "pdf_no_tool"
    cmd = [pdftotext_path, "-layout", "-enc", "UTF-8", str(path), "-"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    except Exception:
        return "", "pdf_error"
    text = (result.stdout or "").strip()
    if not text:
        return "", "pdf_empty"
    return text[:max_chars], "pdf"


def _extract_text(path: Optional[Path], pdftotext_path: Optional[str], max_chars: int) -> Tuple[str, str]:
    if not path:
        return "", "missing"
    ext = path.suffix.lower()
    if ext in (".txt", ".md"):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return "", "read_error"
        return text.strip()[:max_chars], "text"
    if ext == ".pdf":
        return _extract_text_from_pdf(path, pdftotext_path, max_chars)
    return "", "unsupported"


def _build_prompt(
    scope: str,
    include_hint: Optional[str],
    exclude_hint: Optional[str],
    meta: Dict[str, str],
    content: str,
) -> str:
    lines = [
        "You are a strict relevance screener for downloaded papers.",
        f"Project scope: {scope}",
    ]
    if include_hint:
        lines.append(f"In-scope hints: {include_hint}")
    if exclude_hint:
        lines.append(f"Out-of-scope hints: {exclude_hint}")
    lines += [
        "",
        "Instructions:",
        "- Use both metadata and file excerpt when available.",
        "- If evidence is insufficient, answer with label \"unsure\".",
        "- Reply with JSON only.",
        "Format: {\"label\":\"in_scope|out_of_scope|unsure\",\"confidence\":0-1,\"reason\":\"short\"}",
        "",
        "Paper metadata:",
        f"Title: {meta.get('title','')}",
        f"Journal: {meta.get('journal','')}",
        f"Year: {meta.get('year','')}",
        f"Authors: {meta.get('authors','')}",
        f"DOI: {meta.get('doi','')}",
        "",
        "File excerpt:",
        content if content else "NO FILE CONTENT AVAILABLE",
    ]
    return "\n".join(lines).strip()


def _run_llama_cli(
    llama_bin: Path,
    model_path: Path,
    prompt: str,
    max_tokens: int,
    sampling_temperature: float,
    ctx_size: Optional[int],
    threads: Optional[int],
    extra_args: Optional[str],
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
    if extra_args:
        cmd += shlex.split(extra_args)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        err = (result.stderr or "").strip()
        raise RuntimeError(f"llama.cpp failed: {err}")
    return (result.stdout or "").strip()


def _parse_json(text: str) -> Optional[Dict[str, Any]]:
    matches = list(re.finditer(r"\{.*?\}", text, flags=re.DOTALL))
    for match in reversed(matches):
        snippet = match.group(0)
        try:
            return json.loads(snippet)
        except Exception:
            continue
    return None


def _escape_prompt(prompt: str) -> str:
    # llama-cli processes escape sequences; keep prompt on one line.
    return prompt.replace("\\", "\\\\").replace("\n", "\\n")


def _has_flag(args: Sequence[str], flags: Sequence[str]) -> bool:
    return any(a in flags for a in args)


def _append_if_missing(args: List[str], flag: str, value: Optional[str] = None) -> None:
    if flag in args:
        return
    args.append(flag)
    if value is not None:
        args.append(value)


@dataclass
class LlamaReuseProcess:
    cmd: List[str]
    timeout: float
    master_fd: int
    proc: subprocess.Popen

    @classmethod
    def start(cls, cmd: List[str], timeout: float) -> "LlamaReuseProcess":
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

    def _read_until_done(self, timeout: float) -> str:
        buf = ""
        start = time.time()
        last_data = time.time()
        seen_json = False
        while True:
            trimmed = buf.rstrip("\r\n")
            if trimmed:
                lines = trimmed.splitlines()
                if lines and lines[-1].strip() == ">":
                    return "\n".join(lines[:-1])
            if time.time() - start > timeout:
                return buf
            rlist, _, _ = select.select([self.master_fd], [], [], 0.1)
            if not rlist:
                if seen_json and (time.time() - last_data) > IDLE_DONE_SECONDS:
                    return buf
                continue
            data = os.read(self.master_fd, 4096)
            if not data:
                return buf
            buf += data.decode(errors="ignore")
            last_data = time.time()
            if not seen_json and _parse_json(buf):
                seen_json = True

    def _send_line(self, line: str) -> None:
        os.write(self.master_fd, (line + "\n").encode())

    def reset(self) -> None:
        self._send_line("/clear")
        self._drain_to_prompt()

    def generate(self, prompt: str) -> str:
        escaped = _escape_prompt(prompt)
        self._send_line(escaped)
        out = self._read_until_done(self.timeout)
        return out.strip()


@app.command()
def flag(
    csv_path: Path = typer.Argument(..., exists=True, dir_okay=False),
    out_csv: Path = typer.Option(Path("results/flagged.csv"), "--out-csv"),
    files_dir: Optional[Path] = typer.Option(None, "--files-dir"),
    config: Optional[Path] = typer.Option(None, "--config"),
    model_path: Optional[Path] = typer.Option(None, "--model"),
    llama_bin: Optional[str] = typer.Option(None, "--llama-bin"),
    llama_args: Optional[str] = typer.Option(None, "--llama-args"),
    ctx_size: Optional[int] = typer.Option(None, "--ctx-size"),
    threads: Optional[int] = typer.Option(None, "--threads"),
    scope: Optional[str] = typer.Option(None, "--scope"),
    include_hint: Optional[str] = typer.Option(None, "--include-hint"),
    exclude_hint: Optional[str] = typer.Option(None, "--exclude-hint"),
    file_exts: Optional[str] = typer.Option(None, "--file-exts"),
    file_path_col: Optional[str] = typer.Option("file_path", "--file-path-col"),
    min_token_len: Optional[int] = typer.Option(None, "--min-token-len"),
    min_token_matches: Optional[int] = typer.Option(None, "--min-token-matches"),
    max_chars: Optional[int] = typer.Option(None, "--max-chars"),
    max_tokens: Optional[int] = typer.Option(None, "--max-tokens"),
    sampling_temperature: Optional[float] = typer.Option(None, "--sampling-temperature"),
    timeout: Optional[float] = typer.Option(None, "--timeout"),
    pdftotext: Optional[str] = typer.Option(None, "--pdftotext"),
    reuse_process: Optional[bool] = typer.Option(None, "--reuse-process/--no-reuse-process"),
    batch_size: Optional[int] = typer.Option(None, "--batch-size"),
    batch_index: Optional[int] = typer.Option(None, "--batch-index"),
    resume: bool = typer.Option(True, "--resume/--no-resume"),
    limit: Optional[int] = typer.Option(None, "--limit"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Flag out-of-scope papers by inspecting downloaded files with llama.cpp CLI."""
    cfg = _load_config(config)

    files_dir = _coalesce(files_dir, cfg, "files_dir", None)
    model_path = _coalesce(model_path, cfg, "model_path", None)
    llama_bin = _coalesce(llama_bin, cfg, "llama_bin", "llama-cli")
    llama_args = _coalesce(llama_args, cfg, "llama_args", None)
    ctx_size = _coalesce(ctx_size, cfg, "ctx_size", None)
    threads = _coalesce(threads, cfg, "threads", None)
    scope = _normalize_hint(_coalesce(scope, cfg, "scope", None))
    include_hint = _normalize_hint(_coalesce(include_hint, cfg, "include_hint", None))
    exclude_hint = _normalize_hint(_coalesce(exclude_hint, cfg, "exclude_hint", None))
    file_exts = _coalesce(file_exts, cfg, "file_exts", DEFAULT_FILE_EXTS)
    file_path_col = _coalesce(file_path_col, cfg, "file_path_col", "file_path")
    if isinstance(file_path_col, str) and not file_path_col.strip():
        file_path_col = None
    min_token_len = _coalesce(min_token_len, cfg, "min_token_len", 4)
    min_token_matches = _coalesce(min_token_matches, cfg, "min_token_matches", 2)
    max_chars = _coalesce(max_chars, cfg, "max_chars", 6000)
    max_tokens = _coalesce(max_tokens, cfg, "max_tokens", 256)
    sampling_temperature = _coalesce(sampling_temperature, cfg, "sampling_temperature", 0.1)
    timeout = _coalesce(timeout, cfg, "timeout", 300.0)
    pdftotext = _coalesce(pdftotext, cfg, "pdftotext", None)
    reuse_process = _coalesce(reuse_process, cfg, "reuse_process", False)
    batch_size = _coalesce(batch_size, cfg, "batch_size", None)
    batch_index = _coalesce(batch_index, cfg, "batch_index", None)

    if not scope:
        raise typer.BadParameter("scope is required (use --scope or config)")
    if not model_path:
        raise typer.BadParameter("model_path is required (use --model or config)")

    model_path = Path(model_path)
    if not model_path.exists():
        raise typer.BadParameter(f"model not found: {model_path}")

    llama_path = _resolve_llama_bin(str(llama_bin))
    files_dir_path = Path(files_dir) if files_dir else None

    if isinstance(file_exts, (list, tuple)):
        file_ext_list = [str(e).strip().lower() for e in file_exts if str(e).strip()]
    else:
        file_ext_list = [e.strip().lower() for e in str(file_exts).split(",") if e.strip()]
    file_ext_list = [e if e.startswith(".") else f".{e}" for e in file_ext_list]
    pdftotext_path = pdftotext or shutil.which("pdftotext")
    if not pdftotext_path and ".pdf" in file_ext_list:
        typer.echo("pdftotext not found; PDF files will be skipped.", err=True)

    llama_args_list = shlex.split(llama_args) if llama_args else []
    if reuse_process and (_has_flag(llama_args_list, ["--single-turn", "-st"])):
        raise typer.BadParameter("--single-turn cannot be used with --reuse-process")

    entries: List[FileEntry] = []
    if files_dir_path:
        if not files_dir_path.exists():
            raise typer.BadParameter(f"files_dir not found: {files_dir_path}")
        entries = _collect_files(files_dir_path, file_ext_list, min_token_len)

    df = pd.read_csv(csv_path)
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

    processed: set[str] = set()
    if out_csv.exists() and resume:
        existing = pd.read_csv(out_csv)
        if "flag_record_id" in existing.columns:
            processed = set(existing["flag_record_id"].astype(str).tolist())

    out_csv.parent.mkdir(parents=True, exist_ok=True)
    if out_csv.exists() and not resume:
        out_csv.unlink()

    new_columns = [
        "flag_record_id",
        "flag_label",
        "flag_confidence",
        "flag_reason",
        "flag_file_path",
        "flag_file_match",
        "flag_content_source",
        "flag_model_path",
        "flag_prompt_version",
    ]
    fieldnames = list(df.columns) + [c for c in new_columns if c not in df.columns]
    write_header = not out_csv.exists()

    runner: Optional[LlamaReuseProcess] = None
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
                meta = _row_to_meta(row)
                record_id = _record_id(meta)
                if resume and record_id in processed:
                    continue

                file_path, match_hint = _match_file(
                    meta, entries, file_path_col, min_token_len, min_token_matches, files_dir_path
                )
                content, content_source = _extract_text(file_path, pdftotext_path, max_chars)
                prompt = _build_prompt(scope, include_hint, exclude_hint, meta, content)

                if dry_run:
                    typer.echo(prompt)
                    return

                if runner:
                    runner.reset()
                    raw = runner.generate(prompt)
                else:
                    raw = _run_llama_cli(
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

                parsed = _parse_json(raw)
                label = "parse_error"
                confidence = ""
                reason = raw.strip()
                if parsed:
                    label = str(parsed.get("label", "")).strip().lower() or "parse_error"
                    if label not in ("in_scope", "out_of_scope", "unsure"):
                        label = "parse_error"
                    confidence = parsed.get("confidence", "")
                    reason = str(parsed.get("reason", "")).strip() or reason

                out_row = meta.copy()
                out_row.update(
                    {
                        "flag_record_id": record_id,
                        "flag_label": label,
                        "flag_confidence": confidence,
                        "flag_reason": reason,
                        "flag_file_path": str(file_path) if file_path else "",
                        "flag_file_match": match_hint,
                        "flag_content_source": content_source,
                        "flag_model_path": str(model_path),
                        "flag_prompt_version": PROMPT_VERSION,
                    }
                )
                writer.writerow(out_row)
                f.flush()
    finally:
        if runner:
            runner.close()

    typer.echo(f"Wrote flagged CSV: {out_csv}")


if __name__ == "__main__":
    app()
