from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pandas as pd
import requests
import typer
from tqdm import tqdm

from libs.cli_logging import setup_logger
from libs.text_normalize import clean_doi

app = typer.Typer(add_completion=False)

PROMPT_VERSION = "strands-v2"
DEFAULT_BASE_URL = "http://127.0.0.1:8012"

QUESTIONS: dict[str, dict[str, Any]] = {
    "scope": {
        "type": "choice",
        "instructions": (
            "Classify whether this scientific abstract should be retained as environmental DNA (eDNA) "
            "or environmental RNA (eRNA) research. Judge what the study actually does, not whether the "
            "terms eDNA, eRNA, environmental DNA, or environmental RNA literally appear."
        ),
        "criteria": {
            "in_scope": (
                "The study actually collects, detects, quantifies, sequences, analyzes, validates, compares, "
                "or models DNA or RNA obtained directly from an environmental sample or environmental matrix. "
                "Examples include water, seawater, freshwater, sediment, soil, air, snow, ice, wastewater, "
                "biofilms, passive samplers, environmental swabs, dust, or similar material. Include studies "
                "using these nucleic acids to detect or characterize organisms, taxa, populations, communities, "
                "biodiversity, biological signals, pathogens, or ecological patterns. The eDNA/eRNA terminology "
                "does not need to be explicit."
            ),
            "out_of_scope": (
                "The study does not actually analyze environmentally obtained DNA or RNA. This includes studies "
                "based only on tissue, blood, isolated organisms, cultured strains, museum specimens, individual "
                "genomes, ordinary transcriptomics, or studies that mention eDNA/eRNA only in the background, "
                "discussion, comparison, citation, or future work."
            ),
            "unsure": (
                "The abstract does not provide enough information to determine whether environmentally obtained "
                "DNA or RNA was actually collected or analyzed. Prefer unsure over guessing when evidence is "
                "ambiguous."
            ),
        },
    },
    "actual_use": {
        "type": "noul",
        "instructions": (
            "Does the study actually collect or analyze DNA or RNA obtained directly from an environmental "
            "sample or environmental matrix as part of its methods or results?"
        ),
        "criteria": {
            "true": (
                "Environmental material is sampled and nucleic acids from that material are detected, quantified, "
                "sequenced, analyzed, compared, validated, or modeled."
            ),
            "false": (
                "No environmental nucleic-acid analysis is actually performed, or it is only mentioned as "
                "background, comparison, or future work."
            ),
        },
    },
    "microbial_only": {
        "type": "noul",
        "instructions": (
            "Is this primarily a conventional microbiome, microbial-community, metagenomic, or metatranscriptomic "
            "study in which environmental DNA/RNA is simply source material, without a specific eDNA/eRNA "
            "detection, monitoring, sampling, quantification, validation, or ecological-inference focus?"
        ),
        "criteria": {
            "true": (
                "The main goal is general microbial community profiling, microbiome composition, shotgun "
                "metagenomics, MAG reconstruction, resistome/virome profiling, or similar work, without a clear "
                "eDNA/eRNA-oriented detection, monitoring, or methodological contribution."
            ),
            "false": (
                "The study is not merely generic microbial profiling, or it has a meaningful eDNA/eRNA detection, "
                "monitoring, sampling, quantification, validation, or methodological component. Do not mark true "
                "solely because 16S, 18S, ITS, rbcL, COI, metabarcoding, or metagenomics terms appear."
            ),
        },
    },
    "method_relevance": {
        "type": "noul",
        "instructions": (
            "Even if the study focuses on microorganisms or an adjacent field, does it evaluate a sampling, "
            "preservation, extraction, detection, amplification, sequencing, quantification, bioinformatic, "
            "modeling, or monitoring approach that could be directly useful for eDNA/eRNA research?"
        ),
        "criteria": {
            "true": (
                "The methodological findings are directly transferable or informative for environmental DNA/RNA "
                "sampling, preservation, detection, quantification, sequencing, analysis, modeling, or monitoring."
            ),
            "false": "There is no clear methodological relevance to eDNA/eRNA work.",
        },
    },
}

EXTRA_COLUMNS = [
    "flag_record_id",
    "flag_label",
    "flag_confidence",
    "flag_reason",
    "flag_model_path",
    "flag_prompt_version",
    "strands_scope_choice",
    "strands_p_in_scope",
    "strands_p_out_of_scope",
    "strands_p_unsure",
    "strands_p_actual_use",
    "strands_p_microbial_only",
    "strands_p_method_relevance",
    "strands_latency_ms",
    "strands_input_tokens",
]


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
    raw = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".json", ".jsonc"):
        return json.loads(_strip_jsonc(raw))
    raise ValueError("strands_flagger config must be JSON or JSONC")


def _coalesce(value: Any, config: dict[str, Any], key: str, default: Any) -> Any:
    if value is not None:
        return value
    if key in config:
        return config[key]
    return default


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


def _prepare_abstract(value: str, max_chars: int | None) -> str:
    text = " ".join(str(value).split())
    if max_chars is None or max_chars <= 0 or len(text) <= max_chars:
        return text
    head_len = max(1, int(max_chars * 0.75))
    tail_len = max_chars - head_len
    return f"{text[:head_len].rstrip()} ... {text[-tail_len:].lstrip()}"


def _existing_processed_ids(out_csv: Path) -> set[str]:
    if not out_csv.exists() or out_csv.stat().st_size == 0:
        return set()
    try:
        existing = pd.read_csv(out_csv, dtype=str, keep_default_na=False)
    except Exception:
        return set()
    if "flag_record_id" not in existing.columns or "flag_label" not in existing.columns:
        return set()
    labels = existing["flag_label"].str.strip()
    mask = (
        existing["flag_record_id"].str.strip().ne("")
        & labels.ne("")
        & labels.ne("process_error")
    )
    return set(existing.loc[mask, "flag_record_id"].tolist())


def _post_with_retry(
    session: requests.Session,
    url: str,
    payload: dict[str, Any],
    timeout: float,
    retries: int,
) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(max(1, retries)):
        try:
            response = session.post(url, json=payload, timeout=timeout)
            if not response.ok:
                body = response.text.strip()
                if len(body) > 1000:
                    body = body[:1000] + "..."
                raise RuntimeError(
                    f"HTTP {response.status_code} from {url}: {body or response.reason}"
                )
            data = response.json()
            if not isinstance(data, dict):
                raise RuntimeError("Strands Decider returned a non-object response")
            return data
        except Exception as exc:
            last_error = exc
            if attempt + 1 < max(1, retries):
                time.sleep(min(8.0, 2.0**attempt))
    raise RuntimeError(f"Strands Decider request failed: {last_error}")

def _evaluate_questions_sequentially(
    session: requests.Session,
    url: str,
    state: str,
    questions: dict[str, dict[str, Any]],
    timeout: float,
    retries: int,
) -> dict[str, Any]:
    """Evaluate one question per HTTP request to minimize peak CUDA memory.

    This is intentionally compatible with older Strands Decider releases that do not
    expose the newer server-side --max-batch option. It also avoids the multi-question
    shared-prefix/batched path entirely.
    """
    merged_answers: dict[str, Any] = {}
    model = ""
    input_tokens = 0
    output_tokens = 0
    latency_ms = 0.0

    for name, question in questions.items():
        data = _post_with_retry(
            session=session,
            url=url,
            payload={"state": state, "questions": {name: question}},
            timeout=timeout,
            retries=retries,
        )
        answers = data.get("answers")
        if not isinstance(answers, dict) or name not in answers:
            raise RuntimeError(f"response for question {name!r} is missing its answer")
        merged_answers[name] = answers[name]
        if not model:
            model = str(data.get("model", "strands-decider"))
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        input_tokens += int(usage.get("input_tokens", 0) or 0)
        output_tokens += int(usage.get("output_tokens", 0) or 0)
        latency_ms += float(data.get("latency_ms", 0.0) or 0.0)

    return {
        "model": model or "strands-decider",
        "answers": merged_answers,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        },
        "latency_ms": round(latency_ms, 2),
    }



def _decide_label(
    p_in_scope: float,
    p_out_of_scope: float,
    p_actual_use: float,
    p_method_relevance: float,
    include_threshold: float,
    actual_use_threshold: float,
    exclude_threshold: float,
    exclude_actual_use_max: float,
    exclude_method_relevance_max: float,
) -> str:
    if p_in_scope >= include_threshold and p_actual_use >= actual_use_threshold:
        return "in_scope"
    if (
        p_out_of_scope >= exclude_threshold
        and p_actual_use <= exclude_actual_use_max
        and p_method_relevance <= exclude_method_relevance_max
    ):
        return "out_of_scope"
    return "unsure"


def _parse_response(
    data: dict[str, Any],
    include_threshold: float,
    actual_use_threshold: float,
    exclude_threshold: float,
    exclude_actual_use_max: float,
    exclude_method_relevance_max: float,
) -> dict[str, Any]:
    answers = data.get("answers")
    if not isinstance(answers, dict):
        raise RuntimeError("response is missing answers")

    scope = answers.get("scope")
    actual = answers.get("actual_use")
    microbial = answers.get("microbial_only")
    method = answers.get("method_relevance")
    if not all(isinstance(x, dict) for x in (scope, actual, microbial, method)):
        raise RuntimeError("response is missing one or more expected answers")

    probabilities = scope.get("probabilities")
    if not isinstance(probabilities, dict):
        raise RuntimeError("scope answer is missing probabilities")

    p_in_scope = float(probabilities.get("in_scope", 0.0))
    p_out_of_scope = float(probabilities.get("out_of_scope", 0.0))
    p_unsure = float(probabilities.get("unsure", 0.0))
    p_actual_use = float(actual.get("noul", 0.0))
    p_microbial_only = float(microbial.get("noul", 0.0))
    p_method_relevance = float(method.get("noul", 0.0))

    label = _decide_label(
        p_in_scope=p_in_scope,
        p_out_of_scope=p_out_of_scope,
        p_actual_use=p_actual_use,
        p_method_relevance=p_method_relevance,
        include_threshold=include_threshold,
        actual_use_threshold=actual_use_threshold,
        exclude_threshold=exclude_threshold,
        exclude_actual_use_max=exclude_actual_use_max,
        exclude_method_relevance_max=exclude_method_relevance_max,
    )
    reason = (
        f"{label}: scope={scope.get('choice', '')}; "
        f"p_in_scope={p_in_scope:.3f}; p_out_of_scope={p_out_of_scope:.3f}; p_unsure={p_unsure:.3f}; "
        f"actual_use={p_actual_use:.3f}; microbial_only={p_microbial_only:.3f}; "
        f"method_relevance={p_method_relevance:.3f}"
    )

    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    return {
        "flag_label": label,
        "flag_confidence": round(float(scope.get("confidence", 0.0)), 6),
        "flag_reason": reason,
        "flag_model_path": str(data.get("model", "strands-decider")),
        "flag_prompt_version": PROMPT_VERSION,
        "strands_scope_choice": str(scope.get("choice", "")),
        "strands_p_in_scope": round(p_in_scope, 6),
        "strands_p_out_of_scope": round(p_out_of_scope, 6),
        "strands_p_unsure": round(p_unsure, 6),
        "strands_p_actual_use": round(p_actual_use, 6),
        "strands_p_microbial_only": round(p_microbial_only, 6),
        "strands_p_method_relevance": round(p_method_relevance, 6),
        "strands_latency_ms": data.get("latency_ms", ""),
        "strands_input_tokens": usage.get("input_tokens", ""),
    }


@app.command()
def flag(
    input_csv: Path = typer.Argument(..., exists=True, dir_okay=False),
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
    out_csv: Path | None = typer.Option(None, "--out-csv"),
    base_url: str | None = typer.Option(None, "--base-url"),
    abstract_column: str | None = typer.Option(None, "--abstract-column"),
    timeout: float | None = typer.Option(None, "--timeout"),
    retries: int | None = typer.Option(None, "--retries"),
    include_threshold: float | None = typer.Option(None, "--include-threshold"),
    actual_use_threshold: float | None = typer.Option(None, "--actual-use-threshold"),
    exclude_threshold: float | None = typer.Option(None, "--exclude-threshold"),
    exclude_actual_use_max: float | None = typer.Option(None, "--exclude-actual-use-max"),
    exclude_method_relevance_max: float | None = typer.Option(None, "--exclude-method-relevance-max"),
    max_abstract_chars: int | None = typer.Option(None, "--max-abstract-chars"),
    batch_size: int | None = typer.Option(None, "--batch-size"),
    batch_index: int | None = typer.Option(None, "--batch-index"),
    limit: int | None = typer.Option(None, "--limit"),
    resume: bool | None = typer.Option(None, "--resume/--no-resume"),
    dry_run: bool | None = typer.Option(None, "--dry-run"),
    log_file: Path | None = typer.Option(None, "--log-file"),
    log_level: str | None = typer.Option(None, "--log-level"),
) -> None:
    """Flag eDNA/eRNA papers in a CSV using a running Strands Decider server."""
    cfg = _load_config(config)

    out_csv = Path(_coalesce(out_csv, cfg, "out_csv", "results/strands_flagged.csv"))
    base_url = str(_coalesce(base_url, cfg, "base_url", DEFAULT_BASE_URL)).rstrip("/")
    abstract_column = str(_coalesce(abstract_column, cfg, "abstract_column", "abstract"))
    timeout = float(_coalesce(timeout, cfg, "timeout", 120.0))
    retries = int(_coalesce(retries, cfg, "retries", 3))
    include_threshold = float(_coalesce(include_threshold, cfg, "include_threshold", 0.70))
    actual_use_threshold = float(_coalesce(actual_use_threshold, cfg, "actual_use_threshold", 0.60))
    exclude_threshold = float(_coalesce(exclude_threshold, cfg, "exclude_threshold", 0.50))
    exclude_actual_use_max = float(_coalesce(exclude_actual_use_max, cfg, "exclude_actual_use_max", 0.50))
    exclude_method_relevance_max = float(
        _coalesce(exclude_method_relevance_max, cfg, "exclude_method_relevance_max", 0.60)
    )
    max_abstract_chars = _coalesce(max_abstract_chars, cfg, "max_abstract_chars", None)
    max_abstract_chars = int(max_abstract_chars) if max_abstract_chars is not None else None
    batch_size = _coalesce(batch_size, cfg, "batch_size", None)
    batch_size = int(batch_size) if batch_size is not None else None
    batch_index = int(_coalesce(batch_index, cfg, "batch_index", 0))
    limit = _coalesce(limit, cfg, "limit", None)
    limit = int(limit) if limit is not None else None
    resume = bool(_coalesce(resume, cfg, "resume", True))
    dry_run = bool(_coalesce(dry_run, cfg, "dry_run", False))
    log_file_value = _coalesce(log_file, cfg, "log_file", "logs/strands_flagger.log")
    log_file = Path(log_file_value) if log_file_value else None
    log_level = str(_coalesce(log_level, cfg, "log_level", "INFO"))

    if batch_size is not None and limit is not None:
        raise typer.BadParameter("--batch-size and --limit cannot be used together")
    if batch_size is not None and batch_size <= 0:
        raise typer.BadParameter("--batch-size must be > 0")
    if batch_index < 0:
        raise typer.BadParameter("--batch-index must be >= 0")

    logger = setup_logger("strands_flagger", log_level=log_level, log_file=log_file)
    df = pd.read_csv(input_csv, dtype=str, keep_default_na=False)
    if abstract_column not in df.columns:
        raise typer.BadParameter(
            f"abstract column {abstract_column!r} was not found; available columns: {', '.join(df.columns)}"
        )

    if batch_size is not None:
        start = batch_index * batch_size
        df = df.iloc[start : start + batch_size]
    elif limit is not None:
        df = df.iloc[:limit]

    if df.empty:
        typer.echo("No rows to process.")
        return

    out_csv.parent.mkdir(parents=True, exist_ok=True)
    processed = _existing_processed_ids(out_csv) if resume else set()
    fieldnames = list(df.columns) + [col for col in EXTRA_COLUMNS if col not in df.columns]

    session = requests.Session()
    try:
        health = session.get(f"{base_url}/health", timeout=min(timeout, 15.0))
        health.raise_for_status()
        health_data = health.json()
        logger.info(
            "server status=%s model=%s device=%s max_length=%s",
            health_data.get("status"),
            health_data.get("model"),
            health_data.get("device"),
            health_data.get("max_length"),
        )
    except Exception as exc:
        raise typer.BadParameter(f"could not connect to Strands Decider at {base_url}: {exc}") from exc

    write_header = not out_csv.exists() or out_csv.stat().st_size == 0
    processed_count = 0
    skipped_count = 0
    error_count = 0

    with out_csv.open("a", encoding="utf-8", newline="") as handle:
        import csv

        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        if write_header:
            writer.writeheader()

        progress = tqdm(
            df.iterrows(),
            total=len(df),
            desc="Strands screening",
            position=0,
            dynamic_ncols=True,
        )
        status = tqdm(
            total=0,
            position=1,
            bar_format="{desc}",
            leave=False,
        )

        for _, row in progress:
            meta = _row_to_meta(row)
            record_id = _record_id(meta)
            if resume and record_id in processed:
                skipped_count += 1
                continue

            abstract = _prepare_abstract(meta.get(abstract_column, ""), max_abstract_chars)
            out_row = meta.copy()
            out_row["flag_record_id"] = record_id

            if not abstract:
                out_row.update(
                    {
                        "flag_label": "unsure",
                        "flag_confidence": "",
                        "flag_reason": "unsure: abstract is empty",
                        "flag_model_path": "",
                        "flag_prompt_version": PROMPT_VERSION,
                    }
                )
            else:
                payload = {"state": abstract, "questions": QUESTIONS}
                if dry_run:
                    typer.echo(json.dumps(payload, indent=2, ensure_ascii=False))
                    return
                try:
                    data = _evaluate_questions_sequentially(
                        session=session,
                        url=f"{base_url}/v1/systemone",
                        state=abstract,
                        questions=QUESTIONS,
                        timeout=timeout,
                        retries=retries,
                    )
                    out_row.update(
                        _parse_response(
                            data=data,
                            include_threshold=include_threshold,
                            actual_use_threshold=actual_use_threshold,
                            exclude_threshold=exclude_threshold,
                            exclude_actual_use_max=exclude_actual_use_max,
                            exclude_method_relevance_max=exclude_method_relevance_max,
                        )
                    )
                except Exception as exc:
                    error_count += 1
                    logger.warning("process_error record=%s error=%s", record_id, exc)
                    out_row.update(
                        {
                            "flag_label": "process_error",
                            "flag_confidence": "",
                            "flag_reason": str(exc),
                            "flag_model_path": "",
                            "flag_prompt_version": PROMPT_VERSION,
                        }
                    )

            writer.writerow(out_row)
            handle.flush()
            processed_count += 1

            title = meta.get("title", "").strip()
            label = str(out_row.get("flag_label", ""))
            p_in = out_row.get("strands_p_in_scope")
            p_out = out_row.get("strands_p_out_of_scope")

            if isinstance(p_in, (int, float)) and isinstance(p_out, (int, float)):
                message = f"[{label}] in={p_in:.2f} out={p_out:.2f} | {title}"
            else:
                message = f"[{label}] | {title}"

            status.set_description_str(message, refresh=True)

        status.clear()
        status.close()
        progress.refresh()

    typer.echo(
        f"Finished: processed={processed_count} skipped={skipped_count} errors={error_count} output={out_csv}"
    )


if __name__ == "__main__":
    app()
