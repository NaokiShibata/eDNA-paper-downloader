# Strands Decider benchmark labeling guide

This benchmark is intended to measure whether the screening workflow can retain relevant
environmental DNA / environmental RNA literature while safely excluding clearly irrelevant papers.

The reviewer should label `*.review.csv` without looking at Strands Decider predictions or the
sampling manifest. The `*.manifest.csv` file contains the hidden natural/challenge and
calibration/test assignments.

## Primary gold label

Use exactly one of:

- `in_scope`
  - The study actually collects, detects, quantifies, sequences, analyzes, validates, compares,
    or models DNA/RNA obtained directly from an environmental sample or environmental matrix.
  - eDNA/eRNA terminology does not need to appear explicitly.
  - Environmental matrices may include water, seawater, sediment, soil, air, dust, snow, ice,
    wastewater, biofilms, passive samplers, or similar samples.

- `out_of_scope`
  - The study does not actually analyze environmentally obtained DNA/RNA.
  - Examples include tissue/blood/isolated-organism sequencing, ordinary genome/transcriptome
    studies, culture-only work, host-associated microbiome studies with no environmental
    nucleic-acid application, or papers that only mention eDNA/eRNA in the background,
    discussion, comparison, citations, or future work.

- `unsure`
  - The abstract does not contain enough information to make the distinction confidently.
  - Do not infer missing methods.

## Auxiliary gold fields

These are optional but useful for diagnosing why the classifier succeeds or fails.

### gold_actual_use

Use `true`, `false`, or `unsure`.

Question: Does the study actually use DNA/RNA obtained directly from an environmental sample in
its methods or results?

### gold_microbial_only

Use `true`, `false`, or `unsure`.

Question: Is this primarily a conventional microbiome, microbial-community, metagenomic, or
metatranscriptomic study without a meaningful eDNA/eRNA detection, monitoring, sampling,
quantification, validation, or methodological contribution?

Do not mark `true` merely because 16S, 18S, ITS, COI, metabarcoding, or metagenomics terms occur.

### gold_method_relevance

Use `true`, `false`, or `unsure`.

Question: Does the paper evaluate a sampling, preservation, extraction, detection, amplification,
sequencing, quantification, bioinformatic, modeling, or monitoring method that is directly useful
for eDNA/eRNA research?

## Labeling procedure

1. Label the primary `gold_label` first.
2. Fill the three auxiliary fields only after the primary decision.
3. Use only the title and abstract contained in the benchmark file.
4. Do not inspect Strands probabilities while labeling.
5. Use `gold_note` for short explanations in difficult cases.
6. Do not change `benchmark_id` or `benchmark_record_id`.

## Benchmark interpretation

The final test partition should not be used for threshold tuning.

The most important metric for this workflow is `hard_false_negative_rate`: a human `in_scope`
paper that Strands automatically labels `out_of_scope`.

Papers labeled `unsure` by Strands are intended for manual review, so the evaluator also reports
`operational_recall_if_unsure_is_reviewed`. This treats manual-review cases as retained rather
than lost.

The natural subset estimates performance on the source literature distribution. The challenge
subset intentionally over-represents difficult boundary cases and should be interpreted separately.
