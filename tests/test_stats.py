"""Tests for crawler statistics."""

from __future__ import annotations

import json

from src.crawl_references_module.stats import CrawlStats, build_report, save_stats, stats_paths


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
        "raw_refs_received": 0,
        "refs_with_paper_id": 0,
        "refs_unresolved": 0,
        "fallback_requests": 0,
        "partial": 0,
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


import csv
import pytest
from src.crawl_references_module.s2_client import S2NotFound, S2Error, S2Paper
from src.crawl_references_module.stats import (
    QUERY_STATS_CSV_COLUMNS,
    append_query_stat_csv,
    create_query_stats_csv,
    make_query_stat_record,
    query_and_record_paper_stats,
)


def test_make_query_stat_record_valid():
    rec = make_query_stat_record("2501.07642", "success", 10, 5, 1.23456)
    assert rec == {
        "arxiv_id": "2501_07642",
        "ref_status": "success",
        "num_crawled_refs": 10,
        "num_arxiv_refs": 5,
        "ref_time_sec": 1.2346,
    }


def test_make_query_stat_record_invalid_status():
    with pytest.raises(ValueError, match="Invalid ref_status"):
        make_query_stat_record("2501.07642", "unknown", 0, 0, 0.5)


def test_create_and_append_query_stats_csv(tmp_path):
    csv_file = tmp_path / "stats.csv"
    records = [
        make_query_stat_record("2501.07642", "success", 15, 8, 0.75),
        make_query_stat_record("2501.07643", "not_found", 0, 0, 0.25),
    ]
    created = create_query_stats_csv(records, csv_file)
    assert created == csv_file
    assert csv_file.exists()

    with open(csv_file, mode="r", encoding="utf-8") as f:
        reader = list(csv.DictReader(f))
    assert len(reader) == 2
    assert list(reader[0].keys()) == QUERY_STATS_CSV_COLUMNS
    assert reader[0]["arxiv_id"] == "2501_07642"
    assert reader[0]["ref_status"] == "success"
    assert reader[0]["num_crawled_refs"] == "15"
    assert reader[0]["num_arxiv_refs"] == "8"
    assert reader[0]["ref_time_sec"] == "0.75"

    # Test append
    extra = make_query_stat_record("2501.07644", "failed", 0, 0, 0.12)
    append_query_stat_csv(extra, csv_file)
    with open(csv_file, mode="r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 3
    assert rows[2]["ref_status"] == "failed"


@pytest.mark.anyio
async def test_query_and_record_paper_stats_success():
    class MockSuccessClient:
        async def get_references(self, page, arxiv_id):
            return [
                S2Paper("p1", "2401.00001", "T1", ["A1"], 2024, "2024-01-01"),
                S2Paper("p2", None, "T2", ["A2"], 2024, "2024-01-02"),
            ]

    rec = await query_and_record_paper_stats(MockSuccessClient(), "2501.07642")
    assert rec["arxiv_id"] == "2501_07642"
    assert rec["ref_status"] == "success"
    assert rec["num_crawled_refs"] == 2
    assert rec["num_arxiv_refs"] == 1
    assert rec["ref_time_sec"] >= 0.0


@pytest.mark.anyio
async def test_query_and_record_paper_stats_not_found():
    class MockNotFoundClient:
        async def get_references(self, page, arxiv_id):
            raise S2NotFound("Not found")

    rec = await query_and_record_paper_stats(MockNotFoundClient(), "2501.07642")
    assert rec["arxiv_id"] == "2501_07642"
    assert rec["ref_status"] == "not_found"
    assert rec["num_crawled_refs"] == 0
    assert rec["num_arxiv_refs"] == 0


@pytest.mark.anyio
async def test_query_and_record_paper_stats_failed():
    class MockErrorClient:
        async def get_references(self, page, arxiv_id):
            raise S2Error("Server 500 error")

    rec = await query_and_record_paper_stats(MockErrorClient(), "2501.07642")
    assert rec["arxiv_id"] == "2501_07642"
    assert rec["ref_status"] == "failed"
    assert rec["num_crawled_refs"] == 0
    assert rec["num_arxiv_refs"] == 0

