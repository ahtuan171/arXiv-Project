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

## Raw reference-retention output (new opt-in collector)
- `data/references_raw.jsonl`: append-only, one entry for each original API reference position, with `arxiv_id` (citing publication), `ref_index` (stable citation position if available), `ref_s2_id`, `ref_arxiv_id` (empty when unavailable), `ref_title`, `ref_authors`, `ref_submission_date`, `external_ids`, `retrieved_at`, `source_endpoint`, and `raw_cited_paper` when available. Preserve missing-identifier entries; do not silently discard.
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

## Step 3: read-only legacy source comparison (no automatic deduplication)
- Run notebook cell 2.4 or `python -m src.scripts.audit_legacy_matches --data-dir /path/to/data --max-papers 20`. This hashes only explicitly selected legacy directories and verified `vN` files. The default 20-paper cap avoids accidental whole-Drive scans.
- Matching requires the identical **complete list of relative .tex/.bib paths, sizes and SHA-256 hashes**. Only vN directories already trusted by `.version-status.json` are compared. The legacy status marker is ignored as content; unexpected legacy files and symlinks fail closed.
- `exact_match` proves byte-equivalent extracted source, **not** that the legacy unversioned request fetched that historical revision. `ambiguous_match` means identical content across multiple revisions; `no_verified_version` means there is no trusted comparison.
- Legacy files never count toward `verified_existing`, are never moved/rewritten, and this audit does not suppress or prefill version download attempts. Avoiding a first versioned request needs independent version provenance; an unversioned historical URL cannot supply it.
- No migration or deletion is performed at this stage. Phase 4 will address per-version metadata and citations.

## Step 4a: version-level metadata index (no fabricated revision dates)
- Cell 2.5 / `python -m src.scripts.build_version_metadata --data-dir ...` builds an in-memory index for **all** expected versions across both months. `--export` creates or atomically refreshes only `data/metadata_versions.jsonl`.
- Source is existing `papers.jsonl` plus any journal; the file contains `(arxiv_id, version)`, publication metadata reference, paper-level submission date, optional version date, and explicit date provenance.
- The present crawler captures only the most recent revised date for most multi-version papers. Assign that single date **only to the last version**; leave dates of intermediate revisions blank. A complete `n-1` ordered revised date list may be mapped positionally.
- **Do not copy the latest paper title, abstract, or author list to earlier historical revisions** as if they were verified. This index is not a substitute for fetching version-specific metadata and is not a claim that all revision history is complete.
- Notebook progress bars in 2.3/2.4 report audit work processed, not source download success or legacy-match rate. Git operations in cell 1 use local `/content/arxiv-code`; persistent dataset remains on Drive.

## Step 4b: raw references collector (opt-in, not a migration)
- Notebook cell 2.6 runs `collect_raw_references(..., dry_run=True, max_papers=3)` by default. The new API collector follows **paginated GET /paper/arXiv:{id}/references**, retains original reference entries (including unresolved/null citedPaper), never filters out non-arXiv references, and uses the existing S2 rate-limit/retry transport.
- New append-only outputs `data/references_raw.jsonl` (original entry and normalized convenience fields, with zero-based `ref_index`) and `data/references_raw_ledger.jsonl` (per-publication success/error/not-found, raw count, SHA-256, completeness). The raw file is not written until all pages are fetched. A crash between raw append and the ledger is recovered by comparing existing (paper,index) payloads and appending only missing positions; conflicting/duplicate raw keys fail closed. One writer per Drive dataset.
- Legacy `data/references.jsonl` remains the published arXiv-to-arXiv edge file; this raw collector **does not** rebuild or change it. Existing discarded non-arXiv citations must be re-fetched, they cannot be reconstructed from published edges.
- S2's paper-level reference response cannot establish which historical arXiv revision contained a citation. `version=null` and `version_attribution=unknown_s2_publication_level` are deliberate; never claim version-specific citations from this output.
- Incomplete/ambiguous pagination yields an error and does **not** publish partial raw references. A short final page without API `total` is reported as `inferred_short_page`, not verified full count. Test 1–3 papers before increasing the limit; this is independent of the disabled legacy crawler.

## Step 6: integrated read-only M1 integrity audit
- Cell 2.7 / `python -m src.scripts.audit_m1_integrity --data-dir ...` scans the full dataset, not the monthly ID window. SHA-256 source verification is performed by the existing version audit.
- Checks paper ID uniqueness, metadata-version keys, source markers and hashes, published citation edge validity/uniqueness, optional exported version snapshot freshness, raw-reference provenance and ledger count/SHA-256 consistency. Raw/published citation overlap is informational, NOT an equality requirement.
- Status `needs_fix` indicates conflicting or untrusted data. `in_progress` indicates expected missing work including unverified raw references or missing snapshot exports. `structurally_consistent` only confirms checked structure; it cannot assert complete historical revision dates or version-level citations.
- The optional `--export` / `EXPORT_M1_INTEGRITY_REPORT=True` atomically writes ONLY `data/reports/m1_integrity_report.json`; all published and raw data remain unchanged. A full audit is I/O intensive on Drive; run it without parallel writers.

## Step 7: offline integration and Colab smoke check
- The branch includes `.github/workflows/m1-offline-tests.yml` (commit fb0c740), running isolated pytest modules on GitHub Actions at each push/PR. These tests use local temporary fixtures, not real Drive data or external network.
- Colab cell **2.8** runs exactly the scoped offline regression suite with `subprocess.run(check=True)` so failures are visible; pytest plugin autoload and cache are disabled to reduce incidental effects. This does NOT replace real Colab execution of 2.2–2.7.
- Recommended flow: run environment/config, keep downloader and raw references in dry-run, execute 2.1–2.7, execute 2.8, then (only after reviewing logs) enable bounded 10-version and 3-paper network tests. Compare integrity before and after; never delete or overwrite older Drive data.
- Missing/failed CI execution cannot be reported as passed. Do not declare M1 accepted until the workflow and Colab smoke run results have actually been inspected.

## Step 8: reporting and evidence boundaries
- Cell 2.9 / `python -m src.scripts.build_m1_report --data-dir ...` builds a **read-only by default** summary. Optional export writes only `data/reports/m1_summary.json`. Cell 2.9 consumes the fresh `integrity_report` from 2.7 and the verified CSV from 2.3; rerun 2.7 after downloads before generating report.
- Source coverage counts only verified vN marker/hash outcomes. Detailed sizes and complete-paper counts are calculated only if `m1_download_versions.csv` is currently verified and not stale. Compressed archive bytes and extracted TeX/Bib bytes are labelled as different measures, NOT uncompressed before/after figures.
- Raw reference averages include only hash-verified S2 publication-level papers; missing samples yield `null`, not zero. Old discovery/reference statistics are labelled legacy.
- Legacy `m1_system_benchmark.csv` cannot substantiate CPU-only performance of the new version-aware workflow. The report intentionally leaves official end-to-end runtime/peak RAM/mean RAM/peak disk/final storage and session provenance as missing evidence until measured on Colab.
- `book/01_m1_crawling.ipynb` is now a dynamic **draft chapter**: build-time tables/plots use only a supplied `M1_SUMMARY_JSON`; with no data it visibly reports missing evidence (no fabricated 97/3 chart). **Course requires a separate public Jupyter Book repository**, so move/copy the report to the appropriate dedicated repository before official submission. Do not commit private raw data into the source-code repo.

## Seven-cell Colab pipeline (historical snapshot)
- The seven-cell layout below describes an earlier pipeline revision. The current pipeline has 12 code cells (see the update below). The course requires the Jupyter Book report to be published in a separate public repository; `book/` in this repository is a development draft, not the final submission destination.
- In that earlier revision, `src/milestone1_pipeline.ipynb` contained seven code cells: Setup, Config, Preflight, Version Download, Raw References, Validation and Summary. Defaults disabled API writes and snapshot exports.
- Legacy comparison and old four CSV inspection move to notebooks/m1_legacy_debug.ipynb within the same repo, read-only.
- New optional version_report and metadata_report parameters on audit_m1_integrity reuse full-dataset reports. Cell 6 hashes each vN file once; rerun validation after any data changes. Summary in cell 7 uses current integrity result.
- Historical references to notebook cell 2.1-2.9 were superseded by the seven-cell layout, which was later replaced by the 12-cell layout below.

## Presentation matched to main (Vietnamese)
- This section records the earlier **7-code-cell** presentation. The current notebook has 12 code cells, as described below.
- Legacy CSVs appear for read-only comparison, explicitly labelled as legacy; source-vN CSV is separate and based on verified files.
- Keep the public code repository and the public Jupyter Book repository separate for official submission, as required by the course statement.

## Pipeline notebook update (2026-11-01)
- The earlier seven-cell layout and historical cell 2.1–2.9 references above describe previous revisions. The primary notebook now has 12 code cells and starts from a selected arXiv ID range.
- Setup is separated into Drive/code checkout, requirements installation and optional S2 Secret retrieval. It no longer requires `papers.jsonl` before discovery.
- Cell 6 crawls metadata with resume and creates `papers.jsonl`; cell 7 audits the discovered versions; cells 8–10 download, collect references and validate; cell 11 ends an optional run-scoped benchmark; cell 12 reports.
- Metadata crawling is enabled by default for the selected range. Source downloads, S2 collection and exports remain disabled by default. Benchmark output is separate from legacy `m1_system_benchmark.csv`.
