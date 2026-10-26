"""Resumable crawler for arXiv -> arXiv reference edges."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from src import config
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
    S2Paper,
)
from src.crawl_references_module.stats import CrawlStats, format_duration, save_stats, stats_paths
from src.utils import (
    append_jsonl,
    append_jsonl_many,
    canonical_arxiv_id,
    is_valid_arxiv_id,
    load_latest_records,
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


def load_reference_pairs(path: Path) -> set[tuple[str, str]]:
    """Load existing ``(citing, cited)`` pairs using canonical IDs."""
    if not path.exists():
        return set()
    pairs: set[tuple[str, str]] = set()
    for record in read_jsonl(path):
        citing = canonical_arxiv_id(record.get("arxiv_id"))
        cited = canonical_arxiv_id(record.get("ref_arxiv_id"))
        if citing and cited:
            pairs.add((citing, cited))
    return pairs


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
    if not record:
        return False
    status = record.get("status")
    if status == "ok":
        return True
    if status == "partial":
        # Problem D1: partial papers are done unless --retry-partial is given.
        return not retry_partial
    if status == "not_found":
        return not retry_not_found
    if status == "error":
        if retry_errors:
            return False
        return _attempts(record) >= config.MAX_PAPER_ATTEMPTS
    return False


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
        "attempts": attempts,
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

    ledger = load_latest_records(paths.ledger)
    pending: list[str] = []
    for arxiv_id in arxiv_range.iter_ids(sizes):
        record = ledger.get(canonical_arxiv_id(arxiv_id))
        if _is_done(
            record,
            retry_errors=retry_errors,
            retry_not_found=retry_not_found,
            retry_partial=retry_partial,
        ):
            stats.add_skipped()
        else:
            pending.append(arxiv_id)

    if limit is not None:
        pending = pending[: max(0, limit)]

    log.info(
        "Member %s: total=%d, already_done=%d, pending=%d%s",
        member, total_ids, stats.skipped, len(pending),
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
                already_done=stats.skipped,
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

    pairs = load_reference_pairs(paths.references)
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
                already_done=stats.skipped,
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
            remaining = max(0, total_ids - stats.skipped - stats.processed)
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
            append_jsonl(paths.ledger, entry, fsync=True)
            ledger[key] = entry
            stats.add_not_found()
            log.info("[tab %d] %s: not indexed by S2", tab_id, arxiv_id)
        except S2AuthError:
            raise
        except S2Error as error:
            attempts += 1
            entry = _ledger_entry(
                arxiv_id, member, status="error", attempts=attempts, error=str(error),
            )
            append_jsonl(paths.ledger, entry, fsync=True)
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
            )
            append_jsonl(paths.ledger, entry, fsync=True)
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
) -> Any:
    """Crawl Semantic Scholar references for a list of arXiv IDs, generating Table 3 CSV.

    Exports:
    - out_path (JSONL): Reference citation edges matching Milestone 1 requirements.
    - csv_path (CSV): Table 3 (m1_reference_stats) with columns:
      `arxiv_id`, `ref_status`, `num_crawled_refs`, `num_arxiv_refs`, `ref_time_sec`.
    """
    import pandas as pd

    logger_inst = log or logger
    if out_path is None:
        out_path = config.DATA_DIR / "references.jsonl"
    out_p = Path(out_path)
    out_p.parent.mkdir(parents=True, exist_ok=True)

    if csv_path is None:
        csv_path = config.DATA_DIR / "csv" / "m1_reference_stats.csv"
    csv_p = Path(csv_path)
    csv_p.parent.mkdir(parents=True, exist_ok=True)
    jsonl_stats_p = csv_p.with_suffix(".jsonl")

    expected_cols = ["arxiv_id", "ref_status", "num_crawled_refs", "num_arxiv_refs", "ref_time_sec"]

    if dry_run:
        logger_inst.info("dry_run=True, skipping Semantic Scholar queries.")
        return pd.DataFrame(columns=expected_cols)

    done_ids: set[str] = set()
    records: list[dict[str, Any]] = []

    if resume:
        if jsonl_stats_p.exists():
            for row in read_jsonl(jsonl_stats_p):
                records.append(row)
                if row.get("arxiv_id"):
                    done_ids.add(str(row["arxiv_id"]))
        elif csv_p.exists():
            try:
                df_existing = pd.read_csv(csv_p, dtype={"arxiv_id": str})
                records = df_existing.to_dict(orient="records")
                done_ids = set(df_existing["arxiv_id"].astype(str))
            except Exception:
                pass

    todo_ids = [str(x).strip() for x in id_list if str(x).strip() and (not resume or str(x).strip() not in done_ids)]
    if limit > 0:
        todo_ids = todo_ids[:limit]

    # Resolve client
    owns_client = False
    active_client = client
    if active_client is None:
        active_client = S2ApiClient()
        owns_client = True

    async def _fetch_one(cid: str) -> tuple[str, list[S2Paper]]:
        page = active_client.pages[0] if getattr(active_client, "pages", None) else None
        if asyncio.iscoroutinefunction(active_client.get_references):
            refs = await active_client.get_references(page, cid)
        else:
            refs = active_client.get_references(page, cid)
        return cid, refs

    async def _run_crawl():
        if hasattr(active_client, "start"):
            if not getattr(active_client, "started", False):
                if asyncio.iscoroutinefunction(active_client.start):
                    await active_client.start(1)
                else:
                    active_client.start(1)
        try:
            for cid in todo_ids:
                t0 = time.perf_counter()
                ref_status = "failed"
                num_crawled_refs = 0
                num_arxiv_refs = 0
                raw_refs: list[S2Paper] = []

                try:
                    _, raw_refs = await _fetch_one(cid)
                    ref_status = "success"
                    num_crawled_refs = len(raw_refs)
                except S2NotFound:
                    ref_status = "not_found"
                except Exception as err:
                    logger_inst.warning("S2 query failed for %s: %s", cid, err)
                    ref_status = "failed"

                dt = time.perf_counter() - t0

                # Process reference records
                edge_records: list[dict] = []
                for ref_item in raw_refs:
                    rec = build_reference_record(cid, ref_item)
                    if rec is not None:
                        num_arxiv_refs += 1
                        edge_records.append(rec)

                if edge_records:
                    append_jsonl_many(out_p, edge_records, fsync=True)

                stat_item = {
                    "arxiv_id": cid,
                    "ref_status": ref_status,
                    "num_crawled_refs": int(num_crawled_refs),
                    "num_arxiv_refs": int(num_arxiv_refs),
                    "ref_time_sec": round(float(dt), 4),
                }
                records.append(stat_item)
                append_jsonl(jsonl_stats_p, stat_item)
                done_ids.add(cid)
        finally:
            if owns_client and hasattr(active_client, "close"):
                if asyncio.iscoroutinefunction(active_client.close):
                    await active_client.close()
                else:
                    active_client.close()

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # Running inside existing loop
        task = loop.create_task(_run_crawl())
        # If in async loop without nest_asyncio, wait with asyncio.wait
        import concurrent.futures
        # Alternatively run in new thread or nest_asyncio
        try:
            import nest_asyncio
            nest_asyncio.apply()
            loop.run_until_complete(_run_crawl())
        except Exception:
            with concurrent.futures.ThreadPoolExecutor() as pool:
                pool.submit(lambda: asyncio.run(_run_crawl())).result()
    else:
        asyncio.run(_run_crawl())

    df = pd.DataFrame(records)
    if not df.empty:
        df = df.drop_duplicates(subset=["arxiv_id"], keep="last")
        for col in expected_cols:
            if col not in df.columns:
                df[col] = None
        df = df[expected_cols]
    else:
        df = pd.DataFrame(columns=expected_cols)

    df.to_csv(csv_p, index=False)
    logger_inst.info("Saved Table 3 stats to %s (%d rows)", csv_p, len(df))
    return df

