#!/usr/bin/env python3
"""Crawl references from Semantic Scholar for a member's arXiv ID range.

Usage:
    export ARXIV_MEMBER=memberX
    export S2_API_KEY=your_key
    python scripts/crawl_references.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config
from src.utils import setup_logging, load_done_ids, append_jsonl
from src.config import load_member_range, load_month_sizes
from src.s2_client import S2Client
from src.stats import CrawlStats, save_stats

LOG_DIR = config.LOG_DIR
RAW_DIR = config.RAW_DIR
REFERENCES_PATH = RAW_DIR / "references.jsonl"
LOG_PATH = LOG_DIR / "crawl_references.log"

logger = setup_logging("crawl_references", LOG_PATH)


def main() -> int:
    member = config.os.environ.get("ARXIV_MEMBER")
    if not member:
        logger.error("Set ARXIV_MEMBER env var (e.g., member3)")
        return 1

    try:
        month_sizes = load_month_sizes()
        arxiv_range = load_member_range(member)
    except Exception as e:
        logger.error("Failed to load range for %s: %s", member, e)
        return 1

    total_ids = arxiv_range.count(month_sizes)
    logger.info("Member %s: %d paper IDs to process", member, total_ids)

    s2 = S2Client()
    done_ids = load_done_ids(REFERENCES_PATH, key="arxiv_id")
    logger.info("Already done: %d papers", len(done_ids))

    stats = CrawlStats()
    started = time.monotonic()

    def persist() -> None:
        save_stats(
            stats,
            member=member,
            total_ids=total_ids,
            already_done=len(done_ids),
            elapsed_seconds=time.monotonic() - started,
        )

    try:
        for arxiv_id in arxiv_range.iter_ids(month_sizes):
            if arxiv_id in done_ids:
                continue

            try:
                refs = s2.get_references(arxiv_id)
                for ref in refs:
                    if ref.arxiv_id:
                        record = {
                            "arxiv_id": arxiv_id,
                            "ref_arxiv_id": ref.arxiv_id,
                            "ref_title": ref.title,
                            "ref_authors": ref.authors,
                            "ref_submission_date": ref.publication_date or "",
                            "ref_s2_id": ref.paper_id,
                        }
                        append_jsonl(REFERENCES_PATH, record)
                stats.add_paper(refs)
            except Exception as e:
                stats.add_error()
                logger.error("Failed to process %s: %s", arxiv_id, e)

            if stats.processed % 50 == 0:
                persist()
                logger.info(
                    "Progress: processed=%d, refs_found=%d, refs_with_arxiv=%d, errors=%d",
                    stats.processed, stats.refs_found, stats.refs_with_arxiv, stats.errors
                )
    finally:
        persist()

    logger.info(
        "DONE: processed=%d, refs_found=%d, refs_with_arxiv=%d, errors=%d",
        stats.processed, stats.refs_found, stats.refs_with_arxiv, stats.errors
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())