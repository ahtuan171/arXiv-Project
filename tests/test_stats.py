"""Tests for crawler statistics."""

from __future__ import annotations

import json

from src.stats import CrawlStats, build_report, save_stats, stats_paths


class FakeRef:
    def __init__(self, arxiv_id):
        self.arxiv_id = arxiv_id


def test_add_paper_counts_refs_and_arxiv_ids():
    stats = CrawlStats()
    stats.add_paper([FakeRef("2401.00001"), FakeRef(None), FakeRef("2401.00002")])
    stats.add_paper([FakeRef(None)])
    assert stats.processed == 2
    assert stats.refs_found == 4
    assert stats.refs_with_arxiv == 2
    assert stats.refs_written == 2
    assert stats.errors == 0


def test_add_paper_accepts_written_override():
    stats = CrawlStats()
    stats.add_paper([FakeRef("2401.00001"), FakeRef("2401.00002")], written=1)
    assert stats.refs_with_arxiv == 2
    assert stats.refs_written == 1


def test_add_error_and_not_found_count_processed():
    stats = CrawlStats()
    stats.add_error()
    stats.add_not_found()
    stats.add_paper([])
    stats.add_skipped(3)
    assert stats.processed == 3
    assert stats.errors == 1
    assert stats.not_found == 1
    assert stats.skipped == 3
    assert stats.refs_found == 0


def test_build_report_contains_all_counters():
    stats = CrawlStats(processed=3, refs_found=10, refs_with_arxiv=4, refs_written=3, errors=1, not_found=1)
    report = build_report(stats, member="member5", total_ids=5000, already_done=7, elapsed_seconds=12.34)
    assert "member5" in report
    assert "5,000" in report
    assert "| Số paper đã xử lý | 3 |" in report
    assert "| Số paper không có trên S2 | 1 |" in report
    assert "| Số references tìm thấy | 10 |" in report
    assert "| Số references có arXiv ID | 4 |" in report
    assert "| Số cạnh mới đã ghi | 3 |" in report
    assert "| Số lỗi | 1 |" in report
    assert "40.0%" in report


def test_build_report_handles_zero_refs():
    report = build_report(CrawlStats())
    assert "0.0%" in report


def test_save_stats_writes_json_and_markdown(tmp_path):
    stats = CrawlStats(processed=2, refs_found=5, refs_with_arxiv=3, refs_written=2, errors=0)
    json_path = tmp_path / "stats.json"
    report_path = tmp_path / "index.md"
    save_stats(stats, json_path, report_path, member="member5", total_ids=5000, already_done=1, elapsed_seconds=1.5)

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload == {
        "processed": 2,
        "refs_found": 5,
        "refs_with_arxiv": 3,
        "refs_written": 2,
        "not_found": 0,
        "errors": 0,
        "skipped": 0,
        "client_type": "",
        "api_calls": 0,
        "browser_calls": 0,
        "rate_limited": 0,
        "member": "member5",
        "total_ids": 5000,
        "already_done": 1,
        "elapsed_seconds": 1.5,
    }
    assert "| Số references có arXiv ID | 3 |" in report_path.read_text(encoding="utf-8")


def test_stats_paths_are_per_member():
    first_json, first_report = stats_paths("member1")
    second_json, second_report = stats_paths("member2")
    assert first_json != second_json
    assert first_report != second_report
    assert first_json.parent.name == "statistics"
