# Milestone 1 data contract (incremental, append-only outputs)

This document describes **new outputs**. It does not modify the existing dataset.

## Existing publication-level outputs
- `data/papers.jsonl`: one record per canonical publication ID, including `versions`. Preserve existing fields.
- `data/references.jsonl`: deduplicated arXiv-to-arXiv edges with existing published schema.
- `data/csv/m1_download_stats.csv`: existing per-publication statistics; preserve compatibility.

## Version-aware source outputs (incrementally implemented)
- `data/versions_manifest.jsonl`: append-only **attempt history**; the last event per `(arxiv_id, version)` is used when reconciling. It is not a current-state snapshot. Required keys: `arxiv_id` (canonical e.g. `2501_12345`), `version` (e.g. `v2`), `status` (one of `pending`, `success`, `pdf_only`, `not_found`, `failed`), `source_url`, `raw_path`, `raw_size_bytes`, `cleaned_size_bytes`, `num_tex_files`, `error`, `fetched_at`.
- `data/raw/<canonical_id>/vN/`: the extracted nonempty `.tex` and `.bib` source for the version; `.version-status.json` contains hashes and source metadata. A PDF-only version has an empty file list with the same marker.
- `data/csv/m1_download_versions.csv`: generated **only by an explicit full-dataset export** in notebook cell 2.3 / `python -m src.scripts.audit_version_downloads --data-dir ... --export`. One row per expected `(arxiv_id, version)`, containing status, verification, marker/manifest status, sizes, TeX count, and attempt timing. Read-only by default; CSV is a snapshot and must be refreshed after later downloads.
- Audit-only states `conflict` (existing untrusted vN) and `missing_files` (manifest says success but files absent) are separate from download attempt states `pending`, `success`, `pdf_only`, `not_found`, `failed`. An old unversioned source does not count as verified for any `vN`.
- The version audit hashes only the expected `vN` paths and never modifies the legacy raw files, the published JSONLs, or the original CSVs. On large Drive datasets, full hash verification is slower than a lightweight metadata-only audit.
- A paper's version count is **expected work**, not evidence that all source versions downloaded successfully.

## New reference-retention output (planned for Issue #3)
- `data/references_raw.jsonl`: all Semantic Scholar reference entries with `arxiv_id` (citing publication), `ref_index` (stable citation position if available), `ref_s2_id`, `ref_arxiv_id` (empty when unavailable), `ref_title`, `ref_authors`, `ref_submission_date`, `external_ids`, `retrieved_at`, `source_endpoint`, and `raw_cited_paper` when available. Preserve missing-identifier entries; do not silently discard.
- Only references with valid `ref_arxiv_id` go into the published `references.jsonl` schema.
- Distinguish API transport success, missing metadata, unresolved references, and pagination completeness. A successful batch response is not proof that pagination is complete.

## Critical compatibility and safety rules
1. Preserve the original `data/raw/<id>/` until a verified migration or source version is independently identified.
2. Network arXiv IDs use `2501.12345v2`, while filesystem publication IDs use `2501_12345`; keep the version separately.
3. Recovery is keyed by `(publication ID, version)` for sources and by citation identity for raw references; repeat runs must be idempotent.
4. Never commit live `data/` or credentials to Git. Store all new crawl outputs separately until verified.
5. Do not overwrite the existing source directories, published JSONL or Drive data while integrating the incremental pipeline. Validate on an isolated small dataset first.
6. Current baseline (2026-11-01): 5,000 valid metadata records; 8,587 expected versions; 2,309 multi-version publications; 84,061 published arXiv edges; 3,235 out-of-scope not-found discovery records preserved in history.

## Incremental-first migration
- Full raw-data backups are optional and are not a prerequisite for read-only audits or new-version downloads.
- Preserve old files; write version-aware outputs to new paths and publish only after consistency checks.
- Drive Fast Scratch synchronization must be audited before any production writes to prevent overwriting newer persistent data.

## Minimum audit invariants
- Canonical publication IDs are unique in latest snapshots.
- The set of source-version keys has no duplicates.
- Discovery successful IDs and usable metadata agree.
- The sum of version counts over successful metadata equals the declared expected worklist.
- Published edges contain two valid IDs and no duplicate pairs.
- Download stats can be reconciled with source directories; full recursive raw verification must be performed after a trusted tree export or on mounted Drive.

## Source-version reconciliation (step 2)
- `audit_version_downloads(data_dir)` produces a **read-only** full-dataset report, one row per expected version from valid metadata. The verified filesystem marker and SHA-256 hashes override stale manifest entries.
- `export_version_csv(report)` atomically writes **only** `csv/m1_download_versions.csv`; it never truncates the table to the last monthly `id_list` because the report intentionally has no ID-range filter.
- `verified_success + verified_pdf_only + not_found + failed + pending + conflicts + missing_files == expected_versions`. Nonzero `conflicts`, `missing_files`, invalid version metadata or orphan manifest keys must be reviewed before declaring completion.
- No automatic export is triggered by each downloader batch: after a run, rerun notebook cell 2.3 (or the CLI) to refresh the CSV.
