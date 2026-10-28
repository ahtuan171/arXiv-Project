"""Crawl arXiv metadata for all IDs -> data/papers.jsonl and data/csv/m1_discovery_stats.csv.

Output JSONL (Data Schema v1):
    arxiv_id, title, authors, abstract, categories, primary_category,
    submission_date, revised_dates, versions, doi, venue, comment, fetched_at.

Output CSV (Table 1: m1_discovery_stats):
    arxiv_id, discovery_status, num_versions, discovery_time_sec.
"""

from __future__ import annotations

import argparse
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Union

import arxiv
import pandas as pd
import requests

from src import config, utils, recovery
from src.utils import RateLimiter, parse_arxiv_range

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------
log = utils.setup_logging(
    "crawl_papers",
    log_file=config.LOG_DIR / "crawl_papers.log",
)

# ---------------------------------------------------------------------------
# Rate limiter and arXiv client
# ---------------------------------------------------------------------------
_arxiv_limiter = RateLimiter(config.ARXIV_MIN_INTERVAL)


class _MetadataSession(requests.Session):
    """Keep Retry-After before arxiv.HTTPError discards the response headers."""

    def get(self, url: str, **kwargs) -> requests.Response:
        kwargs.setdefault("timeout", config.REQUEST_TIMEOUT)
        response = super().get(url, **kwargs)
        if response.status_code != 200:
            error = arxiv.HTTPError(url, 0, response.status_code)
            error.retry_after = recovery.parse_retry_after(response.headers.get("Retry-After"))
            raise error
        return response


_client = arxiv.Client(delay_seconds=0.0, num_retries=0)
# arxiv has no public session/timeout argument. Its requests transport is the
# only private seam used here; request retries remain owned by try_fetch_arxiv.
_client._session.close()
_client._session = _MetadataSession()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def extract_version_count(entry_id: str) -> int:
    """Extract version number from arXiv entry_id (e.g. '...v3' -> 3)."""
    match = re.search(r"v(\d+)$", entry_id.strip())
    return int(match.group(1)) if match else 1


def build_record(paper: arxiv.Result, original_id: str) -> dict[str, Any]:
    """Convert arxiv.Result into dictionary matching Data Schema v1."""
    n_versions = extract_version_count(paper.entry_id)

    submission_date = paper.published.date().isoformat() if paper.published else None
    if n_versions > 1 and paper.updated:
        revised_dates: list[str] = [paper.updated.date().isoformat()]
    else:
        revised_dates = []

    return {
        "arxiv_id": utils.clean_arxiv_id(original_id),
        "title": paper.title,
        "authors": [a.name for a in paper.authors],
        "abstract": paper.summary.replace("\n", " ") if paper.summary else "",
        "categories": list(paper.categories),
        "primary_category": (
            paper.primary_category.id
            if hasattr(paper.primary_category, "id")
            else (paper.primary_category or None)
        ),
        "submission_date": submission_date,
        "revised_dates": revised_dates,
        "versions": [f"v{i}" for i in range(1, n_versions + 1)],
        "doi": paper.doi,
        "venue": paper.journal_ref,
        "comment": paper.comment,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def error_record(arxiv_id_clean: str, error: str) -> dict[str, Any]:
    """Create a diagnostic record; the ledger decides when it can be retried."""
    return {
        "arxiv_id": arxiv_id_clean,
        "error": error,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


# ---------------------------------------------------------------------------
# Fetch function with retries for HTTP 429, 5xx, and network drops
# ---------------------------------------------------------------------------

def try_fetch_arxiv(
    canonical_id: str,
    rate_limiter: Optional[Any] = None,
    max_retries: int = config.MAX_RETRIES,
    backoff_base: float = config.BACKOFF_BASE,
    max_backoff: float = config.MAX_BACKOFF,
    jitter: float = getattr(config, "JITTER", 0.5),
) -> Optional[arxiv.Result]:
    """Query arXiv API for metadata, retrying on transient errors.

    Retries on:
    - HTTP 429 Too Many Requests (enforces at least 3s rate-limit interval)
    - HTTP 500, 502, 503, 504 server overload
    - Connection timeouts or drops (URLError, ConnectionError, IncompleteRead, Timeout)

    Returns:
        arxiv.Result if found, or None if not found (fail fast, no retries on 404).
    """
    if max_retries < 1:
        raise ValueError("max_retries must be at least 1")
    search = arxiv.Search(id_list=[canonical_id])

    for attempt in range(1, max_retries + 1):
        error_msg = None
        is_429 = False
        retry_after = None

        try:
            return next(_client.results(search), None)
        except arxiv.HTTPError as exc:
            status = getattr(exc, "status", None)
            retry_after = getattr(exc, "retry_after", None)
            if status == 404:
                return None
            elif status == 429:
                is_429 = True
                error_msg = "HTTP 429 Too Many Requests"
            elif status and 500 <= status < 600:
                error_msg = f"HTTP {status} Server Overload"
            else:
                raise recovery.FetchFailure(f"arXiv HTTP error: {exc}", "blocked") from exc
        except Exception as exc:
            err_str = str(exc)
            if "429" in err_str:
                is_429 = True
                error_msg = "HTTP 429 Too Many Requests"
            elif any(code in err_str for code in ["500", "502", "503", "504"]):
                error_msg = f"Server Overload: {exc}"
            else:
                error_msg = f"Network drop / timeout: {exc}"

        if attempt == max_retries:
            log.error("arXiv query %s failed after %d attempts: %s", canonical_id, max_retries, error_msg)
            raise recovery.FetchFailure(f"arXiv query failed for {canonical_id}: {error_msg}")

        if retry_after is not None:
            delay = min(max(float(retry_after), 0.0), config.MAX_RETRY_AFTER)
        else:
            delay = max(config.ARXIV_MIN_INTERVAL, backoff_base ** attempt) if is_429 else (backoff_base ** attempt)
            delay = min(max(delay, 0.0), max_backoff)
        if jitter > 0:
            delay += random.uniform(0.0, jitter)

        log.warning("arXiv query %s failed (%s); retry %d/%d in %.2fs", canonical_id, error_msg, attempt, max_retries, delay)
        time.sleep(delay)

    return None


# ---------------------------------------------------------------------------
# Core Crawl Function
# ---------------------------------------------------------------------------

def crawl_papers(
    id_list: Optional[Iterable[str]] = None,
    start_id: Optional[Union[str, int]] = None,
    end_id: Optional[Union[str, int]] = None,
    month: Optional[str] = None,
    id_list_path: Optional[Union[Path, str]] = None,
    out_path: Union[Path, str] = config.DATA_DIR / "papers.jsonl",
    csv_path: Optional[Union[Path, str]] = None,
    limit: int = 0,
    rate_limiter: Optional[Any] = _arxiv_limiter,
    resume: bool = True,
    retry_errors: bool = False,
    retry_not_found: bool = False,
    retry_partial: bool = False,
) -> dict[str, Any]:
    """Crawl arXiv paper metadata, saving JSONL and Table 1 (m1_discovery_stats) CSV.

    Args:
        id_list: Explicit list of arXiv IDs.
        start_id: First ID in range (if id_list not provided).
        end_id: Last ID in range (if id_list not provided).
        month: Month prefix for integer IDs.
        id_list_path: Path to id_list.txt file.
        out_path: Destination JSONL path.
        csv_path: Destination Table 1 CSV path (default: data/csv/m1_discovery_stats.csv).
        limit: Max papers to crawl (0 = all).
        rate_limiter: Optional RateLimiter instance.
        resume: Skip completed papers and errors still in cooldown.
        retry_errors: Retry failed/blocked papers immediately.
        retry_not_found: Recheck papers previously missing.
        retry_partial: Shared pipeline option; metadata has no partial state.

    Returns:
        dict summary with total, done, success, error, elapsed_sec, etc.
    """
    out_p = Path(out_path)
    out_p.parent.mkdir(parents=True, exist_ok=True)

    if csv_path is None:
        csv_p = config.DATA_DIR / "csv" / "m1_discovery_stats.csv"
    else:
        csv_p = Path(csv_path)
    csv_p.parent.mkdir(parents=True, exist_ok=True)
    jsonl_stats_p = csv_p.with_suffix(".jsonl")

    # Resolve ID list
    if id_list is not None:
        raw_ids = [str(i).strip() for i in id_list if str(i).strip()]
    elif start_id is not None and end_id is not None:
        raw_ids = parse_arxiv_range(start_id, end_id, month=month)
    elif id_list_path is not None or (config.DATA_DIR / "id_list.txt").exists():
        path = Path(id_list_path or (config.DATA_DIR / "id_list.txt"))
        if not path.exists():
            raise FileNotFoundError(f"Khong tim thay file id_list tai {path}")
        raw_ids = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    else:
        raise ValueError("Must provide id_list, (start_id, end_id), or id_list_path")

    store = recovery.RecordStore(out_p)
    stats_records = recovery.load_stats(jsonl_stats_p, csv_p)
    ledger = recovery.RecoveryLedger(csv_p.with_suffix(".ledger.jsonl"))
    todo = []
    already_done = deferred = 0
    # Select once per run: exhaustion moves on to the next ID, never loops here.
    for raw_id in dict.fromkeys(raw_ids):
        key = utils.clean_arxiv_id(raw_id)
        data = store.rows.get(key)
        previous = ledger.get(raw_id)
        if previous is None and data:
            status = ("not_found" if data.get("error") == "not_found" else
                      "error" if data.get("error") else "ok")
            previous = {"status": status}
        # The snapshot/journal is authoritative for the existence of usable data.
        if previous and previous.get("status") == "ok" and (
                not data or data.get("error") or not data.get("title")):
            previous = None
        if resume and recovery.should_skip(previous, retry_errors=retry_errors,
                retry_not_found=retry_not_found, retry_partial=retry_partial):
            if previous.get("status") in {"error", "blocked"}:
                deferred += 1
            else:
                already_done += 1
        else:
            todo.append(raw_id)
    if limit > 0:
        todo = todo[:limit]
    log.info("Tong ID: %d | Da xong: %d | Cho retry: %d | Can crawl: %d", len(raw_ids), already_done, deferred, len(todo))

    success = error = 0
    columns = ["arxiv_id", "discovery_status", "num_versions", "discovery_time_sec"]
    with utils.measure("crawl_papers", disk_path=out_p.parent) as perf:
        try:
            for i, raw_id in enumerate(todo, start=1):
                if rate_limiter is not None:
                    rate_limiter.wait()
                t0 = time.perf_counter()
                status, error_type, message = "ok", "", ""
                record = None
                try:
                    paper = try_fetch_arxiv(raw_id)
                except OSError:
                    # Local I/O failures are not recoverable remote fetch errors.
                    raise
                except Exception as exc:
                    message = str(exc)
                    error_type = getattr(exc, "error_type", "transient")
                    status = "blocked" if error_type == "blocked" else "error"
                    record = error_record(utils.clean_arxiv_id(raw_id), f"fetch_failed: {exc}")
                    error += 1
                    log.error("[%d/%d] %s -> %s: %s", i, len(todo), raw_id, status, exc)
                else:
                    if paper is None:
                        status, error_type, message = "not_found", "not_found", "not_found"
                        record = error_record(utils.clean_arxiv_id(raw_id), "not_found")
                        error += 1
                    else:
                        record = build_record(paper, raw_id)
                        success += 1
                # Journal first, then stats, then ledger. Restart can replay safely.
                store.put(record)
                stat_item = {
                    "arxiv_id": raw_id,
                    "discovery_status": "success" if status == "ok" else "not_found" if status == "not_found" else "failed",
                    "num_versions": len(record.get("versions", [])),
                    "discovery_time_sec": round(time.perf_counter() - t0, 4),
                }
                recovery.durable_append(jsonl_stats_p, stat_item)
                stats_records[utils.clean_arxiv_id(raw_id)] = stat_item
                ledger.record(raw_id, status, error=message, error_type=error_type)
        finally:
            # Ctrl-C still leaves a clean current snapshot; a hard crash is
            # recovered from the journal by both resume and the metadata reader.
            store.compact()
            recovery.export_stats(stats_records, columns, csv_p)

    return {"total_ids": len(raw_ids), "already_done": already_done, "deferred": deferred, "todo": len(todo),
            "success": success, "error": error, "elapsed_sec": round(perf.seconds, 2)}


def main() -> None:
    ap = argparse.ArgumentParser(description="Crawl arXiv paper metadata -> data/papers.jsonl")
    ap.add_argument("--id-list", default=str(config.DATA_DIR / "id_list.txt"))
    ap.add_argument("--start-id", default=None)
    ap.add_argument("--end-id", default=None)
    ap.add_argument("--month", default=None)
    ap.add_argument("--out", default=str(config.DATA_DIR / "papers.jsonl"))
    ap.add_argument("--csv", default=str(config.DATA_DIR / "csv" / "m1_discovery_stats.csv"))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--retry-errors", action="store_true", help="Retry errors/blocked without cooldown")
    ap.add_argument("--retry-not-found", action="store_true", help="Recheck missing papers")
    args = ap.parse_args()

    crawl_papers(
        start_id=args.start_id,
        end_id=args.end_id,
        month=args.month,
        id_list_path=args.id_list if not (args.start_id and args.end_id) else None,
        out_path=args.out,
        csv_path=args.csv,
        limit=args.limit,
        retry_errors=args.retry_errors,
        retry_not_found=args.retry_not_found,
    )


if __name__ == "__main__":
    main()
