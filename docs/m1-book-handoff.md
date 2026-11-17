# Milestone 1 — Handoff contract to Jupyter Book (Tuấn / Duy)

## Owners and boundaries

- **Duy (EDA):** consolidate per-member M1 statistics, validate ID scopes and
  duplicate keys, produce repeatable plots/interpretations in a **separate**
  notebook (proposed: `notebooks/01_eda_m1.ipynb`). Duy owns EDA methods and
  final analysis findings; the filename can be adjusted at integration.
- **Tuấn (pipeline, book, DevOps):** maintain the Jupyter Book chapters,
  reproducible builds, validation and GitHub Actions; review and integrate
  Duy's cells/output after handoff. Do not rewrite Duy's notebook in parallel.
- **Team:** record missing/failed/not-found instead of fabricating complete data.

This handoff was integrated into **Draft PR #11** on 2026-11-17 from Duy's PR #13 (fix commit `a4af010`). It is a review candidate, **not final source-hash verification or a successful publication claim**.

## Integrated review candidate — 2026-11-17

- Imported `notebooks/01_eda_m1.ipynb`, aggregate CSVs, and `book/_data/m1_summary.json` from PR #13.
- `book/01_m1_crawling.ipynb` calls `book/_m1_report.py` to fail the book build on contradictory/malformed input.
- Scope: **25,000 assigned**, **24,999 collected**; report names the missing ID only when that range is represented.
- Benchmark: **36 batch rows** across five source ranges; summed batch wall-time is **not** a continuous end-to-end measurement.
- Raw references with `ref_status == success` remain *resolved*, not SHA-256 verified; verified count is `null` without per-file hash evidence.
- GitHub Actions must pass, the Duy/TV5 handoff needs leader sign-off, and only then may PR #11 be merged to main and Pages deployed. **PR #11 remains Draft pending review.**


## Data Duy should hand off

1. The list of **five explicitly named arXiv ranges**, responsible member,
   expected ID counts and exact input filenames.
2. The latest per-member `m1_discovery_stats(...).csv`,
   `m1_download_stats(...).csv`, `m1_reference_stats(...).csv` and
   `m1_version_pipeline_benchmark(...).csv` with provenance (run date, code
   commit, CPU-only status, coverage). Preserve original source data.
3. A notebook/script that **concatenates** different members' paper rows
   and **outer-joins** different table types by normalized `arxiv_id`.
   Validate each table's schema, unique ID keys, range boundaries,
   duplicate/overlapping members, and missing or extra IDs *before* computing
   rates. Raise or show explicit warnings rather than silently deduplicating.
4. A reproducible analysis covering: metadata and version distributions,
   TeX/PDF-only/failure categories, reference availability and counts,
   scope/coverage gaps; interpret denominators precisely. If category metadata
   is unavailable, explicitly report that limitation.
5. A small, documented **non-sensitive** build-time input or reproducible
   notebook. No fixed/handwritten chart images or hardcoded findings.
6. For benchmarks: per-run raw logs + aggregation method. Time may be summed
   for sequential, non-overlapping runs; peak RAM is a max, not a sum; average
   RAM needs compatible sampling/weights; disk/storage metrics must define
   whether they are cumulative or per-run (don't sum cumulative snapshots).
   The official CPU-only end-to-end benchmark remains *unverified* unless
   session provenance and measurements actually establish it.

## Book inputs already prepared

The M1 chapter reads `M1_SUMMARY_JSON=/absolute/path/m1_summary.json`,
falling back to `book/_data/m1_summary.json`. The JSON is emitted by
`src/scripts/build_m1_report.py --data-dir ... --export` after a fresh
`src/scripts.audit_m1_integrity` run. It has a **single verified dataset
scope**; **do not** label this summary "whole team" unless the underlying
source scope genuinely covers all member ranges.

`book/_m1_report.py` validates schema v1, numeric coverage and reconciliation
of the source-version statuses before showing graphs. An **absent** file
produces a truthful "waiting for verified input" page; a **malformed**
file fails the build rather than showing fictitious results.

No raw `data/`, `.tex`, `.bib`, archives, `.parquet`, Google Drive tokens,
or API secrets are committed. Team statistics and book-safe aggregated
inputs should be lightweight and reviewed before being added.

## Integration checklist (after Duy delivers)

- [ ] Confirm file names and ID ranges, then validate all five sets.
- [ ] Review EDA formulas, groupby denominators, joins and conclusions.
- [ ] Decide a concrete repo-safe build-time input and wire Duy's code in the
      M1 book chapter. Graphs/tables must be Python-generated at build time.
- [ ] Keep incomplete/failed categories visible; no fabricated 100% coverage.
- [ ] Validate isolated schema tests and the full book build.
- [ ] Verify the `Deploy Jupyter Book` GitHub Action and published page.
- [ ] Merge only after Duy's analysis and reviewer approval.

## Local commands

```bash
python -m pip install -r book/requirements.txt
python -m unittest discover -s book/tests -v
jupyter-book build book --warningiserror
```

To supply an explicitly audited M1 report without committing source data:

```bash
M1_SUMMARY_JSON=/absolute/path/m1_summary.json jupyter-book build book --warningiserror
```

The build alone cannot prove the underlying data's correctness or that an
official CPU-only benchmark was run.
