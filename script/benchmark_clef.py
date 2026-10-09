"""Evaluate a local Clef Q8 server with the unchanged Strands v4 policy."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

import requests

from libs.strands_screening import (
    MIN_ABSTRACT_CHARS_FOR_EXCLUSION,
    PROMPT_VERSION,
    QUESTIONS,
    ScreeningConfig,
    apply_thresholds,
    build_state,
    check_health,
    extract_scores,
    prepare_abstract,
)

MODELS = {
    "flash": {
        "model": "ggml-org/Clef-Flash-GGUF:Q8_0",
        "revision": "4a192915ef971886004b5b13294f2b4c7a7fc39d",
        "sha256": "d7c352faf1bdd9ea24d0b9347e8eb1eb4bbadeff6c02383bf750215a74f2f1f1",
        "alias": "clef-flash-q8",
        "file": "Clef-Flash-Q8_0.gguf",
    },
    "27b": {
        "model": "ggml-org/Clef-GGUF:Q8_0",
        "revision": "63840a1a68cb7084c88610cffc328509356b04cb",
        "sha256": "07c6410af7011e0e56873a3b0b3f4ad9e31fb176f0ca80d6fd1525fe6f036548",
        "alias": "clef-27b-q8",
        "file": "Clef-Q8_0.gguf",
    },
}


def validate_answers(data: dict[str, Any]) -> None:
    """Reject incomplete or invalid probabilities before applying exclusion rules."""
    answers = data.get("answers", {})
    for key, question in QUESTIONS.items():
        answer = answers.get(key, {})
        if question["type"] == "noul":
            values = [float(answer["noul"])]
        else:
            probabilities = answer["probabilities"]
            values = [float(probabilities[option]) for option in question["criteria"]]
            if not math.isclose(sum(values), 1.0, abs_tol=0.001):
                raise ValueError(f"{key}: probabilities do not sum to one")
        if any(not math.isfinite(value) or not 0 <= value <= 1 for value in values):
            raise ValueError(f"{key}: invalid probability")


def infer(source: Path, target: Path, base_url: str, model_file: Path, model_name: str = "flash") -> None:
    model_spec = MODELS[model_name]
    cfg = ScreeningConfig(base_url=base_url, cache_csv=None, batch_questions=True, retries=1)
    source_bytes = source.read_bytes()
    with source.open(newline="", encoding="utf-8") as stream:
        records = list(csv.DictReader(stream))
    ids = [row["benchmark_record_id"] for row in records]
    if not records or len(set(ids)) != len(ids) or any(not value for value in ids):
        raise ValueError("Input must contain unique nonempty benchmark_record_id values")
    if target.exists() or target.with_suffix(".run.json").exists():
        raise FileExistsError(f"Refusing to overwrite run: {target}")
    with model_file.open("rb") as model_stream:
        model_hash = hashlib.file_digest(model_stream, "sha256").hexdigest()
    if model_hash != model_spec["sha256"]:
        raise ValueError("Model file does not match the pinned official Q8_0 artifact")
    with requests.Session() as session:
        check_health(session, base_url, cfg.timeout)

        def predict(state: str) -> dict[str, Any]:
            started = time.perf_counter()
            response = session.post(f"{base_url.rstrip('/')}/v1/systemone",
                                    json={"model": model_spec["alias"], "state": state, "questions": QUESTIONS},
                                    timeout=cfg.timeout)
            response.raise_for_status()
            data = response.json()
            if data.get("model") != model_spec["alias"]:
                raise ValueError(f"Server must use the {model_spec['alias']} model alias")
            validate_answers(data)
            data["latency_ms"] = (time.perf_counter() - started) * 1000
            return extract_scores(data)

        predict("Title: eDNA monitoring\nAbstract: River water was sampled to quantify fish DNA by qPCR.")
        target.parent.mkdir(parents=True, exist_ok=True)
        latencies = []
        with target.open("x", newline="", encoding="utf-8") as stream:
            writer = None
            for index, row in enumerate(records, 1):
                scores = predict(build_state(row, cfg))
                labels = apply_thresholds(scores, cfg)
                if (len(prepare_abstract(row.get("abstract", ""), cfg.max_abstract_chars))
                        < MIN_ABSTRACT_CHARS_FOR_EXCLUSION and labels["flag_label"] == "out_of_scope"):
                    labels["flag_label"] = "unsure"
                    labels["flag_reason"] = "unsure: abstract too short to exclude"
                result = {"flag_record_id": row["benchmark_record_id"]} | scores | labels
                if writer is None:
                    writer = csv.DictWriter(stream, fieldnames=list(result))
                    writer.writeheader()
                writer.writerow(result)
                stream.flush()
                latencies.append(scores["strands_latency_ms"])
                if index % 10 == 0 or index == len(records):
                    print(f"{index}/{len(records)}", flush=True)
    provenance = {
        "model": model_spec["model"], "revision": model_spec["revision"],
        "model_file_sha256": model_hash, "input_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "prompt_version": PROMPT_VERSION, "config": asdict(cfg),
        "questions_sha256": hashlib.sha256(json.dumps(QUESTIONS, sort_keys=True).encode()).hexdigest(),
        "records": len(records), "mean_latency_ms": sum(latencies) / len(latencies),
    }
    target.with_suffix(".run.json").write_text(json.dumps(provenance, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:8014")
    parser.add_argument("--model", choices=list(MODELS), default="flash")
    parser.add_argument("--model-file", type=Path)
    args = parser.parse_args()
    model_file = args.model_file or Path(".cache/clef") / MODELS[args.model]["file"]
    infer(args.input, args.output, args.base_url, model_file, args.model)


if __name__ == "__main__":
    main()
