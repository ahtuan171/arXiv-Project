#!/usr/bin/env python3
"""Crawl references from Semantic Scholar for a member's arXiv ID range.

Usage:
    export ARXIV_MEMBER=memberX
    python src/scripts/crawl_references.py

Pipeline and Metrics:
    - raw_refs_received: Raw reference entries returned by S2 API.
    - refs_with_paper_id: Reference entries possessing a Semantic Scholar paperId.
    - refs_with_arxiv_id: Reference entries resolved to a valid arXiv ID.
    - refs_unresolved: Entries that cannot be resolved to a valid identifier.
    - edges_written: Valid arXiv -> arXiv reference edges written to JSONL.

Ledger Statuses:
    - ok: Successfully crawled with all references resolved.
    - partial: Successfully crawled, but some references were unresolvable.
               Skipped on resume unless --retry-partial is specified.
    - not_found: Paper was not found on Semantic Scholar (HTTP 404).
    - error: Transient/transport errors exhausted retries. Retried with --retry-errors.

Fallback & Completeness:
    Direct metadata extraction minimizes API calls by reading nested citedPaper
    objects directly. Records missing required metadata (such as authors or
    publication date for arXiv papers) trigger a batch fallback lookup to
    enrich missing fields without overwriting valid direct data.

Retry & Rate Limits:
    HTTP 429 and 5xx responses are handled with exponential backoff, jitter, and
    Retry-After header parsing (both integer seconds and RFC 7231 HTTP-date).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src import config
from src.crawler import crawl_references, paths_for
from src.utils import LockError, file_lock, setup_logging


def _parse_month_sizes(values: list[str]) -> dict[str, int]:
    sizes: dict[str, int] = {}
    for item in values:
        if "=" not in item:
            raise ValueError(f"Invalid --month-size {item!r}; expected YYMM=MAX_ID")
        month, raw_size = item.split("=", 1)
        try:
            sizes[month.strip()] = int(raw_size)
        except ValueError as error:
            raise ValueError(f"Invalid --month-size {item!r}; MAX_ID must be an integer") from error
    return sizes


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Crawl arXiv references from Semantic Scholar with robust error handling and fallback",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--member", help="Member key in ranges.yaml (default: ARXIV_MEMBER)")
    parser.add_argument(
        "--client",
        choices=["api", "browser", "both"],
        default="api",
        help="Which Semantic Scholar client to use (default: api)",
    )
    parser.add_argument(
        "--api-min-interval",
        type=float,
        default=None,
        help="Minimum seconds between API requests (default: S2_API_MIN_INTERVAL)",
    )
    parser.add_argument("--limit", type=int, default=None, help="Process at most N pending papers")
    parser.add_argument("--dry-run", action="store_true", help="Show progress plan without API calls")
    parser.add_argument("--retry-errors", action="store_true", help="Retry papers that previously ended in error status")
    parser.add_argument("--retry-not-found", action="store_true", help="Retry papers previously marked not_found")
    parser.add_argument(
        "--retry-partial",
        action="store_true",
        help="Retry papers previously completed with partial status (unresolved references)",
    )
    parser.add_argument("--tabs", type=int, default=None, help="Concurrent browser tabs (default: S2_TABS or 3)")
    parser.add_argument("--headed", action="store_true", help="Show the browser window (needs a display)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Log every S2 request as well as every paper")
    parser.add_argument("--tolerate-errors", type=int, default=0, help="Exit 0 with at most N errors")
    parser.add_argument("--data-dir", type=Path, default=None, help="Override ARXIV_DATA_DIR")
    parser.add_argument(
        "--month-size",
        action="append",
        default=[],
        metavar="YYMM=MAX_ID",
        help="Fill a missing month size, e.g. --month-size 2501=24000 (repeatable)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    member = args.member or os.environ.get("ARXIV_MEMBER")
    if not member:
        parser.error("Pass --member or set ARXIV_MEMBER (e.g., member3)")

    try:
        paths = paths_for(member, args.data_dir)
    except ValueError as error:
        parser.error(str(error))

    log = setup_logging(
        f"crawl_references.{member}",
        paths.log,
        level=logging.DEBUG if args.verbose else logging.INFO,
    )
    log.info("Starting crawl for %s", member)

    try:
        month_sizes = _parse_month_sizes(args.month_size)
    except ValueError as error:
        parser.error(str(error))
    if args.tabs is not None and args.tabs < 1:
        parser.error("--tabs must be >= 1")
    if args.api_min_interval is not None and args.api_min_interval < 0:
        parser.error("--api-min-interval must be >= 0")

    def run():
        return crawl_references(
            member,
            paths=paths,
            client_type=args.client,
            api_min_interval=args.api_min_interval,
            limit=args.limit,
            dry_run=args.dry_run,
            retry_errors=args.retry_errors,
            retry_not_found=args.retry_not_found,
            retry_partial=args.retry_partial,
            month_sizes=month_sizes or None,
            log=log,
            tabs=args.tabs or config.S2_TABS,
            headless=not args.headed,
        )

    try:
        if args.dry_run:
            result = run()
        else:
            with file_lock(paths.ledger.with_name(".crawler.lock")):
                result = run()
    except LockError as error:
        log.error("%s", error)
        return 1
    except KeyboardInterrupt:
        log.warning("Interrupted; progress has been saved, rerun to resume")
        return 130
    except Exception:
        log.exception("Fatal error while crawling %s", member)
        return 1

    if result.fatal:
        log.error("Crawl aborted for %s; see the error above", member)
        return 1
    if result.dry_run:
        return 0
    if result.stats.errors > args.tolerate_errors:
        log.error(
            "Finished with %d errors (tolerance %d); rerun to retry",
            result.stats.errors, args.tolerate_errors,
        )
        return 2

    log.info("Finished %s successfully", member)
    return 0


if __name__ == "__main__":
    sys.exit(main())
