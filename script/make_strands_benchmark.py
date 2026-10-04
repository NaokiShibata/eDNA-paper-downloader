from __future__ import annotations

import hashlib
import random
import re
from pathlib import Path

import pandas as pd
import typer

from libs.text_normalize import clean_doi

app = typer.Typer(add_completion=False)

GOLD_COLUMNS = [
    "gold_label",
    "gold_actual_use",
    "gold_microbial_only",
    "gold_method_relevance",
    "gold_note",
]

STRATUM_ORDER = [
    "environmental_nonexplicit",
    "microbial_profile",
    "host_tissue_genomics",
    "wastewater",
    "methodological",
    "review_perspective",
    "explicit_edna",
    "other",
]


def _row_to_record_id(row: pd.Series) -> str:
    doi = clean_doi(str(row.get("doi", "") or ""))
    if doi:
        return f"doi:{doi}"
    title = str(row.get("title", "") or "").strip().lower()
    year = str(row.get("year", "") or "").strip()
    if title or year:
        return f"title:{title}|year:{year}"
    return f"row:{row.name}"


def _contains(pattern: str, text: str) -> bool:
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def _classify_row(row: pd.Series, abstract_column: str) -> tuple[str, str, int]:
    title = str(row.get("title", "") or "")
    abstract = str(row.get(abstract_column, "") or "")
    text = f"{title}\n{abstract}".lower()

    explicit = _contains(
        r"\b(?:environmental\s+(?:dna|rna)|e[- ]?dna|e[- ]?rna)\b",
        text,
    )
    microbial = _contains(
        r"microbiom|microbiota|metagenom|metatranscript|\b16s\b|\b18s\b|\bits\b|"
        r"bacteri(?:a|al)|archaea|mycobiom|virom|resistom|fungal community|microbial community",
        text,
    )
    host_tissue = _contains(
        r"\btissue\b|\bblood\b|\bserum\b|\bplasma\b|\bbiopsy\b|"
        r"host-associated|gut microbi|skin microbi|oral microbi|rumen microbi|"
        r"whole[- ]genome|genome assembly|transcriptom",
        text,
    )
    wastewater = _contains(r"wastewater|sewage|sewer|waste water", text)
    environmental = _contains(
        r"\bwater\b|seawater|freshwater|river|lake|stream|pond|ocean|marine|estuar|"
        r"sediment|\bsoil\b|\bair\b|airborne|dust|snow|ice|wetland|groundwater|biofilm|"
        r"wastewater|sewage|sewer",
        text,
    )
    methodological = _contains(
        r"sampl(?:e|ing|er)|filter|filtration|preserv|extract|primer|assay|\bqpcr\b|"
        r"\bddpcr\b|\blamp\b|metabarcod|amplicon|bioinformatic|occupancy|transport|"
        r"degradation|persistence|quantif|validation|benchmark|workflow",
        text,
    )
    review = _contains(
        r"systematic review|meta-analysis|\breview\b|perspective|overview|"
        r"state of the art|future directions",
        text,
    )

    tags: list[str] = []
    if explicit:
        tags.append("explicit_edna")
    if environmental:
        tags.append("environmental_matrix")
    if microbial:
        tags.append("microbial")
    if host_tissue:
        tags.append("host_tissue")
    if wastewater:
        tags.append("wastewater")
    if methodological:
        tags.append("methodological")
    if review:
        tags.append("review_perspective")

    if environmental and not explicit:
        stratum = "environmental_nonexplicit"
    elif microbial:
        stratum = "microbial_profile"
    elif host_tissue:
        stratum = "host_tissue_genomics"
    elif wastewater:
        stratum = "wastewater"
    elif methodological:
        stratum = "methodological"
    elif review:
        stratum = "review_perspective"
    elif explicit:
        stratum = "explicit_edna"
    else:
        stratum = "other"

    score = 0
    if environmental and not explicit:
        score += 4
    if microbial:
        score += 3
    if host_tissue:
        score += 3
    if wastewater:
        score += 2
    if methodological:
        score += 2
    if review:
        score += 2
    if explicit and (microbial or review or host_tissue):
        score += 2
    if len(abstract) < 500:
        score += 1
    if len(abstract) > 2500:
        score += 1

    return stratum, ";".join(tags), score


def _partition(record_id: str, seed: int, calibration_fraction: float) -> str:
    digest = hashlib.sha256(f"{seed}|{record_id}".encode()).digest()
    value = int.from_bytes(digest[:8], "big") / float(2**64)
    return "calibration" if value < calibration_fraction else "test"


def _round_robin_challenge(
    pool: pd.DataFrame,
    n: int,
    seed: int,
) -> pd.DataFrame:
    if n <= 0 or pool.empty:
        return pool.iloc[0:0].copy()

    rng = random.Random(seed)
    buckets: dict[str, list[int]] = {}
    for stratum in STRATUM_ORDER:
        group = pool.loc[pool["_benchmark_stratum"] == stratum].copy()
        if group.empty:
            continue
        # High challenge score first, random tie-breaker.
        keyed = [
            (-int(row["_challenge_score"]), rng.random(), int(idx))
            for idx, row in group.iterrows()
        ]
        keyed.sort()
        buckets[stratum] = [idx for _, _, idx in keyed]

    selected: list[int] = []
    while len(selected) < n and any(buckets.values()):
        progressed = False
        for stratum in STRATUM_ORDER:
            values = buckets.get(stratum, [])
            if values and len(selected) < n:
                selected.append(values.pop(0))
                progressed = True
        if not progressed:
            break

    if len(selected) < n:
        remaining = [int(i) for i in pool.index if int(i) not in set(selected)]
        rng.shuffle(remaining)
        selected.extend(remaining[: n - len(selected)])

    return pool.loc[selected].copy()


@app.command()
def make(
    input_csv: Path = typer.Argument(..., exists=True, dir_okay=False),
    out_prefix: Path = typer.Option(
        Path("benchmark/strands_edna_200"),
        "--out-prefix",
        help="Writes <prefix>.review.csv and <prefix>.manifest.csv.",
    ),
    abstract_column: str = typer.Option("abstract", "--abstract-column"),
    n_total: int = typer.Option(200, "--n-total", min=20),
    n_natural: int = typer.Option(120, "--n-natural", min=1),
    calibration_fraction: float = typer.Option(0.40, "--calibration-fraction", min=0.1, max=0.9),
    seed: int = typer.Option(3407, "--seed"),
) -> None:
    """Create a blinded, stratified benchmark set from a literature CSV."""
    if n_natural >= n_total:
        raise typer.BadParameter("--n-natural must be smaller than --n-total")

    df = pd.read_csv(input_csv, dtype=str, keep_default_na=False)
    if abstract_column not in df.columns:
        raise typer.BadParameter(
            f"abstract column {abstract_column!r} was not found; "
            f"available columns: {', '.join(df.columns)}"
        )

    df = df.copy()
    df["benchmark_record_id"] = df.apply(_row_to_record_id, axis=1)
    df = df.loc[df[abstract_column].str.strip().ne("")].copy()
    df = df.drop_duplicates(subset=["benchmark_record_id"], keep="first").reset_index(drop=True)

    if len(df) < n_total:
        raise typer.BadParameter(
            f"only {len(df)} unique rows with abstracts are available; "
            f"reduce --n-total from {n_total}"
        )

    classified = df.apply(
        lambda row: _classify_row(row, abstract_column),
        axis=1,
        result_type="expand",
    )
    classified.columns = ["_benchmark_stratum", "_benchmark_tags", "_challenge_score"]
    df = pd.concat([df, classified], axis=1)

    natural = df.sample(n=n_natural, random_state=seed).copy()
    natural["_benchmark_source"] = "natural"

    challenge_n = n_total - n_natural
    remaining = df.drop(index=natural.index)
    challenge = _round_robin_challenge(remaining, challenge_n, seed + 1)
    challenge["_benchmark_source"] = "challenge"

    benchmark = pd.concat([natural, challenge], axis=0).copy()
    benchmark["_sort_key"] = benchmark["benchmark_record_id"].map(
        lambda rid: hashlib.sha256(f"order|{seed}|{rid}".encode()).hexdigest()
    )
    benchmark = benchmark.sort_values("_sort_key").drop(columns=["_sort_key"]).reset_index(drop=True)

    benchmark["benchmark_id"] = [f"B{i:04d}" for i in range(1, len(benchmark) + 1)]
    benchmark["_benchmark_partition"] = benchmark["benchmark_record_id"].map(
        lambda rid: _partition(rid, seed, calibration_fraction)
    )

    for col in GOLD_COLUMNS:
        benchmark[col] = ""

    # Reviewer file deliberately excludes sampling strata and partition to reduce labeling bias.
    helper_cols = {
        "_benchmark_stratum",
        "_benchmark_tags",
        "_challenge_score",
        "_benchmark_source",
        "_benchmark_partition",
    }
    original_cols = [c for c in df.columns if c not in helper_cols and c != "benchmark_record_id"]
    review_cols = ["benchmark_id", "benchmark_record_id"] + original_cols + GOLD_COLUMNS
    review = benchmark[review_cols].copy()

    manifest = benchmark[
        [
            "benchmark_id",
            "benchmark_record_id",
            "_benchmark_source",
            "_benchmark_stratum",
            "_benchmark_tags",
            "_benchmark_partition",
            "_challenge_score",
        ]
    ].rename(
        columns={
            "_benchmark_source": "benchmark_source",
            "_benchmark_stratum": "benchmark_stratum",
            "_benchmark_tags": "benchmark_tags",
            "_benchmark_partition": "benchmark_partition",
            "_challenge_score": "challenge_score",
        }
    )

    out_prefix.parent.mkdir(parents=True, exist_ok=True)
    review_path = Path(f"{out_prefix}.review.csv")
    manifest_path = Path(f"{out_prefix}.manifest.csv")
    review.to_csv(review_path, index=False)
    manifest.to_csv(manifest_path, index=False)

    typer.echo(f"Input unique rows : {len(df)}")
    typer.echo(f"Benchmark rows    : {len(benchmark)}")
    typer.echo(f"  natural         : {(manifest['benchmark_source'] == 'natural').sum()}")
    typer.echo(f"  challenge       : {(manifest['benchmark_source'] == 'challenge').sum()}")
    typer.echo(f"  calibration     : {(manifest['benchmark_partition'] == 'calibration').sum()}")
    typer.echo(f"  test            : {(manifest['benchmark_partition'] == 'test').sum()}")
    typer.echo("Challenge strata:")
    for name, count in manifest.loc[
        manifest["benchmark_source"] == "challenge", "benchmark_stratum"
    ].value_counts().items():
        typer.echo(f"  {name:24s} {count}")
    typer.echo(f"Reviewer CSV      : {review_path}")
    typer.echo(f"Manifest CSV      : {manifest_path}")


if __name__ == "__main__":
    app()
