tiếp tục thực hiện task trước:
TASK: Fix all identified issues in the arXiv reference crawler
1. Role and objective
Act as a senior Python engineer responsible for data-pipeline reliability, API integration, metadata normalization, and automated testing.
Repository:
[https://github.com/ahtuan171/arXiv-Project](https://github.com/ahtuan171/arXiv-Project)
Working branch:
craw-metadata-ref
Your task is to inspect, implement, test, and verify fixes for every issue listed below. Do not stop after writing a review or proposing code snippets. Make the necessary changes in the repository and report the actual results.
The current project crawls Semantic Scholar references for arXiv papers and produces reference-edge datasets.
Previously observed behavior:
The crawler uses Semantic Scholar's /paper/{paperId}/references endpoint.
The response contains nested citedPaper objects.
Direct metadata extraction was introduced to reduce API calls and avoid unnecessary /paper/batch requests.
API rate limiting and retry logic already exist.
A prior test run reported 75 passing tests, but all tests must be rerun against the current code.
Some API entries had citedPaper.paperId=None and externalIds=None.
A crawl of 20 papers completed without fatal errors, but unresolved references could be silently discarded and aggregate statistics were not sufficiently informative.
Treat these observations as leads, not as proof that the current code is unchanged. Inspect the actual branch first.
2. Non-negotiable constraints
Preserve the existing project architecture unless a change is necessary to fix a demonstrated problem.
Preserve the current dataset schema and downstream compatibility.
Do not change the mapping ref_submission_date = ref.publication_date or "". This mapping is an explicit project requirement.
Never invent an arXiv ID from a paper title, author, or year.
Do not silently discard unresolved references without recording their counts and, where feasible, retaining enough information for diagnosis or later recovery.
Do not increase concurrency or relax API rate limits merely to improve throughput.
Do not make real API calls in ordinary unit tests. Mock HTTP responses and use deterministic fixtures.
Do not overwrite existing data or delete crawl progress files to make tests pass.
Avoid unrelated refactoring and new dependencies unless clearly justified.
Do not claim a test, benchmark, or live crawl succeeded unless it was actually executed and its result observed.
3. Phase A — Inspect the current implementation
Inspect at least:
src/s2_api_client.py
src/crawler.py
src/config.py
scripts/crawl_references.py
tests/test_s2_api_client.py
Other tests and data-model/schema modules used by the crawler.
Trace one reference through the entire pipeline:
API response → parsing → metadata cache → fallback lookup → arXiv ID normalization → edge creation → output serialization → ledger/statistics.
Identify the exact functions and code paths responsible for each issue below before editing.
4. Phase B — Fix missing identifiers and unresolved references
Problem B1: References without paperId are discarded too early
The previous review found code that skips a cited paper when citedPaper.paperId is absent, before checking whether its externalIds contain a usable arXiv identifier.
Implement the following behavior:
If paperId is present, process the reference normally.
If paperId is missing but externalIds.ArXiv contains a valid arXiv ID, normalize and retain the reference if the existing schema and edge model allow it.
If paperId is missing but another supported identifier permits a valid lookup, use that lookup only if the existing API integration supports it safely.
If the reference cannot be identified reliably, classify it as unresolved rather than fabricating an identifier.
Preserve diagnostic information where practical, such as the source paper ID, reference title, year, available external IDs, and reason for failure. Do not introduce malformed rows into the canonical reference-edge dataset just to preserve raw metadata.
Ensure unresolved entries cannot be mistaken for valid arXiv reference edges.
Do not infer arXiv IDs from titles.
Problem B2: Partial metadata may prevent useful batch fallback
Inspect the current _ref_cache and batch-fallback logic.
Previously, a cited-paper record could be cached when it contained a title or externalIds, even if useful fields such as authors, year, publication date, or a reliable identifier were missing. If fallback selection checks only whether a paper ID exists in the cache, incomplete cached records may never receive a fallback lookup.
Implement a clear completeness policy:
Distinguish between an absent record, a partial record, and a sufficiently complete record.
Define which fields are required for a record to be considered complete, based on actual downstream requirements and API behavior.
Do not treat a title alone as proof that metadata is complete.
Select partial records for fallback only when the fallback endpoint can plausibly improve them.
Merge fallback metadata into existing records without overwriting valid values with None, empty strings, or empty collections.
Preserve valid direct-response metadata when the fallback response is missing fields.
Avoid redundant requests for records already sufficiently complete.
If the API cannot supply a missing field, retain the best available record and expose its partial status through appropriate diagnostics.
Do not automatically require every optional field to be present. The completeness policy must be realistic and documented.
5. Phase C — Correct metrics and unresolved-reference accounting
Problem C1: refs_found can undercount API references
Previously, entries without paperId were filtered inside the API client before reaching the crawler. Consequently, the crawler's reference count could represent successfully parsed records rather than the number of reference entries actually received from the API.
Introduce or correct metrics so that they distinguish at least:
raw_refs_received: reference entries returned by the API before filtering.
refs_with_paper_id: entries with a usable Semantic Scholar paper ID.
refs_with_arxiv_id: entries that can be mapped to a valid arXiv ID.
refs_unresolved: entries that cannot be resolved to a valid target identifier.
edges_written: valid reference edges actually persisted.
fallback_requests: number of metadata fallback requests, if applicable.
api_calls: actual HTTP attempts, including retries if that matches the project's documented convention.
rate_limited: number of HTTP 429 responses, with semantics clearly documented.
Requirements:
Inspect existing metric names and maintain backward compatibility where reasonable.
Do not count the same reference multiple times merely because it appears in multiple processing stages.
Distinguish API entries from deduplicated references and persisted edges.
Define whether counts are per paper, per API response, or cumulative for a crawl run.
Make metric semantics explicit in code comments or documentation.
Update the CLI summary and statistics outputs consistently.
If pagination exists, count raw entries across all successfully retrieved pages without double-counting retry attempts as additional reference entries.
Ensure failed API requests do not get reported as successful empty responses.
Do not silently change the meaning of an existing metric while keeping its old name. If a legacy metric is retained, document its exact meaning.
6. Phase D — Clarify ledger status and incomplete crawls
Problem D1: Ledger status ok may hide unresolved metadata
Inspect how process_one() and the ledger decide whether a paper is complete.
An API request may succeed even when some references cannot be resolved. This creates two distinct concepts:
The crawl operation completed successfully.
All reference metadata was resolved successfully.
Do not conflate them.
Implement an explicit and backward-compatible status policy. Depending on the current schema, this may use a separate completeness field, an unresolved count, or a documented partial status.
Requirements:
Preserve the distinction between transport/API failure and successful crawling with partial metadata.
Store unresolved counts and other useful per-paper metrics in the ledger where feasible.
Ensure existing resume logic continues to work.
Do not create an infinite recrawl loop for papers whose missing metadata is unavailable from the provider.
Define how a future rerun handles previously partial records and whether an explicit retry policy is needed.
Preserve existing ledger compatibility or implement a safe migration/defaulting strategy.
Add tests for successful complete, successful partial, API failure, and resumed processing.
The meaning of ok, partial, and error must be documented and consistent across the crawler, ledger, and CLI.
7. Phase E — Review retry and rate-limit behavior
Inspect _request() and the shared API limiter.
Previously observed behavior included HTTP 429 responses that were eventually retried successfully. The current implementation appears to use backoff and jitter, but this must be verified against the actual code.
Requirements:
Respect Retry-After when provided.
Support both valid delay-seconds and HTTP-date forms of Retry-After, if compatible with the HTTP library and current design.
Apply bounded exponential backoff and jitter when appropriate.
Retry only appropriate transient failures, such as 429 and selected 5xx responses.
Do not retry permanent failures indefinitely.
Ensure retry exhaustion returns or raises a well-defined failure that the crawler and ledger can distinguish from an empty reference list.
Keep request pacing coordinated across requests and workers; avoid a burst of requests after a retry.
Record retry attempts and rate-limit events accurately.
Add deterministic tests for retry success, retry exhaustion, Retry-After in both formats, and permanent errors.
Do not make the crawler faster by removing backoff or increasing request concurrency while rate limits are still observed.
If the existing code already satisfies a requirement, retain it and add tests or documentation where necessary instead of rewriting it unnecessarily.
8. Phase F — Preserve data normalization and schema correctness
Verify and test all of the following:
arXiv identifiers are normalized consistently.
arXiv: prefixes and version suffixes are handled according to existing project rules.
Invalid or missing identifiers do not create canonical reference edges.
Duplicate edges are handled consistently across pages and repeated API responses.
Missing optional metadata does not corrupt valid records.
Author, year, and publication-date parsing remains compatible with existing output.
The field mapping ref_submission_date = ref.publication_date or "" remains unchanged.
Existing output column names and types remain stable unless an explicitly justified schema migration is implemented.
The current direct-metadata optimization remains in place where it is correct; do not reintroduce unnecessary batch requests.
The resulting parquet/JSONL outputs remain compatible with downstream dataset creation.
Inspect the actual schema rather than assuming the field names or types.
9. Phase G — Add regression tests
Add focused tests covering every issue.
At minimum, include these cases:
Reference parsing and identifiers
paperId exists and complete metadata is returned.
paperId is missing but externalIds.ArXiv is valid.
paperId is missing and no reliable identifier is available.
Title-only record is classified as partial/unresolved, not falsely complete.
Invalid arXiv ID does not create an edge.
arXiv prefix and version normalization work correctly.
Duplicate references across pages do not create unintended duplicate edges.
Metadata fallback
Complete direct metadata avoids unnecessary batch lookup.
Partial direct metadata triggers fallback when useful.
Fallback fills missing fields.
Fallback None values do not overwrite valid direct metadata.
Failed fallback retains the best available partial record.
No redundant fallback requests occur for complete records.
Metrics and ledger
Raw entries are counted before filtering.
Entries are classified correctly as identified, arXiv-resolvable, or unresolved.
Written edge counts match persisted edges.
Retry attempts do not inflate reference-entry counts.
Successful partial crawl is distinguished from API failure.
Resume logic handles complete and partial ledger records without infinite retries.
Rate limiting
429 followed by success.
Multiple 429 responses followed by retry exhaustion.
429 with delay-seconds Retry-After.
429 with HTTP-date Retry-After.
Selected 5xx failures are retried.
Permanent failures are not retried unnecessarily.
Failed requests are not reported as successful empty results.
Schema regression
Existing required columns remain unchanged.
ref_submission_date retains its required mapping.
Serialization works for empty and non-empty results.
Use mocked HTTP responses and deterministic fixtures. Avoid live API calls in the regular test suite.
10. Phase H — Documentation and operational safety
Update relevant documentation or CLI help so that users understand:
The difference between raw references, arXiv-resolvable references, unresolved references, and written edges.
The meaning of ledger statuses.
How fallback requests affect API usage.
How retries and Retry-After affect runtime.
How partial records are handled during resume.
Which fields are optional and what constitutes sufficiently complete metadata.
Do not change the project's data directories, member partitioning, or command-line behavior unnecessarily.
Keep the existing workflow for:
data/raw/member5/references.jsonl
data/raw/member5/ledger.jsonl
statistics under statistics/
Do not delete or overwrite existing crawl outputs during validation.
11. Phase I — Execute verification
After making the changes:
Run the focused tests for the API client, crawler, normalization, metrics, and ledger.
Run the complete test suite.
Run static checks or linting if configured by the project.
Inspect the final diff for accidental schema changes and unrelated edits.
Verify that ref_submission_date = ref.publication_date or "" is preserved.
Run a mocked end-to-end test that exercises API response parsing, metadata fallback, edge creation, ledger updates, and metrics.
If credentials and network access are available, optionally run a very small live smoke test against 1–2 papers. Do not require live API access for test-suite success.
Do not run a large crawl as part of routine verification.
Do not claim real-world performance improvements based only on fixtures or mocked benchmarks.
If an existing issue cannot be fully fixed, explain exactly why, what remains, and what data or API capability is needed.
12. Acceptance criteria
The task is complete only when:
Every identified issue has either been fixed or explicitly documented with a concrete reason it cannot be fixed.
Missing paperId entries are not silently lost when they have another reliable usable identifier.
Unresolvable entries are counted and diagnosed without fabricating identifiers or contaminating the canonical edge dataset.
Partial metadata and fallback behavior follow an explicit completeness policy.
Metrics distinguish raw entries, identified references, unresolved references, and persisted edges.
Ledger statuses distinguish API/transport success from metadata completeness.
Retry behavior is bounded, tested, and honors supported Retry-After formats.
The required date-field mapping and existing schema compatibility are preserved.
Regression tests cover the failure cases described above.
The full test suite has been executed and its actual result reported.
No unrelated changes or destructive modifications to existing crawl data have been introduced.
13. Final report format
Provide a concise but evidence-based report with these sections:
Root causes found — exact files/functions and the observed behavior.
Changes implemented — files changed and the specific fix in each.
Metric and ledger semantics — exact definitions of all relevant counters/statuses.
Tests executed — commands, pass/fail counts, and any skipped tests.
Schema verification — explicitly confirm whether the required ref_submission_date mapping and existing output columns are preserved.
Live smoke test — report results only if actually executed; otherwise say not run.
Remaining risks — unresolved API limitations or incomplete cases.
Diff summary — list modified files and summarize the changes.
Do not report completion merely because the code has been edited. Completion requires actual verification evidence.