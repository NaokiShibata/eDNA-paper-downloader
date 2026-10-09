from __future__ import annotations

import csv
import hashlib
import json
import math
import shutil
import time
from collections.abc import Mapping
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any

import requests

from libs.text_normalize import clean_doi

PROMPT_VERSION = "edna-macrofauna-v6"
MIN_ABSTRACT_CHARS_FOR_EXCLUSION = 300
DEFAULT_BASE_URL = "http://127.0.0.1:8012"


@dataclass(frozen=True)
class ScreeningConfig:
    base_url: str = DEFAULT_BASE_URL
    expected_model: str | None = None
    first_stage_base_url: str | None = None
    first_stage_model: str | None = None
    first_stage_batch_questions: bool = True
    timeout: float = 120.0
    retries: int = 3
    include_threshold: float = 0.45
    include_microbial_only_max: float = 0.35
    exclude_microbial_only_min: float = 0.80
    actual_use_threshold: float = 0.60
    exclude_threshold: float = 0.50
    exclude_actual_use_max: float = 0.50
    exclude_method_relevance_max: float = 0.60
    exclude_review_min: float = 0.60
    exclude_review_out_min: float = 0.40
    max_abstract_chars: int | None = 6000
    batch_questions: bool = False
    cache_csv: str | None = ".cache/strands_scores.csv"

    @classmethod
    def from_sources(cls, config: Mapping[str, Any], **overrides: Any) -> ScreeningConfig:
        unknown = (set(config) | set(overrides)) - {field.name for field in fields(cls)}
        if unknown:
            raise ValueError(f"Unknown screening config keys: {', '.join(sorted(unknown))}")
        values: dict[str, Any] = {}
        for field in fields(cls):
            name = field.name
            value = coalesce(overrides.get(name), config, name, field.default)
            if value is not None:
                if name in ("base_url", "first_stage_base_url"):
                    value = str(value).rstrip("/")
                elif name in ("cache_csv", "expected_model", "first_stage_model"):
                    value = str(value)
                elif name in ("retries", "max_abstract_chars"):
                    value = int(value)
                elif name in ("batch_questions", "first_stage_batch_questions"):
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
            "environmental RNA (eRNA) research. Studies targeting the detection, identification, monitoring, "
            "distribution or diversity of fungi (including yeasts, molds, mushrooms and fungal pathogens) are "
            "out of scope, even when they use environmental DNA/RNA or develop fungal detection methods. "
            "Fungi are not vertebrates or invertebrates. A passing mention of fungi does not exclude a study "
            "whose actual environmental DNA/RNA detection target is macroscopic vertebrates or invertebrates. "
            "Microbiome-only and microbial-only studies are out of scope, "
            "even if described as eDNA/eRNA detection, monitoring, or method development, unless the study directly "
            "evaluates methods for detecting or monitoring macroscopic vertebrates or invertebrates. Judge what the study "
            "actually does, not whether the terms eDNA, "
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
                "The study must report its own primary data from such environmental samples. Microbial-only studies "
                "qualify only when directly evaluating methods for detection or monitoring of macroscopic vertebrates "
                "or invertebrates; microbial composition, function, or microbial detection alone does not qualify."
            ),
            "out_of_scope": (
                "The study targets fungi rather than macroscopic vertebrates or invertebrates: fungal species "
                "detection, fungal pathogens, airborne fungal spores, soil fungi, mycobiomes or fungal diversity, "
                "including environmental DNA/RNA and fungal-specific detection methods. Also out of scope when "
                "the study does not itself analyze environmentally obtained DNA or RNA. This includes reviews, "
                "systematic reviews, meta-analyses of published studies, perspectives, opinion pieces, editorials, "
                "book chapters, and conference reports, even when eDNA/eRNA is their main subject; studies based only"
                " on tissue, blood, isolated organisms, cultured strains, museum specimens, individual genomes, "
                "ordinary transcriptomics, diet or gut-content DNA, or host-associated microbiomes (gut, skin, plant "
                "or fruit surfaces); generic profiling of microbial communities (bacteria, archaea, fungi, "
                "microalgae, protists, viruses) or metagenomics/metatranscriptomics without an eDNA/eRNA detection, "
                "monitoring, or methodological purpose for macroscopic vertebrates or invertebrates; and studies that "
                "mention eDNA/eRNA only in the background, "
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
            "method development for macroscopic vertebrates or invertebrates? Microbial-only methods, detection "
            "and monitoring still count as microbial_only. Studies targeting fungi, including yeasts, molds, "
            "mushrooms and fungal pathogens, count as true regardless of fungal size or eDNA terminology. "
            "Do not mistake fungi for macroscopic invertebrate animals."
        ),
        "criteria": {
            "true": (
                "The main goal is to describe microbial community composition, diversity, function, or responses to "
                "environmental factors (e.g. soil fungal diversity, harmful algal assemblages, bacterial communities "
                "in water or sediment, MAGs, resistomes, viromes), without an explicit eDNA/eRNA detection, "
                "monitoring, or methodological contribution related to macroscopic vertebrates or invertebrates."
            ),
            "false": (
                "The study is not merely generic microbial profiling, or it has a meaningful eDNA/eRNA detection, "
                "monitoring, sampling, quantification, validation, or methodological component directly related to "
                "macroscopic vertebrates or invertebrates. Do not mark true "
                "solely because marker or metabarcoding terms appear. A microbial-only study remains true even when "
                "it uses eDNA terminology or develops microbial detection methods."
            ),
        },
    },
    "method_relevance": {
        "type": "noul",
        "instructions": (
            "Does the study itself develop, evaluate, compare, or validate a sampling, preservation, extraction, "
            "detection, amplification, sequencing, quantification, bioinformatic, or modeling method for DNA or "
            "RNA recovered from environmental samples (water, sediment, soil, air, wastewater, biofilm, swabs, or"
            " similar), using its own data? For microbial studies, qualify only if the method is directly "
            "evaluated for detection or monitoring of macroscopic vertebrates or invertebrates; microbial-only "
            "detection, profiling and activity measurement do not qualify. Methods intended to detect, identify "
            "or monitor fungi or fungal pathogens do not qualify; fungi are not invertebrate animals."
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
    "study_type": {
        "type": "choice",
        "instructions": "Classify the type of publication described by this title and abstract.",
        "criteria": {
            "primary_research": (
                "Reports the authors' own new empirical data: field sampling, laboratory experiments, assay development "
                "and validation, surveys, mesocosms, case studies, datasets, or new analyses of samples they collected or obtained."
            ),
            "review": (
                "Summarizes or discusses existing work without new empirical data of its own: narrative or systematic reviews, "
                "meta-analyses of published studies, perspectives, opinion pieces, commentaries, editorials, book or chapter "
                "introductions, conference reports, and correction notices."
            ),
            "other": "Anything else, including policy, social-science, or theoretical work not based on new empirical data.",
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
    "evaluation_stage",
    "first_stage_model",
    "first_stage_label",
    "first_stage_p_in_scope",
    "first_stage_p_out_of_scope",
    "first_stage_confidence",
    "first_stage_latency_ms",
    "first_stage_error",
    "strands_scope_choice",
    "strands_p_in_scope",
    "strands_p_out_of_scope",
    "strands_p_unsure",
    "strands_p_actual_use",
    "strands_p_microbial_only",
    "strands_p_method_relevance",
    "strands_p_review",
    "strands_latency_ms",
    "strands_input_tokens",
]


SCORE_COLUMNS = [col for col in EXTRA_COLUMNS if col not in {
    "flag_record_id", "flag_label", "flag_reason", "flag_prompt_version",
}]


def output_column(name: str) -> str:
    return name.removeprefix("strands_") if name in EXTRA_COLUMNS else name


def output_row(row: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(row)
    for name in EXTRA_COLUMNS:
        if name.startswith("strands_") and name in result:
            result[output_column(name)] = result.pop(name)
    return result


class ScoreCache:
    def __init__(self, path: Path):
        path = path.with_name(f"{path.stem}.{PROMPT_VERSION}{path.suffix}")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.rows: dict[str, dict[str, str]] = {}
        self.hits = 0
        self.misses = 0
        self._incompatible_header = False
        if path.exists():
            with path.open(newline="", encoding="utf-8") as stream:
                reader = csv.DictReader(stream)
                self._incompatible_header = not set(["flag_record_id", "flag_prompt_version", *(output_column(col) for col in SCORE_COLUMNS)]).issubset(reader.fieldnames or [])
                if self._incompatible_header:
                    return
                for row in reader:
                    if row.get("flag_prompt_version") == PROMPT_VERSION:
                        self.rows[row["flag_record_id"]] = {col: row[output_column(col)] for col in SCORE_COLUMNS}

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
        with self.path.open("w" if self._incompatible_header else "a", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=["flag_record_id", "flag_prompt_version", *(output_column(col) for col in SCORE_COLUMNS)])
            if stream.tell() == 0:
                writer.writeheader()
            writer.writerow(output_row({"flag_record_id": rec_id, "flag_prompt_version": PROMPT_VERSION, **row}))
            stream.flush()
        self._incompatible_header = False
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


def default_config_path() -> Path:
    config_dir = Path(__file__).resolve().parents[2] / "config"
    local = config_dir / "clef_flagger.jsonc"
    return local if local.exists() else config_dir / "clef_flagger.example.jsonc"


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
    first_stage_base_url: str | None = None,
) -> dict[str, Any]:
    if first_stage_base_url is not None:
        check_health(session, first_stage_base_url, timeout)
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
    p_review: float,
    include_threshold: float,
    include_microbial_only_max: float,
    actual_use_threshold: float,
    exclude_threshold: float,
    exclude_actual_use_max: float,
    exclude_method_relevance_max: float,
    exclude_review_min: float,
    exclude_review_out_min: float,
    exclude_microbial_only_min: float = 0.80,
) -> str:
    if p_microbial_only >= exclude_microbial_only_min and p_method_relevance <= exclude_method_relevance_max:
        return "out_of_scope"
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
    ) or (p_review >= exclude_review_min and p_out_of_scope >= exclude_review_out_min):
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

    study_type = answers.get("study_type")
    if not isinstance(study_type, dict):
        raise RuntimeError("response is missing study_type answer")
    study_probabilities = study_type.get("probabilities")
    if not isinstance(study_probabilities, dict):
        raise RuntimeError("study_type answer is missing probabilities")

    probabilities = scope.get("probabilities")
    if not isinstance(probabilities, dict):
        raise RuntimeError("scope answer is missing probabilities")

    p_in_scope = float(probabilities.get("in_scope", 0.0))
    p_out_of_scope = float(probabilities.get("out_of_scope", 0.0))
    p_unsure = float(probabilities.get("unsure", 0.0))
    p_actual_use = float(actual.get("noul", 0.0))
    p_microbial_only = float(microbial.get("noul", 0.0))
    p_method_relevance = float(method.get("noul", 0.0))
    p_review = float(study_probabilities.get("review", 0.0))

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
        "strands_p_review": round(p_review, 6),
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
    p_review = float(scores["strands_p_review"])
    label = decide_label(
        p_in_scope=p_in_scope,
        p_out_of_scope=p_out_of_scope,
        p_actual_use=p_actual_use,
        p_method_relevance=p_method_relevance,
        p_microbial_only=p_microbial_only,
        p_review=p_review,
        include_threshold=cfg.include_threshold,
        include_microbial_only_max=cfg.include_microbial_only_max,
        exclude_microbial_only_min=cfg.exclude_microbial_only_min,
        actual_use_threshold=cfg.actual_use_threshold,
        exclude_threshold=cfg.exclude_threshold,
        exclude_actual_use_max=cfg.exclude_actual_use_max,
        exclude_method_relevance_max=cfg.exclude_method_relevance_max,
        exclude_review_min=cfg.exclude_review_min,
        exclude_review_out_min=cfg.exclude_review_out_min,
    )
    reason = (
        f"{label}: scope={scores.get('strands_scope_choice', '')}; "
        f"p_in_scope={p_in_scope:.3f}; p_out_of_scope={p_out_of_scope:.3f}; p_unsure={p_unsure:.3f}; "
        f"actual_use={p_actual_use:.3f}; microbial_only={p_microbial_only:.3f}; "
        f"method_relevance={p_method_relevance:.3f}; review={p_review:.3f}"
    )

    return {"flag_label": label, "flag_reason": reason, "flag_prompt_version": PROMPT_VERSION}


def evaluate_abstract(session: requests.Session, abstract: str, cfg: ScreeningConfig) -> dict[str, Any]:
    if cfg.first_stage_base_url is None:
        return _evaluate_abstract(session, abstract, cfg) | {"evaluation_stage": "single"}
    fast_cfg = replace(cfg, base_url=cfg.first_stage_base_url, expected_model=cfg.first_stage_model,
                       first_stage_base_url=None, batch_questions=cfg.first_stage_batch_questions)
    started = time.perf_counter()
    try:
        fast = _evaluate_abstract(session, abstract, fast_cfg)
        label = apply_thresholds(fast, cfg)["flag_label"]
        abstract_text = abstract.split("\nAbstract:", 1)[-1].strip()
        if label == "out_of_scope" and len(abstract_text) < MIN_ABSTRACT_CHARS_FOR_EXCLUSION:
            label = "unsure"
        metadata = {
            "first_stage_model": fast["flag_model_path"], "first_stage_label": label,
            "first_stage_p_in_scope": fast["strands_p_in_scope"],
            "first_stage_p_out_of_scope": fast["strands_p_out_of_scope"],
            "first_stage_latency_ms": fast["strands_latency_ms"], "first_stage_error": "",
        }
        probabilities = [float(fast[f"strands_p_{name}"]) for name in ("in_scope", "out_of_scope", "unsure")]
        checks = probabilities + [float(fast[f"strands_p_{name}"]) for name in
                                  ("actual_use", "microbial_only", "method_relevance", "review")]
        valid = all(math.isfinite(p) and 0 <= p <= 1 for p in checks) and math.isclose(sum(probabilities), 1, abs_tol=0.001)
        confidence = float(fast["strands_p_in_scope"]) if label == "in_scope" else float(fast["strands_p_out_of_scope"])
        if label == "out_of_scope":
            if float(fast["strands_p_method_relevance"]) <= cfg.exclude_method_relevance_max:
                confidence = max(confidence, float(fast["strands_p_microbial_only"]))
            if float(fast["strands_p_out_of_scope"]) >= cfg.exclude_review_out_min:
                confidence = max(confidence, float(fast["strands_p_review"]))
        metadata["first_stage_confidence"] = confidence
        if label in ("in_scope", "out_of_scope") and valid:
            return fast | metadata | {"evaluation_stage": "first"}
    except Exception as exc:
        metadata = {"first_stage_model": cfg.first_stage_model or "", "first_stage_label": "process_error",
                    "first_stage_error": str(exc), "first_stage_latency_ms": (time.perf_counter() - started) * 1000}
    final = _evaluate_abstract(session, abstract, cfg)
    final["strands_latency_ms"] = (time.perf_counter() - started) * 1000
    return final | metadata | {"evaluation_stage": "second"}


def _evaluate_abstract(session: requests.Session, abstract: str, cfg: ScreeningConfig) -> dict[str, Any]:
    url = f"{cfg.base_url.rstrip('/')}/v1/systemone"
    started = time.perf_counter()
    if cfg.batch_questions:
        data = _post_with_retry(session, url, {"state": abstract, "questions": QUESTIONS}, cfg.timeout, cfg.retries)
    else:
        data = evaluate_questions_sequentially(session, url, abstract, QUESTIONS, cfg.timeout, cfg.retries)
    if cfg.expected_model is not None and data.get("model") != cfg.expected_model:
        raise RuntimeError(f"expected model {cfg.expected_model!r}, got {data.get('model')!r}")
    data["latency_ms"] = (time.perf_counter() - started) * 1000
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
            state = build_state(meta, cfg, abstract_column)
            cache_key = result["flag_record_id"] + ":" + hashlib.sha256(json.dumps({
                "state": state, "questions": QUESTIONS, "server": cfg.base_url,
                "model": cfg.expected_model, "batch_questions": cfg.batch_questions,
                "first_stage_server": cfg.first_stage_base_url, "first_stage_model": cfg.first_stage_model,
                "first_stage_batch_questions": cfg.first_stage_batch_questions,
                "routing_policy": {field.name: getattr(cfg, field.name) for field in fields(cfg)
                                   if field.name.startswith(("include_", "exclude_", "actual_use_"))}
                                   if cfg.first_stage_base_url is not None else None,
            }, sort_keys=True).encode()).hexdigest()
            scores = cache.get(cache_key) if cache is not None else None
            if scores is None:
                scores = evaluate_abstract(session, state, cfg)
                labels = apply_thresholds(scores, cfg)
                if cache is not None:
                    cache.put(cache_key, scores)
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
