"""Statistics for the reference-metadata crawler.

Counters are kept in :class:`CrawlStats`, persisted as JSON and rendered to a
Markdown report so a run can be inspected without parsing the log file.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

STATS_DIR = Path(__file__).resolve().parent.parent / "statistics"
STATS_JSON_PATH = STATS_DIR / "crawl_references.json"
STATS_REPORT_PATH = STATS_DIR / "index.md"


def stats_paths(member: str) -> tuple[Path, Path]:
    """Return ``(json_path, markdown_path)`` for one member's run."""
    return (
        STATS_DIR / f"crawl_references_{member}.json",
        STATS_DIR / f"crawl_references_{member}.md",
    )


@dataclass
class CrawlStats:
    """Counters collected while crawling references of arXiv papers.

    Metric semantics (Problem C1):
    - ``raw_refs_received``: reference entries returned by the API before any
      filtering, across all pagination pages.  Does not double-count retries.
    - ``refs_with_paper_id``: entries with a usable Semantic Scholar paper ID.
    - ``refs_found``: deduplicated references passed to the crawler (legacy name
      retained for backward compatibility; equals len(refs) in add_paper).
    - ``refs_with_arxiv`` / ``refs_with_arxiv_id``: references mapped to a valid
      arXiv ID.  The property ``refs_with_arxiv_id`` is a read-only alias.
    - ``refs_unresolved``: entries that cannot be resolved to any target identifier.
    - ``refs_written`` / ``edges_written``: valid reference edges actually persisted.
      The property ``edges_written`` is a read-only alias.
    - ``fallback_requests``: number of batch-metadata fallback requests made.
    - ``partial``: number of papers whose crawl succeeded but had unresolved references.
    """

    processed: int = 0
    refs_found: int = 0
    refs_with_arxiv: int = 0
    refs_written: int = 0
    not_found: int = 0
    errors: int = 0
    skipped: int = 0
    client_type: str = ""
    api_calls: int = 0
    browser_calls: int = 0
    rate_limited: int = 0
    # --- New granular metrics (Problem C1) ---
    raw_refs_received: int = 0
    refs_with_paper_id: int = 0
    refs_unresolved: int = 0
    fallback_requests: int = 0
    partial: int = 0

    @property
    def edges_written(self) -> int:
        """Alias for ``refs_written`` — valid edges actually persisted."""
        return self.refs_written

    @property
    def refs_with_arxiv_id(self) -> int:
        """Alias for ``refs_with_arxiv`` — references mapped to a valid arXiv ID."""
        return self.refs_with_arxiv

    def add_paper(
        self,
        refs: Iterable[Any],
        written: Optional[int] = None,
        *,
        raw_refs_received: int = 0,
        refs_with_paper_id: int = 0,
        refs_unresolved: int = 0,
        fallback_requests: int = 0,
        is_partial: bool = False,
    ) -> None:
        """Record one successfully processed paper and its references."""
        refs = list(refs)
        with_arxiv = sum(1 for ref in refs if getattr(ref, "arxiv_id", None))
        self.processed += 1
        self.refs_found += len(refs)
        self.refs_with_arxiv += with_arxiv
        self.refs_written += with_arxiv if written is None else written
        self.raw_refs_received += raw_refs_received
        self.refs_with_paper_id += refs_with_paper_id
        self.refs_unresolved += refs_unresolved
        self.fallback_requests += fallback_requests
        if is_partial:
            self.partial += 1

    def add_error(self) -> None:
        """Record one paper whose crawl failed after retries."""
        self.processed += 1
        self.errors += 1

    def add_not_found(self) -> None:
        """Record one paper that Semantic Scholar does not index."""
        self.processed += 1
        self.not_found += 1

    def add_skipped(self, count: int = 1) -> None:
        """Record papers skipped because they were already completed."""
        self.skipped += count

    def to_dict(self) -> dict[str, int]:
        """Return the counters as a plain dictionary."""
        return asdict(self)


def format_duration(seconds: float) -> str:
    seconds = max(0.0, seconds)
    minutes, rest = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {rest:02d}s"
    if minutes:
        return f"{minutes}m {rest:02d}s"
    return f"{rest}s"


def build_report(
    stats: CrawlStats,
    *,
    member: Optional[str] = None,
    total_ids: Optional[int] = None,
    already_done: int = 0,
    elapsed_seconds: Optional[float] = None,
) -> str:
    """Render the stats as a Markdown report."""
    lines = [
        "# Thống kê cào metadata references",
        "",
        "Số liệu của lần chạy crawler gần nhất.",
        "",
    ]
    if member is not None:
        lines.append(f"- **Member:** `{member}`")
    if stats.client_type:
        lines.append(f"- **Client:** `{stats.client_type}`")
    if total_ids is not None:
        remaining = max(0, total_ids - already_done - stats.processed)
        lines.append(f"- **Tổng số arXiv ID:** {total_ids:,}")
        lines.append(f"- **Còn lại sau lần chạy này:** {remaining:,}")
    lines.append(f"- **Đã xong từ trước (bỏ qua):** {already_done:,}")
    if elapsed_seconds is not None:
        lines.append(f"- **Thời gian chạy:** {format_duration(elapsed_seconds)}")
        if stats.processed and elapsed_seconds > 0:
            rate = stats.processed / elapsed_seconds
            lines.append(f"- **Tốc độ:** {rate:.2f} paper/s")
    lines.append(f"- **Cập nhật lúc:** {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    lines += [
        "",
        "| Chỉ số | Giá trị |",
        "|---|---:|",
        f"| Số paper đã xử lý | {stats.processed:,} |",
        f"| Số paper không có trên S2 | {stats.not_found:,} |",
        f"| Số references tìm thấy | {stats.refs_found:,} |",
        f"| Số references có arXiv ID | {stats.refs_with_arxiv:,} |",
        f"| Số cạnh mới đã ghi | {stats.refs_written:,} |",
        f"| Số lỗi | {stats.errors:,} |",
    ]
    if stats.raw_refs_received or stats.refs_unresolved or stats.partial:
        lines += [
            f"| Tổng raw API refs | {stats.raw_refs_received:,} |",
            f"| Refs có paperId | {stats.refs_with_paper_id:,} |",
            f"| Refs không xác định | {stats.refs_unresolved:,} |",
            f"| Paper partial | {stats.partial:,} |",
        ]
    if stats.client_type:
        lines += [
            f"| API calls | {stats.api_calls:,} |",
            f"| Browser calls | {stats.browser_calls:,} |",
            f"| Fallback requests | {stats.fallback_requests:,} |",
            f"| Số lần bị rate limit | {stats.rate_limited:,} |",
        ]
    lines.append("")
    ratio = stats.refs_with_arxiv / stats.refs_found * 100 if stats.refs_found else 0.0
    lines.append(f"Tỉ lệ references có arXiv ID: **{ratio:.1f}%**")
    lines.append("")
    return "\n".join(lines)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)


def save_stats(
    stats: CrawlStats,
    json_path: Path = STATS_JSON_PATH,
    report_path: Path = STATS_REPORT_PATH,
    **report_kwargs: Any,
) -> None:
    """Write the JSON snapshot and the Markdown report atomically."""
    payload = stats.to_dict()
    payload.update({key: value for key, value in report_kwargs.items() if value is not None})
    _atomic_write(json_path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    _atomic_write(report_path, build_report(stats, **report_kwargs))
