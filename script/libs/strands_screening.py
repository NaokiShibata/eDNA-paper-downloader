from __future__ import annotations

import csv
import json
import shutil
import time
from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

import requests

from libs.text_normalize import clean_doi

PROMPT_VERSION = "strands-v3"
MIN_ABSTRACT_CHARS_FOR_EXCLUSION = 300
DEFAULT_BASE_URL = "http://127.0.0.1:8012"


@dataclass(frozen=True)
class ScreeningConfig:
    base_url: str = DEFAULT_BASE_URL
    timeout: float = 120.0
    retries: int = 3
    include_threshold: float = 0.45
    include_microbial_only_max: float = 0.35
    actual_use_threshold: float = 0.60
    exclude_threshold: float = 0.50
    exclude_actual_use_max: float = 0.50
    exclude_method_relevance_max: float = 0.60
    max_abstract_chars: int | None = 6000
    batch_questions: bool = False

    @classmethod
    def from_sources(cls, config: Mapping[str, Any], **overrides: Any) -> ScreeningConfig:
        values: dict[str, Any] = {}
        for field in fields(cls):
            name = field.name
            value = coalesce(overrides.get(name), config, name, field.default)
            if value is not None:
                if name == "base_url":
                    value = str(value).rstrip("/")
                elif name in ("retries", "max_abstract_chars"):
                    value = int(value)
                elif name == "batch_questions":
                    value = bool(value)
                else:
                    value = float(value)
            values[name] = value
        return cls(**values)


QUESTIONS: dict[str, dict[str, Any]] = {
    "scope": {
        "type": "choice",
        "instructions": (
            "Classify whether this scientific abstract should be retained as environmental DNA (eDNA) or "
            "environmental RNA (eRNA) research. Judge what the study actually does, not whether the terms eDNA, "
            "eRNA, environmental DNA, or environmental RNA literally appear."
        ),
        "criteria": {
            "in_scope": (
                "The study actually collects, detects, quantifies, sequences, analyzes, validates, compares, or "
                "models DNA or RNA obtained directly from an environmental sample or environmental matrix. Examples "
                "include water, seawater, freshwater, sediment, soil, air, snow, ice, wastewater, biofilms, passive "
                "samplers, environmental swabs, dust, or similar material. Include studies using these nucleic acids "
                "to detect or characterize organisms, taxa, populations, communities, biodiversity, biological "
                "signals, pathogens, or ecological patterns. The eDNA/eRNA terminology does not need to be explicit. "
                "The study must report its own primary data from such environmental samples."
            ),
            "out_of_scope": (
                "The study does not itself analyze environmentally obtained DNA or RNA. This includes reviews, "
                "systematic reviews, meta-analyses of published studies, perspectives, opinion pieces, editorials, "
                "book chapters, and conference reports, even when eDNA/eRNA is their main subject; studies based only"
                " on tissue, blood, isolated organisms, cultured strains, museum specimens, individual genomes, "
                "ordinary transcriptomics, diet or gut-content DNA, or host-associated microbiomes (gut, skin, plant "
                "or fruit surfaces); generic profiling of microbial communities (bacteria, archaea, fungi, "
                "microalgae, protists, viruses) or metagenomics/metatranscriptomics without an eDNA/eRNA detection, "
                "monitoring, or methodological purpose; and studies that mention eDNA/eRNA only in the background, "
                "discussion, comparison, citation, or future work."
            ),
            "unsure": (
                "The abstract does not provide enough information to determine whether environmentally obtained DNA "
                "or RNA was actually collected or analyzed. Prefer unsure over guessing when evidence is ambiguous."
            ),
        },
    },
    "actual_use": {
        "type": "noul",
        "instructions": (
            "Does the study actually collect or analyze DNA or RNA obtained directly from an environmental sample"
            " or environmental matrix as part of its methods or results?"
        ),
        "criteria": {
            "true": (
                "Environmental material is sampled and nucleic acids from that material are detected, quantified, "
                "sequenced, analyzed, compared, validated, or modeled."
            ),
            "false": (
                "No environmental nucleic-acid analysis is performed by the authors in this study. Reviews, meta-"
                "analyses of published studies, perspectives, editorials, book chapters, and conference reports are "
                "false even if they discuss eDNA/eRNA methods in depth. Also false when eDNA/eRNA is only background,"
                " comparison, or future work."
            ),
        },
    },
    "microbial_only": {
        "type": "noul",
        "instructions": (
            "Is this primarily a study of microbial communities (bacteria, archaea, fungi, microalgae, protists, "
            "or viruses) or of microbiomes, metagenomes, or metatranscriptomes, where nucleic acids are simply "
            "the source material and the study is not framed as eDNA/eRNA detection, monitoring, sampling, or "
            "method development?"
        ),
        "criteria": {
            "true": (
                "The main goal is to describe microbial community composition, diversity, function, or responses to "
                "environmental factors (e.g. soil fungal diversity, harmful algal assemblages, bacterial communities "
                "in water or sediment, MAGs, resistomes, viromes), without an explicit eDNA/eRNA detection, "
                "monitoring, or methodological contribution."
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
            "Does the study itself develop, evaluate, compare, or validate a sampling, preservation, extraction, "
            "detection, amplification, sequencing, quantification, bioinformatic, or modeling method for DNA or "
            "RNA recovered from environmental samples (water, sediment, soil, air, wastewater, biofilm, swabs, or"
            " similar), using its own data?"
        ),
        "criteria": {
            "true": (
                "The paper's own experiments or field data directly test or validate such a method on environmental "
                "DNA/RNA."
            ),
            "false": (
                "No such method is tested with the paper's own data. Reviews, perspectives, editorials, clinical or "
                "diagnostic assays on patient material, tissue or specimen analyses, phylogenetics, and environmental"
                " policy, chemistry, or ecology without environmental nucleic-acid methods are false."
            ),
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


SCORE_COLUMNS = [col for col in EXTRA_COLUMNS if col not in {
    "flag_record_id", "flag_label", "flag_reason", "flag_prompt_version",
}]


class ScoreCache:
    def __init__(self, path: Path):
        self.path = path
        self.rows: dict[str, dict[str, str]] = {}
        self.hits = 0
        self.misses = 0
        if path.exists():
            with path.open(newline="", encoding="utf-8") as stream:
                for row in csv.DictReader(stream):
                    if row.get("flag_prompt_version") == PROMPT_VERSION:
                        self.rows[row["flag_record_id"]] = {col: row[col] for col in SCORE_COLUMNS}

    def get(self, rec_id: str) -> dict[str, str] | None:
        scores = self.rows.get(rec_id)
        if scores is None:
            self.misses += 1
        else:
            self.hits += 1
        return scores

    def put(self, rec_id: str, scores: Mapping[str, Any]) -> None:
        row = {col: str(scores.get(col, "")) for col in SCORE_COLUMNS}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=["flag_record_id", "flag_prompt_version", *SCORE_COLUMNS])
            if stream.tell() == 0:
                writer.writeheader()
            writer.writerow({"flag_record_id": rec_id, "flag_prompt_version": PROMPT_VERSION, **row})
            stream.flush()
        self.rows[rec_id] = row


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


def load_config(path: Path | None) -> dict[str, Any]:
    if not path:
        return {}
    if not path.exists():
        raise FileNotFoundError(f"config not found: {path}")
    raw = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".json", ".jsonc"):
        return json.loads(_strip_jsonc(raw))
    raise ValueError("Strands config must be JSON or JSONC")


def coalesce(value: Any, config: Mapping[str, Any], key: str, default: Any) -> Any:
    if value is not None:
        return value
    if key in config:
        return config[key]
    return default


def record_id(meta: Mapping[str, str]) -> str:
    doi = clean_doi(meta.get("doi") or "")
    if doi:
        return f"doi:{doi}"
    title = (meta.get("title") or "").strip().lower()
    year = (meta.get("year") or "").strip()
    if title or year:
        return f"title:{title}|year:{year}"
    return json.dumps(dict(meta), sort_keys=True)


def prepare_abstract(value: str, max_chars: int | None) -> str:
    text = " ".join(str(value).split())
    if max_chars is None or max_chars <= 0 or len(text) <= max_chars:
        return text
    head_len = max(1, int(max_chars * 0.75))
    tail_len = max_chars - head_len
    return f"{text[:head_len].rstrip()} ... {text[-tail_len:].lstrip()}"


def build_state(meta: Mapping[str, str], cfg: ScreeningConfig, abstract_column: str = "abstract") -> str:
    abstract = prepare_abstract(meta.get(abstract_column, ""), cfg.max_abstract_chars)
    title = (meta.get("title") or "").strip()
    return f"Title: {title}\nAbstract: {abstract}" if title else abstract


def check_health(
    session: requests.Session,
    base_url: str,
    timeout: float,
) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/health"
    response = session.get(url, timeout=min(timeout, 15.0))
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError("Strands Decider health endpoint returned a non-object response")
    if data.get("status") != "ok":
        raise RuntimeError(f"unexpected Strands Decider health response: {data!r}")
    return data


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


def evaluate_questions_sequentially(
    session: requests.Session,
    url: str,
    state: str,
    questions: dict[str, dict[str, Any]],
    timeout: float,
    retries: int,
) -> dict[str, Any]:
    """Evaluate one question per request to minimize peak CUDA memory."""
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
        usage_raw = data.get("usage")
        usage: dict[str, Any] = usage_raw if isinstance(usage_raw, dict) else {}
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


def decide_label(
    p_in_scope: float,
    p_out_of_scope: float,
    p_actual_use: float,
    p_method_relevance: float,
    p_microbial_only: float,
    include_threshold: float,
    include_microbial_only_max: float,
    actual_use_threshold: float,
    exclude_threshold: float,
    exclude_actual_use_max: float,
    exclude_method_relevance_max: float,
) -> str:
    if (
        p_in_scope >= include_threshold
        and p_actual_use >= actual_use_threshold
        and p_microbial_only < include_microbial_only_max
    ):
        return "in_scope"
    if (
        p_out_of_scope >= exclude_threshold
        and p_actual_use <= exclude_actual_use_max
        and p_method_relevance <= exclude_method_relevance_max
    ):
        return "out_of_scope"
    return "unsure"


def extract_scores(data: dict[str, Any]) -> dict[str, Any]:
    answers = data.get("answers")
    if not isinstance(answers, dict):
        raise RuntimeError("response is missing answers")

    scope = answers.get("scope")
    actual = answers.get("actual_use")
    microbial = answers.get("microbial_only")
    method = answers.get("method_relevance")
    if not isinstance(scope, dict):
        raise RuntimeError("response is missing scope answer")
    if not isinstance(actual, dict):
        raise RuntimeError("response is missing actual_use answer")
    if not isinstance(microbial, dict):
        raise RuntimeError("response is missing microbial_only answer")
    if not isinstance(method, dict):
        raise RuntimeError("response is missing method_relevance answer")

    probabilities = scope.get("probabilities")
    if not isinstance(probabilities, dict):
        raise RuntimeError("scope answer is missing probabilities")

    p_in_scope = float(probabilities.get("in_scope", 0.0))
    p_out_of_scope = float(probabilities.get("out_of_scope", 0.0))
    p_unsure = float(probabilities.get("unsure", 0.0))
    p_actual_use = float(actual.get("noul", 0.0))
    p_microbial_only = float(microbial.get("noul", 0.0))
    p_method_relevance = float(method.get("noul", 0.0))

    usage_raw = data.get("usage")
    usage: dict[str, Any] = usage_raw if isinstance(usage_raw, dict) else {}
    return {
        "flag_confidence": round(float(scope.get("confidence", 0.0)), 6),
        "flag_model_path": str(data.get("model", "strands-decider")),
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


def apply_thresholds(scores: Mapping[str, Any], cfg: ScreeningConfig) -> dict[str, Any]:
    p_in_scope = float(scores["strands_p_in_scope"])
    p_out_of_scope = float(scores["strands_p_out_of_scope"])
    p_unsure = float(scores["strands_p_unsure"])
    p_actual_use = float(scores["strands_p_actual_use"])
    p_microbial_only = float(scores["strands_p_microbial_only"])
    p_method_relevance = float(scores["strands_p_method_relevance"])
    label = decide_label(
        p_in_scope=p_in_scope,
        p_out_of_scope=p_out_of_scope,
        p_actual_use=p_actual_use,
        p_method_relevance=p_method_relevance,
        p_microbial_only=p_microbial_only,
        include_threshold=cfg.include_threshold,
        include_microbial_only_max=cfg.include_microbial_only_max,
        actual_use_threshold=cfg.actual_use_threshold,
        exclude_threshold=cfg.exclude_threshold,
        exclude_actual_use_max=cfg.exclude_actual_use_max,
        exclude_method_relevance_max=cfg.exclude_method_relevance_max,
    )
    reason = (
        f"{label}: scope={scores.get('strands_scope_choice', '')}; "
        f"p_in_scope={p_in_scope:.3f}; p_out_of_scope={p_out_of_scope:.3f}; p_unsure={p_unsure:.3f}; "
        f"actual_use={p_actual_use:.3f}; microbial_only={p_microbial_only:.3f}; "
        f"method_relevance={p_method_relevance:.3f}"
    )

    return {"flag_label": label, "flag_reason": reason, "flag_prompt_version": PROMPT_VERSION}


def evaluate_abstract(session: requests.Session, abstract: str, cfg: ScreeningConfig) -> dict[str, Any]:
    url = f"{cfg.base_url.rstrip('/')}/v1/systemone"
    if cfg.batch_questions:
        data = _post_with_retry(session, url, {"state": abstract, "questions": QUESTIONS}, cfg.timeout, cfg.retries)
    else:
        data = evaluate_questions_sequentially(session, url, abstract, QUESTIONS, cfg.timeout, cfg.retries)
    return extract_scores(data)


def classify_abstract(session: requests.Session, abstract: str, cfg: ScreeningConfig) -> dict[str, Any]:
    scores = evaluate_abstract(session, abstract, cfg)
    return scores | apply_thresholds(scores, cfg)


def screen_row(
    session: requests.Session,
    meta: Mapping[str, str],
    cfg: ScreeningConfig,
    abstract_column: str = "abstract",
    cache: ScoreCache | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {"flag_record_id": record_id(meta)}
    abstract = prepare_abstract(meta.get(abstract_column, ""), cfg.max_abstract_chars)
    if not abstract:
        label, reason = "unsure", "unsure: abstract is empty"
    else:
        try:
            scores = cache.get(result["flag_record_id"]) if cache is not None else None
            if scores is None:
                scores = evaluate_abstract(session, build_state(meta, cfg, abstract_column), cfg)
                labels = apply_thresholds(scores, cfg)
                if cache is not None:
                    cache.put(result["flag_record_id"], scores)
            else:
                labels = apply_thresholds(scores, cfg)
            if len(abstract) < MIN_ABSTRACT_CHARS_FOR_EXCLUSION and labels["flag_label"] == "out_of_scope":
                labels["flag_label"] = "unsure"
                labels["flag_reason"] = "unsure: abstract too short to exclude; " + labels["flag_reason"]
            return result | scores | labels
        except Exception as exc:
            label, reason = "process_error", str(exc)
    return result | {
        "flag_label": label,
        "flag_confidence": "",
        "flag_reason": reason,
        "flag_model_path": "",
        "flag_prompt_version": PROMPT_VERSION,
    }


def format_status(row: Mapping[str, Any], title: str) -> str:
    label = str(row.get("flag_label", ""))
    p_in = row.get("strands_p_in_scope")
    p_out = row.get("strands_p_out_of_scope")
    label_display = f"[{label:<13}]"
    if isinstance(p_in, (int, float)) and isinstance(p_out, (int, float)):
        message = f"{label_display} in={p_in:.2f} out={p_out:.2f} | {title}"
    else:
        message = f"{label_display} | {title}"
    terminal_width = shutil.get_terminal_size(fallback=(120, 24)).columns
    max_status_width = max(20, terminal_width - 1)
    if len(message) > max_status_width:
        message = message[: max_status_width - 3] + "..."
    return message
