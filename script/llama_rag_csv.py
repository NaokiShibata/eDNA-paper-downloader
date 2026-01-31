from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

import pandas as pd
import requests
import typer
from tqdm import tqdm

app = typer.Typer(add_completion=False)

DEFAULT_TEXT_COLS = ["title", "journal", "year", "authors", "doi"]
DEFAULT_CONTEXT_COLS = ["title", "journal", "year", "authors", "doi"]


@dataclass
class RagRecord:
    doc_id: str
    text: str
    meta: Dict[str, str]
    embedding: List[float]


def _normalize_url(url: str) -> str:
    return url.rstrip("/")


def _split_cols(value: str) -> List[str]:
    cols = [c.strip() for c in value.split(",")]
    return [c for c in cols if c]


def _label(col: str) -> str:
    return col.replace("_", " ").title()


def _row_to_meta(row: pd.Series) -> Dict[str, str]:
    meta: Dict[str, str] = {}
    for col in row.index:
        val = row[col]
        if pd.isna(val):
            continue
        meta[str(col)] = str(val)
    return meta


def _build_text(meta: Dict[str, str], cols: Sequence[str]) -> str:
    parts: List[str] = []
    for col in cols:
        val = meta.get(col)
        if not val:
            continue
        parts.append(f"{_label(col)}: {val}")
    return "\n".join(parts).strip()


def _cosine_sim(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b:
        return -1.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    denom = math.sqrt(norm_a) * math.sqrt(norm_b)
    if denom == 0.0:
        return -1.0
    return dot / denom


def _embed_openai(
    base_url: str,
    texts: Sequence[str],
    model: Optional[str],
    timeout: float,
) -> List[List[float]]:
    url = f"{_normalize_url(base_url)}/v1/embeddings"
    payload: Dict[str, Any] = {"input": list(texts)}
    if model:
        payload["model"] = model
    resp = requests.post(url, json=payload, timeout=timeout)
    resp.raise_for_status()
    data = resp.json().get("data", [])
    data = sorted(data, key=lambda x: x.get("index", 0))
    return [item["embedding"] for item in data]


def _embed_legacy(
    base_url: str,
    texts: Sequence[str],
    timeout: float,
) -> List[List[float]]:
    url = f"{_normalize_url(base_url)}/embedding"
    embeddings: List[List[float]] = []
    for text in texts:
        payload = {"content": text, "input": text}
        resp = requests.post(url, json=payload, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        if "embedding" in data:
            embeddings.append(data["embedding"])
        elif "data" in data and data["data"]:
            embeddings.append(data["data"][0]["embedding"])
        else:
            raise ValueError("embedding response missing 'embedding' field")
    return embeddings


def _embed_texts(
    base_url: str,
    texts: Sequence[str],
    api: str,
    model: Optional[str],
    timeout: float,
) -> List[List[float]]:
    if api == "openai":
        return _embed_openai(base_url, texts, model, timeout)
    if api == "legacy":
        return _embed_legacy(base_url, texts, timeout)
    raise ValueError(f"unsupported embed api: {api}")


def _completion_openai(
    base_url: str,
    prompt: str,
    model: Optional[str],
    max_tokens: int,
    temperature: float,
    timeout: float,
) -> str:
    url = f"{_normalize_url(base_url)}/v1/completions"
    payload: Dict[str, Any] = {
        "prompt": prompt,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if model:
        payload["model"] = model
    resp = requests.post(url, json=payload, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0].get("text", "").strip()


def _completion_openai_chat(
    base_url: str,
    system_prompt: str,
    user_prompt: str,
    model: Optional[str],
    max_tokens: int,
    temperature: float,
    timeout: float,
) -> str:
    url = f"{_normalize_url(base_url)}/v1/chat/completions"
    payload: Dict[str, Any] = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if model:
        payload["model"] = model
    resp = requests.post(url, json=payload, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"].strip()


def _completion_legacy(
    base_url: str,
    prompt: str,
    max_tokens: int,
    temperature: float,
    timeout: float,
) -> str:
    url = f"{_normalize_url(base_url)}/completion"
    payload = {
        "prompt": prompt,
        "n_predict": max_tokens,
        "temperature": temperature,
    }
    resp = requests.post(url, json=payload, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    if "content" in data:
        return str(data["content"]).strip()
    if "choices" in data and data["choices"]:
        return str(data["choices"][0].get("text", "")).strip()
    return ""


def _format_context(records: Sequence[Dict[str, Any]], cols: Sequence[str]) -> str:
    lines: List[str] = []
    for idx, rec in enumerate(records, start=1):
        meta = rec.get("meta", {})
        parts: List[str] = []
        for col in cols:
            val = meta.get(col)
            if not val:
                continue
            parts.append(f"{_label(col)}: {val}")
        if not parts:
            parts.append(rec.get("text", "").strip())
        lines.append(f"[{idx}] " + " | ".join(parts))
    return "\n".join(lines).strip()


@app.command()
def index(
    csv_path: Path = typer.Argument(..., exists=True, dir_okay=False),
    out_index: Path = typer.Option(Path("results/papers.index.jsonl"), "--out-index"),
    text_cols: str = typer.Option(",".join(DEFAULT_TEXT_COLS), "--text-cols"),
    embed_url: str = typer.Option("http://localhost:8080", "--embed-url"),
    embed_api: str = typer.Option("openai", "--embed-api", case_sensitive=False),
    embed_model: Optional[str] = typer.Option(None, "--embed-model"),
    batch_size: int = typer.Option(8, "--batch-size"),
    limit: Optional[int] = typer.Option(None, "--limit"),
    timeout: float = typer.Option(60.0, "--timeout"),
) -> None:
    """Build an embedding index from a CSV file."""
    cols = _split_cols(text_cols)
    df = pd.read_csv(csv_path)
    if limit:
        df = df.head(limit)

    out_index.parent.mkdir(parents=True, exist_ok=True)
    embed_api = embed_api.lower()

    records: List[RagRecord] = []
    for idx, (_, row) in tqdm(enumerate(df.iterrows()), total=len(df), desc="prepare"):
        meta = _row_to_meta(row)
        text = _build_text(meta, cols)
        if not text:
            continue
        doc_id = f"row_{idx:06d}"
        records.append(RagRecord(doc_id=doc_id, text=text, meta=meta, embedding=[]))

    with out_index.open("w", encoding="utf-8") as f:
        for i in tqdm(range(0, len(records), batch_size), desc="embed"):
            chunk = records[i : i + batch_size]
            texts = [r.text for r in chunk]
            embeddings = _embed_texts(embed_url, texts, embed_api, embed_model, timeout)
            for rec, emb in zip(chunk, embeddings):
                rec.embedding = emb
                line = {
                    "id": rec.doc_id,
                    "text": rec.text,
                    "meta": rec.meta,
                    "embedding": rec.embedding,
                }
                f.write(json.dumps(line, ensure_ascii=True) + "\n")

    typer.echo(f"Wrote index: {out_index}")


@app.command()
def ask(
    index_path: Path = typer.Argument(..., exists=True, dir_okay=False),
    query: str = typer.Argument(...),
    embed_url: str = typer.Option("http://localhost:8080", "--embed-url"),
    embed_api: str = typer.Option("openai", "--embed-api", case_sensitive=False),
    embed_model: Optional[str] = typer.Option(None, "--embed-model"),
    llm_url: str = typer.Option("http://localhost:8080", "--llm-url"),
    llm_api: str = typer.Option("openai-chat", "--llm-api", case_sensitive=False),
    llm_model: Optional[str] = typer.Option(None, "--llm-model"),
    top_k: int = typer.Option(5, "--top-k"),
    context_cols: str = typer.Option(",".join(DEFAULT_CONTEXT_COLS), "--context-cols"),
    max_tokens: int = typer.Option(512, "--max-tokens"),
    temperature: float = typer.Option(0.2, "--temperature"),
    timeout: float = typer.Option(60.0, "--timeout"),
    show_sources: bool = typer.Option(False, "--show-sources"),
) -> None:
    """Answer a question using the indexed CSV (RAG-style)."""
    embed_api = embed_api.lower()
    llm_api = llm_api.lower()
    context_cols_list = _split_cols(context_cols)

    records: List[Dict[str, Any]] = []
    with index_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            emb = rec.get("embedding") or []
            rec["_norm"] = math.sqrt(sum(x * x for x in emb)) if emb else 0.0
            records.append(rec)

    query_emb = _embed_texts(embed_url, [query], embed_api, embed_model, timeout)[0]
    query_norm = math.sqrt(sum(x * x for x in query_emb)) if query_emb else 0.0

    scored: List[Dict[str, Any]] = []
    for rec in records:
        emb = rec.get("embedding") or []
        if not emb or rec["_norm"] == 0.0 or query_norm == 0.0:
            score = -1.0
        else:
            score = _cosine_sim(query_emb, emb)
        scored.append({"score": score, "record": rec})

    top = sorted(scored, key=lambda x: x["score"], reverse=True)[:top_k]
    top_records = [t["record"] for t in top]
    context = _format_context(top_records, context_cols_list)

    system_prompt = (
        "You are a research assistant. Use only the provided context to answer. "
        "If the answer is not in the context, say you do not know. "
        "Cite sources like [1], [2] when relevant."
    )
    user_prompt = f"Question:\n{query}\n\nContext:\n{context}\n\nAnswer:"
    prompt = f"{system_prompt}\n\n{user_prompt}"

    if llm_api == "openai-chat":
        answer = _completion_openai_chat(
            llm_url, system_prompt, user_prompt, llm_model, max_tokens, temperature, timeout
        )
    elif llm_api == "openai":
        answer = _completion_openai(llm_url, prompt, llm_model, max_tokens, temperature, timeout)
    elif llm_api == "legacy":
        answer = _completion_legacy(llm_url, prompt, max_tokens, temperature, timeout)
    else:
        raise ValueError(f"unsupported llm api: {llm_api}")

    if show_sources:
        for idx, item in enumerate(top, start=1):
            meta = item["record"].get("meta", {})
            title = meta.get("title", "")
            typer.echo(f"[{idx}] score={item['score']:.4f} {title}")
        typer.echo("")

    typer.echo(answer)


if __name__ == "__main__":
    app()
