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

from src import config, utils
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
_client = arxiv.Client(delay_seconds=0.0, num_retries=0)


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
    """Create error record for JSONL (to prevent duplicate queries on resume)."""
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
    rate_limiter: Optional[Any] = _arxiv_limiter,
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
    search = arxiv.Search(id_list=[canonical_id])

    for attempt in range(1, max_retries + 1):
        if rate_limiter is not None:
            rate_limiter.wait()

        error_msg = None
        is_429 = False

        try:
            return next(_client.results(search), None)
        except arxiv.HTTPError as exc:
            status = getattr(exc, "status", None)
            if status == 404:
                return None
            elif status == 429:
                is_429 = True
                error_msg = "HTTP 429 Too Many Requests"
            elif status and 500 <= status < 600:
                error_msg = f"HTTP {status} Server Overload"
            else:
                error_msg = f"arXiv HTTP error: {exc}"
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
            raise RuntimeError(f"arXiv query failed for {canonical_id}: {error_msg}")

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
        resume: Skip already completed papers if True.

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

    done: set[str] = set()
    stats_records: list[dict[str, Any]] = []

    if resume:
        if out_p.exists():
            done = utils.load_done_ids(out_p, key="arxiv_id")
        # Load existing stats from CSV or JSONL
        if jsonl_stats_p.exists():
            for rec in utils.read_jsonl(jsonl_stats_p):
                stats_records.append(rec)
        elif csv_p.exists():
            try:
                existing_df = pd.read_csv(csv_p)
                stats_records = existing_df.to_dict(orient="records")
            except Exception:
                pass

    todo = [rid for rid in raw_ids if utils.clean_arxiv_id(rid) not in done]
    if limit > 0:
        todo = todo[:limit]

    log.info("Tong ID: %d | Da co: %d | Can crawl: %d", len(raw_ids), len(done), len(todo))

    success = error = 0
    with utils.measure("crawl_papers", disk_path=out_p.parent) as perf:
        for i, raw_id in enumerate(todo, start=1):
            canonical_id = raw_id
            t0 = time.perf_counter()
            discovery_status = "failed"
            num_versions = 0

            try:
                paper = try_fetch_arxiv(canonical_id, rate_limiter=rate_limiter)
            except Exception as exc:
                dt = time.perf_counter() - t0
                log.error("[%d/%d] %s -> error: %s", i, len(todo), raw_id, exc)
                record = error_record(utils.clean_arxiv_id(canonical_id), f"fetch_failed: {exc}")
                error += 1
            else:
                dt = time.perf_counter() - t0
                if paper is None:
                    log.warning("[%d/%d] %s -> not_found", i, len(todo), raw_id)
                    record = error_record(utils.clean_arxiv_id(canonical_id), "not_found")
                    discovery_status = "not_found"
                    error += 1
                else:
                    record = build_record(paper, canonical_id)
                    num_versions = len(record["versions"])
                    discovery_status = "success"
                    log.info("[%d/%d] %s -> OK (%d version(s)): %s", i, len(todo), raw_id, num_versions, record["title"][:60])
                    success += 1

            utils.append_jsonl(out_p, record)

            stat_item = {
                "arxiv_id": canonical_id,
                "discovery_status": discovery_status,
                "num_versions": int(num_versions),
                "discovery_time_sec": round(float(dt), 4),
            }
            stats_records.append(stat_item)
            utils.append_jsonl(jsonl_stats_p, stat_item)
            done.add(utils.clean_arxiv_id(canonical_id))

    # Export Table 1 CSV
    expected_cols = ["arxiv_id", "discovery_status", "num_versions", "discovery_time_sec"]
    if stats_records:
        df = pd.DataFrame(stats_records).drop_duplicates(subset=["arxiv_id"], keep="last")
        for col in expected_cols:
            if col not in df.columns:
                df[col] = None
        df = df[expected_cols]
    else:
        df = pd.DataFrame(columns=expected_cols)

    df.to_csv(csv_p, index=False)
    log.info("Saved Table 1 stats to %s (%d rows)", csv_p, len(df))

    return {
        "total_ids": len(raw_ids),
        "already_done": len(done) - (success + error),
        "todo": len(todo),
        "success": success,
        "error": error,
        "elapsed_sec": round(perf.seconds, 2),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Crawl arXiv paper metadata -> data/papers.jsonl")
    ap.add_argument("--id-list", default=str(config.DATA_DIR / "id_list.txt"))
    ap.add_argument("--start-id", default=None)
    ap.add_argument("--end-id", default=None)
    ap.add_argument("--month", default=None)
    ap.add_argument("--out", default=str(config.DATA_DIR / "papers.jsonl"))
    ap.add_argument("--csv", default=str(config.DATA_DIR / "csv" / "m1_discovery_stats.csv"))
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    crawl_papers(
        start_id=args.start_id,
        end_id=args.end_id,
        month=args.month,
        id_list_path=args.id_list if not (args.start_id and args.end_id) else None,
        out_path=args.out,
        csv_path=args.csv,
        limit=args.limit,
    )


if __name__ == "__main__":
    main()
