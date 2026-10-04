from __future__ import annotations

import json
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import requests

from libs.text_normalize import clean_doi

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


def parse_response(
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

    label = decide_label(
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

    usage_raw = data.get("usage")
    usage: dict[str, Any] = usage_raw if isinstance(usage_raw, dict) else {}
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


def classify_abstract(
    session: requests.Session,
    abstract: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = 120.0,
    retries: int = 3,
    include_threshold: float = 0.70,
    actual_use_threshold: float = 0.60,
    exclude_threshold: float = 0.50,
    exclude_actual_use_max: float = 0.50,
    exclude_method_relevance_max: float = 0.60,
) -> dict[str, Any]:
    data = evaluate_questions_sequentially(
        session=session,
        url=f"{base_url.rstrip('/')}/v1/systemone",
        state=abstract,
        questions=QUESTIONS,
        timeout=timeout,
        retries=retries,
    )
    return parse_response(
        data=data,
        include_threshold=include_threshold,
        actual_use_threshold=actual_use_threshold,
        exclude_threshold=exclude_threshold,
        exclude_actual_use_max=exclude_actual_use_max,
        exclude_method_relevance_max=exclude_method_relevance_max,
    )
