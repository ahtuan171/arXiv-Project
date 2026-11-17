# Milestone 2 — Integration boundaries (TV5: Tuấn)

This is an architecture preparation note, not a claim that the M2 pipeline
has been implemented or tested on production arXiv source.

## Inputs and owners

- Đắc Anh: LaTeX parsing from version-aware raw TeX/Bib source;
  see open PR #10 (`src/parser_module/`).
- Hào: BibTeX parsing and citation normalization;
  see open PR #9 (`src/parsing_module/`).
- Toàn: content/BibTeX deduplication and hierarchy remapping; a compatible
  API/PR has not yet been verified on `main`.
- Tuấn: CPU-only Colab integration, bounded parallel workers, batch flushing,
  measurements and book/CI.
- Duy: EDA after processed data and stats are available.

## Contract to finalize before coding `m2_pipeline.ipynb`

Agree on **one publication-level call contract** across modules before
parallelizing. Proposed normalized input:
`(arxiv_id, [vN source paths], status/metadata)`.
Proposed output per paper:
`elements`, `hierarchy`, `bibtex`, plus three stats rows and errors.
The contract must specify schema, keys, deterministic ordering, partial
failures, PDF-only papers and citation provenance. Do **not** assume
PR #9 and #10 already expose compatible APIs.

Required target Parquet tables:
`elements(element_id, arxiv_id, kind, content)`;
`hierarchy(arxiv_id, version, element_id, parent_id)`;
`bibtex(arxiv_id, citation_key, entry_type, title, authors, year, doi, journal, url)`.
Cross-version dedup must not create dangling hierarchy parents.

## Proposed processing design

1. Stage raw data on Colab **local SSD**, never have workers recursively
   traverse Drive. Snapshot input provenance (commit, range, hashes, CPU-only).
2. Run parser + BibTeX extractor + deduplicator per paper in one
   `ProcessPoolExecutor` task, allowing partial failures to be reported.
   Workers should not write shared output paths.
3. Parent process validates output schema and version provenance, then writes
   **bounded-size** Parquet shards to a staging directory.
4. Persist checkpoints/manifest after each committed batch so an interrupted
   run can resume without duplicate rows. Only publish complete shards;
   do not overwrite successful earlier output.
5. Record the four M2 statistics tables and CPU-only timing/RAM/disk, with
   process-tree measurements and exact session scope; separate peak from sum.
6. Run small synthetic multi-version/PDF-only/retry tests and a small real
   smoke dataset. Scale up only when the schemas and tests pass.
7. Duy's `02_eda_m2.ipynb` remains independent and provides EDA inputs/
   computations for the second book chapter once the processed shards exist.

## Waiting gates

- [ ] PR #9 normalized output contract reviewed and merged.
- [ ] PR #10 parse/hierarchy contract reviewed and merged.
- [ ] Toàn's dedup API delivered, including hierarchy re-parenting rules.
- [ ] Sample multi-version paper and PDF-only case in a shared test fixture.
- [ ] Approved M2 schemas and CSV column types.
- [ ] Benchmarks produced from real Colab CPU-only runs.

This note intentionally does not write `m2_pipeline.ipynb` prematurely,
which would hard-code guesses about APIs owned by other members.
