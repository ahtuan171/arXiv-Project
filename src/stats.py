"""Statistics for the reference-metadata crawler.

Counters are kept in :class:`CrawlStats`, persisted as JSON and rendered to a
Markdown report so a run can be inspected without parsing the log file.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

STATS_DIR = Path(__file__).resolve().parent.parent / "statitics"
STATS_JSON_PATH = STATS_DIR / "crawl_references.json"
STATS_REPORT_PATH = STATS_DIR / "index.md"


@dataclass
class CrawlStats:
    """Counters collected while crawling references of arXiv papers."""

    processed: int = 0
    refs_found: int = 0
    refs_with_arxiv: int = 0
    errors: int = 0

    def add_paper(self, refs: Iterable[Any]) -> None:
        """Record one successfully processed paper and its references."""
        refs = list(refs)
        self.processed += 1
        self.refs_found += len(refs)
        self.refs_with_arxiv += sum(1 for ref in refs if getattr(ref, "arxiv_id", None))

    def add_error(self) -> None:
        """Record one paper that could not be crawled."""
        self.processed += 1
        self.errors += 1

    def to_dict(self) -> dict[str, int]:
        """Return the counters as a plain dictionary."""
        return asdict(self)


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
    if total_ids is not None:
        lines.append(f"- **Tổng số arXiv ID:** {total_ids:,}")
    lines.append(f"- **Đã xử lý từ trước (bỏ qua):** {already_done:,}")
    if elapsed_seconds is not None:
        lines.append(f"- **Thời gian chạy:** {elapsed_seconds:.1f}s")
    lines.append(f"- **Cập nhật lúc:** {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    lines += [
        "",
        "| Chỉ số | Giá trị |",
        "|---|---:|",
        f"| Số paper đã xử lý | {stats.processed:,} |",
        f"| Số references tìm thấy | {stats.refs_found:,} |",
        f"| Số references có arXiv ID | {stats.refs_with_arxiv:,} |",
        f"| Số lỗi | {stats.errors:,} |",
        "",
    ]
    ratio = stats.refs_with_arxiv / stats.refs_found * 100 if stats.refs_found else 0.0
    lines.append(f"Tỉ lệ references có arXiv ID: **{ratio:.1f}%**")
    lines.append("")
    return "\n".join(lines)


def save_stats(
    stats: CrawlStats,
    json_path: Path = STATS_JSON_PATH,
    report_path: Path = STATS_REPORT_PATH,
    **report_kwargs: Any,
) -> None:
    """Write the JSON snapshot and the Markdown report."""
    json_path.parent.mkdir(parents=True, exist_ok=True)
    payload = stats.to_dict()
    payload.update({key: value for key, value in report_kwargs.items() if value is not None})
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(build_report(stats, **report_kwargs), encoding="utf-8")
