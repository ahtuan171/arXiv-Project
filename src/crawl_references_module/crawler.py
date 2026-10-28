"""Resumable crawler for arXiv -> arXiv reference edges."""

from __future__ import annotations

import asyncio
import logging
import inspect
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Union

from src import config, recovery
from src.crawl_references_module.s2_api_client import (
    ReferenceResult,
    S2ApiAuthError,
    S2ApiClient,
    S2ApiRateLimited,
)
from src.crawl_references_module.s2_client import (
    S2AuthError,
    S2BrowserClient,
    S2Error,
    S2NotFound,
    S2PermanentError,
    S2Paper,
)
from src.crawl_references_module.stats import CrawlStats, format_duration, save_stats, stats_paths
from src.utils import (
    append_jsonl,
    append_jsonl_many,
    canonical_arxiv_id,
    is_valid_arxiv_id,
    normalize_arxiv_id,
    read_jsonl,
)

logger = logging.getLogger(__name__)

_MEMBER_RE = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True)
class CrawlPaths:
    """Per-member output, ledger, log and stats locations."""

    references: Path
    ledger: Path
    log: Path
    stats_json: Path
    stats_markdown: Path


@dataclass
class CrawlResult:
    """Outcome of one crawler invocation."""

    stats: CrawlStats
    total_ids: int = 0
    pending: int = 0
    fatal: bool = False
    dry_run: bool = False


class _ComparisonClient:
    """Fetch with the API and the browser, log differences, return API data."""

    def __init__(self, api: Any, browser: Any) -> None:
        self.api = api
        self.browser = browser
        self.pages: list[Any] = []
        self.rate_limiter = getattr(browser, "rate_limiter", None)
        self.api_disabled = False

    @property
    def api_calls(self) -> int:
        return int(getattr(self.api, "api_calls", 0))

    @property
    def request_count(self) -> int:
        return int(getattr(self.browser, "request_count", 0))

    @property
    def rate_limit_hits(self) -> int:
        return int(getattr(self.api, "rate_limit_hits", 0))

    @property
    def auth_errors(self) -> int:
        return int(getattr(self.api, "auth_errors", 0))

    async def start(self, tabs: int) -> "_ComparisonClient":
        await self.api.start(tabs)
        await self.browser.start(tabs)
        self.pages = list(getattr(self.browser, "pages", None) or [])
        return self

    async def close(self) -> None:
        await self.api.close()
        await self.browser.close()

    async def get_references(self, page: Any, arxiv_id: str) -> list[S2Paper]:
        api_refs: Optional[list[S2Paper]] = None
        if not self.api_disabled:
            try:
                api_refs = await self.api.get_references(None, arxiv_id)
            except S2ApiAuthError as error:
                self.api_disabled = True
                logger.critical(
                    "API authentication failed during comparison (%s); disabling API",
                    error,
                )
            except S2ApiRateLimited as error:
                logger.critical(
                    "API rate limited during comparison (%s); using browser only", error,
                )
        browser_refs = await self.browser.get_references(page, arxiv_id)
        if api_refs is None:
            return browser_refs
        self._compare(arxiv_id, api_refs, browser_refs)
        return api_refs

    @staticmethod
    def _compare(arxiv_id: str, api_refs: list[S2Paper], browser_refs: list[S2Paper]) -> None:
        api_ids = {ref.paper_id for ref in api_refs if ref.paper_id}
        browser_ids = {ref.paper_id for ref in browser_refs if ref.paper_id}
        api_arxiv = {ref.arxiv_id for ref in api_refs if ref.arxiv_id}
        browser_arxiv = {ref.arxiv_id for ref in browser_refs if ref.arxiv_id}
        if api_ids == browser_ids and api_arxiv == browser_arxiv:
            return
        missing = browser_ids - api_ids
        extra = api_ids - browser_ids
        logger.warning(
            "Client mismatch for %s: api=%d browser=%d missing=%d extra=%d "
            "arxiv_only_api=%d arxiv_only_browser=%d",
            arxiv_id, len(api_refs), len(browser_refs), len(missing), len(extra),
            len(api_arxiv - browser_arxiv), len(browser_arxiv - api_arxiv),
        )
        if missing:
            logger.debug("References missing from API for %s: %s", arxiv_id, sorted(missing)[:5])
        if extra:
            logger.debug("References only in API for %s: %s", arxiv_id, sorted(extra)[:5])


def log_metrics(
    stats: CrawlStats,
    clients: list[Any],
    member: str,
    log: logging.Logger,
) -> None:
    """Log per-run metrics and alert on the plan's thresholds."""
    processed = max(1, stats.processed)
    error_rate = stats.errors / processed
    log.info(
        "metrics member=%s client=%s processed=%d errors=%d not_found=%d "
        "rate_limited=%d error_rate=%.1f%% refs_per_paper=%.2f api_calls=%d "
        "browser_calls=%d",
        member, stats.client_type, stats.processed, stats.errors, stats.not_found,
        stats.rate_limited, error_rate * 100, stats.refs_found / processed,
        stats.api_calls, stats.browser_calls,
    )
    if error_rate > 0.25:
        log.error("%s: error rate %.1f%% > 25%%; consider the browser fallback", member, error_rate * 100)
    elif error_rate > 0.10:
        log.warning("%s: error rate %.1f%% > 10%%", member, error_rate * 100)
    seen: set[int] = set()
    auth_errors = 0
    for client in clients:
        if id(client) in seen:
            continue
        seen.add(id(client))
        auth_errors += int(getattr(client, "auth_errors", 0))
    if auth_errors:
        log.critical("%s: %d API authentication error(s); API client disabled", member, auth_errors)
    if stats.rate_limited > 5:
        log.warning(
            "%s: %d rate-limit responses; increase --api-min-interval",
            member, stats.rate_limited,
        )


def paths_for(member: str, data_dir: Optional[Path] = None) -> CrawlPaths:
    """Build isolated paths per member so concurrent crawls cannot collide."""
    if not _MEMBER_RE.fullmatch(member):
        raise ValueError(f"Invalid member name: {member!r}")
    base = Path(data_dir) if data_dir is not None else config.DATA_DIR
    member_dir = base / "raw" / member
    json_path, report_path = stats_paths(member)
    return CrawlPaths(
        references=member_dir / "references.jsonl",
        ledger=member_dir / "ledger.jsonl",
        log=config.LOG_DIR / f"crawl_references_{member}.log",
        stats_json=json_path,
        stats_markdown=report_path,
    )


def load_reference_pairs(path: Path, *, compact: bool = False) -> set[tuple[str, str]]:
    """Load canonical pairs; optionally repair duplicate edges from older runs."""
    if not path.exists():
        return set()
    unique = {}
    unkeyed = []
    duplicates = 0
    for record in read_jsonl(path):
        citing = canonical_arxiv_id(record.get("arxiv_id"))
        cited = canonical_arxiv_id(record.get("ref_arxiv_id"))
        if not citing or not cited:
            unkeyed.append(record)
            continue
        pair = (citing, cited)
        duplicates += pair in unique
        unique[pair] = record
    if compact and duplicates:
        # Atomic replacement keeps the original intact if compaction fails.
        recovery.atomic_write_jsonl(path, [*unkeyed, *unique.values()])
    return set(unique)


def build_reference_record(
    citing_id: str,
    ref: S2Paper,
    member: Optional[str] = None,
    crawled_at: Optional[str] = None,
) -> Optional[dict]:
    """Build one output record, or ``None`` when the reference has no arXiv ID."""
    ref_id = normalize_arxiv_id(ref.arxiv_id)
    if not ref_id or not is_valid_arxiv_id(ref_id):
        return None
    return {
        "arxiv_id": canonical_arxiv_id(citing_id),
        "ref_arxiv_id": canonical_arxiv_id(ref_id),
        "ref_title": ref.title or "",
        "ref_authors": list(ref.authors or []),
        "ref_submission_date": ref.publication_date or "",
        "ref_s2_id": ref.paper_id or "",
    }


def migrate_legacy_references(
    paths: CrawlPaths,
    member: str,
    arxiv_range: config.ArxivRange,
    sizes: dict[str, int],
    log: logging.Logger,
) -> int:
    """Import the old shared ``data/raw/references.jsonl`` once, if present.

    The previous crawler wrote every member into one file. Records belonging
    to this member's range are canonicalized into the per-member file and
    marked done in the ledger. ``ref_publication_date`` is accepted as an
    alias of ``ref_submission_date``.
    Returns the number of edges imported.
    """
    legacy_path = paths.references.parent.parent / "references.jsonl"
    if not legacy_path.exists() or paths.references.exists():
        return 0

    range_ids = {canonical_arxiv_id(arxiv_id) for arxiv_id in arxiv_range.iter_ids(sizes)}
    crawled_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    records: list[dict] = []
    seen: set[tuple[str, str]] = set()
    counts: dict[str, int] = {}

    for record in read_jsonl(legacy_path):
        citing = canonical_arxiv_id(record.get("arxiv_id"))
        cited = canonical_arxiv_id(record.get("ref_arxiv_id"))
        if not citing or not cited or citing not in range_ids:
            continue
        pair = (citing, cited)
        if pair in seen:
            continue
        seen.add(pair)
        counts[citing] = counts.get(citing, 0) + 1
        records.append({
            "arxiv_id": citing,
            "ref_arxiv_id": cited,
            "ref_title": record.get("ref_title") or "",
            "ref_authors": list(record.get("ref_authors") or []),
            "ref_submission_date": record.get("ref_submission_date") or record.get("ref_publication_date") or "",
            "ref_s2_id": record.get("ref_s2_id") or "",
        })

    if not records:
        return 0

    append_jsonl_many(paths.references, records, fsync=True)
    ledger_entries = [
        {
            "schema_version": config.SCHEMA_VERSION,
            "arxiv_id": citing,
            "member": member,
            "status": "ok",
            "attempts": 0,
            "refs_total": count,
            "refs_with_arxiv": count,
            "refs_written": count,
            "error": "",
            "crawled_at": crawled_at,
        }
        for citing, count in counts.items()
    ]
    append_jsonl_many(paths.ledger, ledger_entries, fsync=True)
    log.info(
        "Migrated %d legacy edges from %s (%d papers)",
        len(records), legacy_path, len(ledger_entries),
    )
    return len(records)


def _attempts(record: Optional[dict]) -> int:
    try:
        return int((record or {}).get("attempts") or 0)
    except (TypeError, ValueError):
        return 0


def _is_done(
    record: Optional[dict],
    *,
    retry_errors: bool,
    retry_not_found: bool,
    retry_partial: bool = False,
) -> bool:
    return recovery.should_skip(record, retry_errors=retry_errors,
                                retry_not_found=retry_not_found, retry_partial=retry_partial)


def _ledger_entry(
    arxiv_id: str,
    member: str,
    *,
    status: str,
    attempts: int,
    refs_total: int = 0,
    refs_with_arxiv: int = 0,
    refs_written: int = 0,
    raw_refs_received: int = 0,
    refs_with_paper_id: int = 0,
    refs_unresolved: int = 0,
    fallback_requests: int = 0,
    completeness: str = "",
    refs_unique_total: Optional[int] = None,
    error_type: str = "",
    error: str = "",
) -> dict:
    """Build one ledger record.

    Problem D1 statuses:
    - ``ok``: crawl succeeded, all reference metadata resolved.
    - ``partial``: crawl succeeded, some references unresolved.
    - ``not_found``: S2 does not index the paper.
    - ``error``: transport/API failure.
    """
    return {
        "schema_version": config.SCHEMA_VERSION,
        "arxiv_id": canonical_arxiv_id(arxiv_id),
        "member": member,
        "status": status,
        **recovery.progress_entry(canonical_arxiv_id(arxiv_id), status,
            {"attempts": max(0, attempts - 1)}, error=error, error_type=error_type),
        "refs_unique_total": refs_unique_total,
        "refs_total": refs_total,
        "refs_with_arxiv": refs_with_arxiv,
        "refs_written": refs_written,
        "raw_refs_received": raw_refs_received,
        "refs_with_paper_id": refs_with_paper_id,
        "refs_unresolved": refs_unresolved,
        "fallback_requests": fallback_requests,
        "completeness": completeness,
        "error": error[:500],
        "crawled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def crawl_references(
    member: str,
    *,
    paths: Optional[CrawlPaths] = None,
    client: Optional[S2BrowserClient] = None,
    api_client: Optional[S2ApiClient] = None,
    browser_client: Optional[S2BrowserClient] = None,
    client_type: str = "api",
    api_min_interval: Optional[float] = None,
    limit: Optional[int] = None,
    dry_run: bool = False,
    retry_errors: bool = False,
    retry_not_found: bool = False,
    retry_partial: bool = False,
    month_sizes: Optional[dict[str, int]] = None,
    log: Optional[logging.Logger] = None,
    tabs: int = config.S2_TABS,
    headless: bool = config.S2_HEADLESS,
) -> CrawlResult:
    """Synchronous entry point; see :func:`crawl_references_async`."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError(
            "crawl_references() cannot run inside an event loop; "
            "use await crawl_references_async() instead"
        )
    return asyncio.run(
        crawl_references_async(
            member,
            paths=paths,
            client=client,
            api_client=api_client,
            browser_client=browser_client,
            client_type=client_type,
            api_min_interval=api_min_interval,
            limit=limit,
            dry_run=dry_run,
            retry_errors=retry_errors,
            retry_not_found=retry_not_found,
            retry_partial=retry_partial,
            month_sizes=month_sizes,
            log=log,
            tabs=tabs,
            headless=headless,
        )
    )


async def crawl_references_async(
    member: str,
    *,
    paths: Optional[CrawlPaths] = None,
    client: Optional[S2BrowserClient] = None,
    api_client: Optional[S2ApiClient] = None,
    browser_client: Optional[S2BrowserClient] = None,
    client_type: str = "api",
    api_min_interval: Optional[float] = None,
    limit: Optional[int] = None,
    dry_run: bool = False,
    retry_errors: bool = False,
    retry_not_found: bool = False,
    retry_partial: bool = False,
    month_sizes: Optional[dict[str, int]] = None,
    log: Optional[logging.Logger] = None,
    tabs: int = config.S2_TABS,
    headless: bool = config.S2_HEADLESS,
) -> CrawlResult:
    """Crawl one member's range with the official API or a browser fallback.

    ``client_type`` selects ``"api"`` (default), ``"browser"`` or ``"both"``
    (API result with a browser cross-check). Workers consume papers from a
    shared queue; completed papers are durable in the ledger, so a restart
    resumes exactly where this run stopped. On API auth/quota errors the run
    switches to a browser client without losing progress. An injected
    ``client`` is treated as the browser client (legacy callers) or, when no
    ``api_client`` is given, as the active client for ``client_type="api"``.
    """
    if client_type not in ("api", "browser", "both"):
        raise ValueError(
            f"client_type must be 'api', 'browser' or 'both', not {client_type!r}"
        )
    log = log or logger
    paths = paths or paths_for(member)
    stats = CrawlStats(client_type=client_type)

    try:
        sizes = config.load_month_sizes()
        if month_sizes:
            sizes.update({config.normalize_month(k): int(v) for k, v in month_sizes.items()})
        arxiv_range = config.load_member_range(member)
        total_ids = arxiv_range.count(sizes)
    except Exception as error:
        log.error("Cannot load range for %s: %s", member, error)
        return CrawlResult(stats=stats, fatal=True)

    if not dry_run:
        migrate_legacy_references(paths, member, arxiv_range, sizes, log)

    ledger = recovery.latest_rows(paths.ledger, canonical_arxiv_id)
    existing_pairs = load_reference_pairs(paths.references, compact=not dry_run)
    edge_counts = {}
    for citing, cited in existing_pairs:
        edge_counts[citing] = edge_counts.get(citing, 0) + 1
    pending: list[str] = []
    for arxiv_id in arxiv_range.iter_ids(sizes):
        record = ledger.get(canonical_arxiv_id(arxiv_id))
        if record and record.get("status") in {"ok", "partial"}:
            expected = record.get("refs_unique_total")
            if expected is None:
                expected = record.get("refs_with_arxiv", 0)
            if edge_counts.get(canonical_arxiv_id(arxiv_id), 0) < int(expected or 0):
                record = None
        if _is_done(
            record,
            retry_errors=retry_errors,
            retry_not_found=retry_not_found,
            retry_partial=retry_partial,
        ):
            stats.add_skipped()
            if record.get("status") in {"error", "blocked"}:
                stats.deferred += 1
        else:
            pending.append(arxiv_id)

    if limit is not None:
        pending = pending[: max(0, limit)]

    log.info(
        "Member %s: total=%d, already_done=%d, deferred=%d, pending=%d%s",
        member, total_ids, stats.skipped - stats.deferred, stats.deferred, len(pending),
        f", limit={limit}" if limit is not None else "",
    )

    if dry_run:
        log.info("Dry run: no API calls and no files written")
        return CrawlResult(stats=stats, total_ids=total_ids, pending=len(pending), dry_run=True)

    if not pending:
        log.info("Nothing to do for %s", member)
        try:
            save_stats(
                stats,
                paths.stats_json,
                paths.stats_markdown,
                member=member,
                total_ids=total_ids,
                already_done=stats.skipped - stats.deferred,
                elapsed_seconds=0.0,
            )
        except Exception:
            log.exception("Failed to write stats files")
        return CrawlResult(stats=stats, total_ids=total_ids, pending=0)

    tab_count = max(1, min(int(tabs), len(pending)))

    browser = browser_client or client
    api = api_client
    allow_fallback = False
    if client_type == "browser":
        if browser is None:
            browser = S2BrowserClient(tabs=tab_count, headless=headless)
        active_client: Any = browser
        limiter = getattr(browser, "rate_limiter", None)
        log.info(
            "S2 website in %s: %.2fs/request, tabs=%d",
            "headed Chromium" if getattr(browser, "headless", headless) is False else "headless Chromium",
            getattr(limiter, "min_interval", 0.0), tab_count,
        )
    elif client_type == "both":
        if api is None:
            api = S2ApiClient(min_interval=api_min_interval)
        if browser is None:
            browser = S2BrowserClient(tabs=tab_count, headless=headless)
        active_client = _ComparisonClient(api, browser)
        log.info("S2 API + browser comparison: tabs=%d", tab_count)
    else:
        allow_fallback = True
        if api is None:
            if client is not None:
                # Legacy callers inject a browser-like client directly.
                api = client
                allow_fallback = False
            else:
                api = S2ApiClient(min_interval=api_min_interval)
        active_client = api
        limiter = getattr(api, "rate_limiter", None)
        log.info(
            "S2 API: %.2fs/request, workers=%d",
            getattr(limiter, "min_interval", 0.0), tab_count,
        )

    used_clients: list[Any] = [active_client]
    fallback_lock = asyncio.Lock()

    pairs = existing_pairs
    started = time.monotonic()
    last_flush = started
    last_processed = 0
    fatal = False
    worker_count = 0

    def client_metrics() -> None:
        stats.api_calls = 0
        stats.browser_calls = 0
        stats.rate_limited = 0
        seen: set[int] = set()
        for target in used_clients:
            if id(target) in seen:
                continue
            seen.add(id(target))
            stats.api_calls += int(getattr(target, "api_calls", 0))
            stats.browser_calls += int(getattr(target, "request_count", 0))
            stats.rate_limited += int(getattr(target, "rate_limit_hits", 0))

    def persist() -> None:
        client_metrics()
        try:
            save_stats(
                stats,
                paths.stats_json,
                paths.stats_markdown,
                member=member,
                total_ids=total_ids,
                already_done=stats.skipped - stats.deferred,
                elapsed_seconds=time.monotonic() - started,
            )
        except Exception:
            log.exception("Failed to write stats files")

    def after_paper() -> None:
        nonlocal last_flush, last_processed
        now = time.monotonic()
        if (
            stats.processed - last_processed >= config.STATS_FLUSH_EVERY
            or now - last_flush >= config.PROGRESS_INTERVAL_SECONDS
        ):
            persist()
            elapsed = now - started
            rate = stats.processed / elapsed if elapsed > 0 else 0.0
            remaining = max(0, total_ids - (stats.skipped - stats.deferred) - (stats.processed - stats.errors))
            eta = format_duration(remaining / rate) if rate > 0 else "?"
            log.info(
                "Progress: processed=%d, skipped=%d, not_found=%d, errors=%d, "
                "refs=%d, rate=%.2f/s, ETA=%s",
                stats.processed, stats.skipped, stats.not_found, stats.errors,
                stats.refs_found, rate, eta,
            )
            last_flush = now
            last_processed = stats.processed

    async def ensure_browser() -> Any:
        """Start (once) the browser used when the API becomes unavailable."""
        nonlocal browser
        async with fallback_lock:
            if browser is None:
                browser = S2BrowserClient(
                    tabs=worker_count or tab_count, headless=headless,
                )
            if not getattr(browser, "pages", None):
                await browser.start(worker_count or tab_count)
            if all(target is not browser for target in used_clients):
                used_clients.append(browser)
            return browser

    async def fetch_references(page: object, tab_id: int, arxiv_id: str) -> list[S2Paper]:
        """Fetch references from the active client, falling back on API errors."""
        nonlocal active_client
        current = active_client
        call_page = page
        if call_page is None and not isinstance(current, S2ApiClient):
            # After a fallback the worker still holds an API placeholder page.
            pages = getattr(current, "pages", None) or []
            if len(pages) >= tab_id:
                call_page = pages[tab_id - 1]
        try:
            return await current.get_references(call_page, arxiv_id)
        except (S2ApiAuthError, S2ApiRateLimited) as error:
            if not allow_fallback:
                raise
            target = await ensure_browser()
            active_client = target
            stats.client_type = "api->browser"
            log.critical(
                "%s: API unavailable (%s); switched to browser fallback",
                member, error,
            )
            return await target.get_references(target.pages[tab_id - 1], arxiv_id)

    async def process_one(arxiv_id: str, page: object, tab_id: int) -> None:
        key = canonical_arxiv_id(arxiv_id)
        previous = ledger.get(key)
        attempts = _attempts(previous)

        try:
            refs = await fetch_references(page, tab_id, arxiv_id)
        except S2NotFound:
            entry = _ledger_entry(arxiv_id, member, status="not_found", attempts=attempts)
            recovery.durable_append(paths.ledger, entry)
            ledger[key] = entry
            stats.add_not_found()
            log.info("[tab %d] %s: not indexed by S2", tab_id, arxiv_id)
        except S2AuthError as error:
            entry = _ledger_entry(arxiv_id, member, status="blocked", attempts=attempts + 1,
                                  error=str(error), error_type="blocked")
            recovery.durable_append(paths.ledger, entry)
            ledger[key] = entry
            raise
        except S2Error as error:
            attempts += 1
            entry = _ledger_entry(
                arxiv_id, member, status="blocked" if isinstance(error, S2PermanentError) else "error",
                attempts=attempts, error=str(error),
                error_type="blocked" if isinstance(error, S2PermanentError) else "transient",
            )
            recovery.durable_append(paths.ledger, entry)
            ledger[key] = entry
            stats.add_error()
            log.error("[tab %d] Failed to process %s: %s", tab_id, arxiv_id, error)
        else:
            crawled_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            new_records: list[dict] = []
            seen: set[tuple[str, str]] = set()
            refs_with_arxiv = 0
            for ref in refs:
                record = build_reference_record(arxiv_id, ref, member, crawled_at)
                if record is None:
                    continue
                refs_with_arxiv += 1
                pair = (record["arxiv_id"], record["ref_arxiv_id"])
                if pair in pairs or pair in seen:
                    continue
                seen.add(pair)
                new_records.append(record)

            append_jsonl_many(paths.references, new_records, fsync=True)
            pairs.update(seen)
            edge_counts[key] = edge_counts.get(key, 0) + len(seen)

            # Problem C1/D1: extract per-paper metrics from ReferenceResult
            per_paper_raw = 0
            per_paper_with_pid = 0
            per_paper_unresolved = 0
            per_paper_fallback = 0
            if isinstance(refs, ReferenceResult):
                per_paper_raw = refs.raw_refs_received
                per_paper_with_pid = refs.refs_with_paper_id
                per_paper_unresolved = refs.refs_unresolved
                per_paper_fallback = refs.fallback_requests

            is_partial = per_paper_unresolved > 0
            paper_status = "partial" if is_partial else "ok"
            completeness = "partial" if is_partial else "complete"

            entry = _ledger_entry(
                arxiv_id, member,
                status=paper_status,
                attempts=attempts,
                refs_total=len(refs),
                refs_with_arxiv=refs_with_arxiv,
                refs_written=len(new_records),
                raw_refs_received=per_paper_raw,
                refs_with_paper_id=per_paper_with_pid,
                refs_unresolved=per_paper_unresolved,
                fallback_requests=per_paper_fallback,
                completeness=completeness,
                refs_unique_total=edge_counts.get(key, 0),
            )
            recovery.durable_append(paths.ledger, entry)
            ledger[key] = entry
            stats.add_paper(
                refs,
                written=len(new_records),
                raw_refs_received=per_paper_raw,
                refs_with_paper_id=per_paper_with_pid,
                refs_unresolved=per_paper_unresolved,
                fallback_requests=per_paper_fallback,
                is_partial=is_partial,
            )
            if refs:
                log.info(
                    "[tab %d] %s: refs=%d, with_arxiv=%d, new_edges=%d%s",
                    tab_id, arxiv_id, len(refs), refs_with_arxiv, len(new_records),
                    f", unresolved={per_paper_unresolved}" if per_paper_unresolved else "",
                )
            else:
                log.info("[tab %d] %s: refs=0 (S2 has no references)", tab_id, arxiv_id)
        after_paper()

    try:
        await active_client.start(tab_count)
    except BaseException:
        try:
            await active_client.close()
        except Exception:
            pass
        raise
    pages = list(getattr(active_client, "pages", None) or [])
    if not pages:
        await active_client.close()
        log.critical("Client did not create any worker slots")
        return CrawlResult(stats=stats, total_ids=total_ids, pending=len(pending), fatal=True)

    worker_count = min(tab_count, len(pages))
    queue: asyncio.Queue[Optional[str]] = asyncio.Queue()
    for arxiv_id in pending:
        queue.put_nowait(arxiv_id)
    for _ in range(worker_count):
        queue.put_nowait(None)

    async def worker(tab_id: int) -> None:
        page = pages[tab_id]
        while True:
            arxiv_id = await queue.get()
            try:
                if arxiv_id is None:
                    return
                await process_one(arxiv_id, page, tab_id + 1)
            finally:
                queue.task_done()

    tasks = [asyncio.create_task(worker(i), name=f"s2-tab-{i + 1}") for i in range(worker_count)]
    try:
        try:
            await asyncio.gather(*tasks)
        except S2AuthError as error:
            fatal = True
            log.critical("Semantic Scholar authentication failed: %s", error)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        persist()
        seen_clients: set[int] = set()
        for target in reversed(used_clients):
            if id(target) in seen_clients:
                continue
            seen_clients.add(id(target))
            try:
                await target.close()
            except Exception:
                log.exception("Failed to close a Semantic Scholar client")

    client_metrics()
    tabs_note = f" tabs={worker_count}" if worker_count > 1 else ""
    log.info(
        "DONE member=%s%s: client=%s, processed=%d, not_found=%d, errors=%d, "
        "refs_found=%d, refs_with_arxiv=%d, refs_written=%d, skipped=%d, "
        "api_calls=%d, browser_calls=%d",
        member, tabs_note, stats.client_type, stats.processed, stats.not_found,
        stats.errors, stats.refs_found, stats.refs_with_arxiv, stats.refs_written,
        stats.skipped, stats.api_calls, stats.browser_calls,
    )
    log_metrics(stats, used_clients, member, log)
    return CrawlResult(
        stats=stats,
        total_ids=total_ids,
        pending=len(pending),
        fatal=fatal,
    )


def crawl_references_range(
    id_list: list[str],
    out_path: Optional[Union[Path, str]] = None,
    csv_path: Optional[Union[Path, str]] = None,
    client: Optional[Any] = None,
    limit: int = 0,
    resume: bool = True,
    dry_run: bool = False,
    log: Optional[logging.Logger] = None,
    retry_errors: bool = False,
    retry_not_found: bool = False,
    retry_partial: bool = False,
) -> Any:
    """Collect Table 3 with state-aware resume and idempotent citation writes.

    Retry flags match metadata/source: errors resume after cooldown, while
    not_found and partial results require an explicit request to recheck.
    """
    import pandas as pd

    logger_inst = log or logger
    out_p = Path(out_path or config.DATA_DIR / "references.jsonl")
    csv_p = Path(csv_path or config.DATA_DIR / "csv" / "m1_reference_stats.csv")
    jsonl_stats_p = csv_p.with_suffix(".jsonl")
    columns = ["arxiv_id", "ref_status", "num_crawled_refs", "num_arxiv_refs", "ref_time_sec"]
    if dry_run:
        return pd.DataFrame(columns=columns)

    records = recovery.load_stats(jsonl_stats_p, csv_p, canonical_arxiv_id)
    ledger = recovery.RecoveryLedger(csv_p.with_suffix(".ledger.jsonl"), canonical_arxiv_id)
    pairs = load_reference_pairs(out_p, compact=True)
    counts = {}
    for citing, cited in pairs:
        counts[citing] = counts.get(citing, 0) + 1
    pending = []
    seen_ids = set()
    for cid in id_list:
        cid = str(cid).strip()
        key = canonical_arxiv_id(cid)
        if not cid or key in seen_ids:
            continue
        seen_ids.add(key)
        previous = ledger.get(cid)
        old = records.get(key, {})
        if previous is None:
            status = {"success": "ok", "not_found": "not_found"}.get(old.get("ref_status"), "error")
            previous = {"status": status, "refs_unique_total": old.get("num_arxiv_refs", 0)}
        if previous.get("status") in {"ok", "partial"}:
            expected = int(previous.get("refs_unique_total") or 0)
            if counts.get(key, 0) < expected:
                previous = None
        if not resume or not recovery.should_skip(previous, retry_errors=retry_errors,
                retry_not_found=retry_not_found, retry_partial=retry_partial):
            pending.append(cid)
    if limit > 0:
        pending = pending[:limit]

    async def maybe_await(value):
        return await value if inspect.isawaitable(value) else value

    async def run_crawl():
        # Owned clients are created inside the executing loop, including the
        # notebook bridge thread; no task is scheduled on the notebook loop.
        active = client if client is not None else S2ApiClient()
        try:
            if hasattr(active, "start") and not getattr(active, "started", False):
                await maybe_await(active.start(1))
            for cid in pending:
                key = canonical_arxiv_id(cid)
                t0 = time.perf_counter()
                status, message, error_type = "ok", "", ""
                refs = []
                try:
                    page = active.pages[0] if getattr(active, "pages", None) else None
                    refs = await maybe_await(active.get_references(page, cid))
                except S2NotFound as exc:
                    status, message, error_type = "not_found", str(exc), "not_found"
                except (S2AuthError, S2PermanentError) as exc:
                    status, message, error_type = "blocked", str(exc), "blocked"
                except S2Error as exc:
                    status, message, error_type = "error", str(exc), "transient"
                if isinstance(refs, ReferenceResult) and refs.refs_unresolved > 0:
                    status = "partial"
                # A replay after a crash writes only missing edges. Existing
                # good edges are preserved even if a forced refresh fails.
                edges = []
                current = set()
                for ref in refs:
                    row = build_reference_record(cid, ref)
                    if row is None:
                        continue
                    pair = (row["arxiv_id"], row["ref_arxiv_id"])
                    current.add(pair)
                    if pair not in pairs:
                        edges.append(row)
                        pairs.add(pair)
                append_jsonl_many(out_p, edges, fsync=True)
                counts[key] = counts.get(key, 0) + len(edges)
                stat = {"arxiv_id": cid,
                        "ref_status": "success" if status in {"ok", "partial"} else "not_found" if status == "not_found" else "failed",
                        "num_crawled_refs": len(refs), "num_arxiv_refs": len(current),
                        "ref_time_sec": round(time.perf_counter() - t0, 4)}
                recovery.durable_append(jsonl_stats_p, stat)
                records[key] = stat
                ledger.record(cid, status, error=message, error_type=error_type,
                    refs_unique_total=counts.get(key, 0),
                    refs_unresolved=getattr(refs, "refs_unresolved", 0))
                if status in {"error", "blocked"}:
                    logger_inst.warning("S2 query failed for %s: %s", cid, message)
        finally:
            if client is None and hasattr(active, "close"):
                await maybe_await(active.close())

    if pending:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(run_crawl())
        else:
            # Run exactly once. Do not create a background task and then call
            # run_until_complete again, or retry the whole run on exceptions.
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(lambda: asyncio.run(run_crawl())).result()
    return recovery.export_stats(records, columns, csv_p)
