"""Offline tests for opt-in raw reference capture and resumability."""
import json
import pytest

import csv
from src.crawl_references_module.raw_reference_collector import (
    _fetch_all_pages, collect_raw_references, export_raw_reference_stats, publish_references,
    prune_unverified_references,
)


class Pages:
    def __init__(self, mapping, size=2):
        self.mapping = mapping
        self.page_size = size
        self.calls = []

    async def _request(self, method, path, *, params):
        self.calls.append((path, params["offset"]))
        key = path.split("/paper/arXiv:", 1)[-1].split("/references", 1)[0]
        answer = self.mapping.get((key, params["offset"]))
        if isinstance(answer, Exception):
            raise answer
        if answer is None:
            raise AssertionError(f"Unexpected request {key}/{params['offset']}")
        return answer


def data(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    (root / "references.jsonl").write_text('{"old":"keep"}\n')
    (root / "papers.jsonl").write_text('{"old":"paper"}\n')
    return root


def test_dry_run_never_calls_api_or_writes(tmp_path):
    root = data(tmp_path)
    client = Pages({})
    result = collect_raw_references(root, ids=["2501.00001"], dry_run=True, client=client)
    assert result["summary"]["to_fetch"] == 1
    assert result["summary"]["planned_this_run"] == 1
    assert not client.calls
    assert not (root / "references_raw.jsonl").exists()
    assert not (root / "references_raw_ledger.jsonl").exists()


def test_retains_unresolved_null_duplicate_and_non_arxiv_across_pages(tmp_path):
    root = data(tmp_path)
    rows = [
        {"citedPaper": {"paperId": "S2", "title": "Nonarxiv", "authors": [{"name": "B"}]}},
        {"citedPaper": {"paperId": None, "title": "Unresolved"}},
        {"citedPaper": None},
        {"citedPaper": {"paperId": "S2", "title": "Nonarxiv", "externalIds": {"ArXiv": "2401.00001"}}},
    ]
    client = Pages({
        ("2501.00001", 0): {"data": rows[:2], "total": 4, "next": 2},
        ("2501.00001", 2): {"data": rows[2:], "total": 4, "next": None},
    })
    result = collect_raw_references(
        root, ids=["2501.00001"], dry_run=False, client=client,
    )
    saved = [json.loads(x) for x in (root / "references_raw.jsonl").read_text().splitlines()]
    assert result["summary"]["ok"] == 1
    assert len(saved) == 4
    assert [r["ref_index"] for r in saved] == [0, 1, 2, 3]
    assert all(r["version"] is None and r["version_attribution"] == "unknown_s2_publication_level"
               for r in saved)
    assert saved[0]["ref_arxiv_id"] == ""
    assert saved[1]["ref_s2_id"] == ""
    assert saved[2]["raw_cited_paper"] is None
    assert saved[3]["ref_arxiv_id"] == "2401_00001"
    assert saved[0]["ref_authors"] == ["B"]
    assert (root / "references.jsonl").read_text() == '{"old":"keep"}\n'
    assert (root / "papers.jsonl").read_text() == '{"old":"paper"}\n'
    before = (root / "references_raw.jsonl").read_bytes()
    again = collect_raw_references(root, ids=["2501.00001"], dry_run=False, client=client)
    assert again["summary"]["verified_existing"] == 1
    assert (root / "references_raw.jsonl").read_bytes() == before
    assert len(client.calls) == 2


def test_crash_after_raw_append_before_ledger_recovers_without_duplicates(tmp_path):
    root = data(tmp_path)
    ref = {"citedPaper": {"title": "Foo"}}
    client = Pages({("2501.00001", 0): {"data": [ref], "total": 1, "next": None}})
    collect_raw_references(root, ids=["2501.00001"], dry_run=False, client=client)
    (root / "references_raw_ledger.jsonl").unlink()
    answer = collect_raw_references(root, ids=["2501.00001"], dry_run=False, client=client)
    assert answer["summary"]["ok"] == 1
    assert len((root / "references_raw.jsonl").read_text().splitlines()) == 1
    assert len((root / "references_raw_ledger.jsonl").read_text().splitlines()) == 1


def test_pagination_incomplete_does_not_write_raw(tmp_path):
    root = data(tmp_path)
    client = Pages({("2501.00001", 0): {"data": [{"citedPaper": {}}], "total": 2, "next": None}})
    result = collect_raw_references(root, ids=["2501.00001"], dry_run=False, client=client)
    assert result["summary"]["error"] == 1
    assert not (root / "references_raw.jsonl").exists()


def test_full_page_without_total_or_next_fails_closed(tmp_path):
    root = data(tmp_path)
    client = Pages({("2501.00001", 0): {"data": [{"citedPaper": {}}, {"citedPaper": {}}]}})
    result = collect_raw_references(root, ids=["2501.00001"], dry_run=False, client=client)
    assert result["summary"]["error"] == 1
    assert not (root / "references_raw.jsonl").exists()


def test_conflict_on_changed_upstream_order_does_not_modify_old_rows(tmp_path):
    root = data(tmp_path)
    first = Pages({("2501.00001", 0): {"data": [{"citedPaper": {"title": "A"}}], "total": 1}})
    collect_raw_references(root, ids=["2501.00001"], dry_run=False, client=first)
    old = (root / "references_raw.jsonl").read_bytes()
    (root / "references_raw_ledger.jsonl").unlink()
    changed = Pages({("2501.00001", 0): {"data": [{"citedPaper": {"title": "B"}}], "total": 1}})
    with pytest.raises(ValueError, match="content changed"):
        collect_raw_references(root, ids=["2501.00001"], dry_run=False, client=changed)
    assert (root / "references_raw.jsonl").read_bytes() == old


def test_dry_run_skips_known_not_found_without_retry(tmp_path):
    root = data(tmp_path)
    (root / "references_raw_ledger.jsonl").write_text(json.dumps({
        "arxiv_id": "2501_00001", "status": "not_found"
    }) + "\n")
    plan = collect_raw_references(root, ids=["2501.00001"], dry_run=True)
    assert plan["summary"]["known_not_found"] == 1
    forced = collect_raw_references(root, ids=["2501.00001"], dry_run=True, retry_not_found=True)
    assert forced["summary"]["to_fetch"] == 1


def test_zero_references_valid_completed_and_skipped(tmp_path):
    root = data(tmp_path)
    client = Pages({("2501.00001", 0): {"data": [], "total": 0}})
    result = collect_raw_references(root, ids=["2501.00001"], dry_run=False, client=client)
    assert result["summary"]["ok"] == 1
    again = collect_raw_references(root, ids=["2501.00001"], dry_run=False, client=client)
    assert again["summary"]["verified_existing"] == 1
    assert not (root / "references_raw.jsonl").exists()


def test_progress_and_limit_count_real_requests_not_skips(tmp_path):
    root = data(tmp_path)
    pages = Pages({
        ("2501.00001", 0): {"data": [], "total": 0},
        ("2501.00002", 0): {"data": [], "total": 0},
    })
    progress = []
    result = collect_raw_references(
        root, ids=["2501.00001", "2501.00002"], max_papers=1,
        dry_run=False, client=pages,
        progress_callback=lambda n, total, event: progress.append((n, total, event["status"])),
    )
    assert result["summary"]["network_attempts"] == 1
    assert progress == [(1, 1, "ok")]
    second = collect_raw_references(
        root, ids=["2501.00001", "2501.00002"], max_papers=1,
        dry_run=False, client=pages,
    )
    assert second["summary"]["verified_existing"] == 1
    assert second["summary"]["network_attempts"] == 1
    assert len(pages.calls) == 2


def test_corrupted_jsonl_refuses_writes(tmp_path):
    root = data(tmp_path)
    (root / "references_raw.jsonl").write_text('{"arxiv_id":')
    with pytest.raises(ValueError, match="Invalid JSONL"):
        collect_raw_references(root, ids=["2501.00001"], dry_run=False, client=Pages({}))

def test_export_raw_reference_stats_creates_table3_csv(tmp_path):
    root = data(tmp_path)
    (root / "references_raw_ledger.jsonl").write_text(
        json.dumps({"arxiv_id": "2501_00001", "status": "ok", "raw_refs_total": 5, "refs_with_arxiv_id": 3, "ref_time_sec": 0.1234}) + "\n" +
        json.dumps({"arxiv_id": "2501_00002", "status": "not_found", "raw_refs_total": 0, "refs_with_arxiv_id": 0, "ref_time_sec": 0.05}) + "\n" +
        json.dumps({"arxiv_id": "2501_00003", "status": "error", "raw_refs_total": 0, "refs_with_arxiv_id": 0, "ref_time_sec": 0.01}) + "\n"
    )
    csv_p = export_raw_reference_stats(root)
    assert csv_p == root / "csv" / "m1_reference_stats.csv"
    assert csv_p.is_file()

    with open(csv_p, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 3
    assert [r["arxiv_id"] for r in rows] == ["2501.00001", "2501.00002", "2501.00003"]
    assert [r["ref_status"] for r in rows] == ["success", "not_found", "failed"]
    assert [r["num_crawled_refs"] for r in rows] == ["5", "0", "0"]
    assert [r["num_arxiv_refs"] for r in rows] == ["3", "0", "0"]
    assert [r["ref_time_sec"] for r in rows] == ["0.1234", "0.05", "0.01"]


def test_collect_raw_references_auto_exports_csv(tmp_path):
    root = data(tmp_path)
    client = Pages({
        ("2501.00001", 0): {"data": [{"citedPaper": {"paperId": "p1", "externalIds": {"ArXiv": "2401.00001"}}}], "total": 1, "next": None}
    })
    result = collect_raw_references(root, ids=["2501.00001"], dry_run=False, client=client)
    assert result["csv_path"] == str(root / "csv" / "m1_reference_stats.csv")
    csv_p = root / "csv" / "m1_reference_stats.csv"
    assert csv_p.is_file()
    with open(csv_p, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1
    assert rows[0]["arxiv_id"] == "2501.00001"
    assert rows[0]["ref_status"] == "success"
    assert rows[0]["num_crawled_refs"] == "1"
    assert rows[0]["num_arxiv_refs"] == "1"

def test_publish_references_extracts_valid_pairs(tmp_path):
    root = tmp_path / 'data'
    root.mkdir()
    (root / 'references_raw.jsonl').write_text(
        json.dumps({'arxiv_id': '2501_00001', 'ref_arxiv_id': '2401_00002', 'ref_title': 'Paper A', 'ref_authors': ['Auth']}) + '\n' +
        json.dumps({'arxiv_id': '2501_00001', 'ref_arxiv_id': '', 'ref_title': 'Non-arxiv', 'ref_authors': []}) + '\n'
    )
    p = publish_references(root)
    assert p == root / 'references.jsonl'
    rows = [json.loads(x) for x in p.read_text(encoding='utf-8').splitlines()]
    assert len(rows) == 1
    assert rows[0]['arxiv_id'] == '2501_00001'
    assert rows[0]['ref_arxiv_id'] == '2401_00002'
    assert rows[0]['ref_title'] == 'Paper A'



class MockRawBatchClient:
    def __init__(self, data_map):
        self.data_map = data_map
        self.calls = []

    def _clean_id(self, aid: str) -> str:
        return aid.replace("_", ".")

    async def fetch_papers_batch_raw(self, formatted_ids: list[str], fields: str = ""):
        self.calls.append(formatted_ids)
        results = []
        for fid in formatted_ids:
            clean = fid.replace("ARXIV:", "")
            results.append(self.data_map.get(clean))
        return results


def test_collect_raw_references_batch_mode_success_and_not_found(tmp_path):
    root = data(tmp_path)
    client = MockRawBatchClient({
        "2501.00001": {
            "paperId": "p1",
            "title": "Paper 1",
            "references": [
                {
                    "paperId": "ref_1",
                    "title": "Ref One",
                    "authors": [{"name": "Author 1"}],
                    "year": 2023,
                    "publicationDate": "2023-05-01",
                    "externalIds": {"ArXiv": "2301.00001"},
                },
                {
                    "paperId": "ref_2",
                    "title": "Ref Non-arXiv",
                    "authors": [{"name": "Author 2"}],
                    "year": 2022,
                    "externalIds": {},
                },
            ],
        },
        "2501.00002": {
            "paperId": "p2",
            "title": "Paper 2 Zero Refs",
            "references": [],
        },
        # 2501.00003 is not in data_map -> returns None (not found)
    })

    progress = []
    result = collect_raw_references(
        root,
        ids=["2501.00001", "2501.00002", "2501.00003"],
        max_papers=3,
        batch_size=100,
        dry_run=False,
        client=client,
        progress_callback=lambda n, total, ev: progress.append((n, total, ev["arxiv_id"], ev["status"])),
    )

    assert len(client.calls) == 1
    assert len(progress) == 3
    assert progress[0] == (1, 3, "2501_00001", "ok")
    assert progress[1] == (2, 3, "2501_00002", "ok")
    assert progress[2] == (3, 3, "2501_00003", "not_found")

    assert result["summary"]["ok"] == 2
    assert result["summary"]["not_found"] == 1

    # Verify references_raw.jsonl
    raw_lines = [json.loads(l) for l in (root / "references_raw.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(raw_lines) == 2
    assert raw_lines[0]["arxiv_id"] == "2501_00001"
    assert raw_lines[0]["ref_arxiv_id"] == "2301_00001"
    assert raw_lines[0]["ref_title"] == "Ref One"
    assert raw_lines[0]["ref_authors"] == ["Author 1"]
    assert raw_lines[1]["arxiv_id"] == "2501_00001"
    assert raw_lines[1]["ref_arxiv_id"] == ""

    # Verify ledger
    ledger_lines = [json.loads(l) for l in (root / "references_raw_ledger.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(ledger_lines) == 3
    ledger_by_id = {r["arxiv_id"]: r for r in ledger_lines}
    assert ledger_by_id["2501_00001"]["status"] == "ok"
    assert ledger_by_id["2501_00001"]["raw_refs_total"] == 2
    assert ledger_by_id["2501_00001"]["refs_with_arxiv_id"] == 1
    assert ledger_by_id["2501_00002"]["status"] == "ok"
    assert ledger_by_id["2501_00002"]["raw_refs_total"] == 0
    assert ledger_by_id["2501_00003"]["status"] == "not_found"

    # Verify m1_reference_stats.csv
    csv_p = root / "csv" / "m1_reference_stats.csv"
    assert csv_p.is_file()
    with open(csv_p, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 3
    assert rows[0]["arxiv_id"] == "2501.00001"
    assert rows[0]["ref_status"] == "success"
    assert rows[0]["num_crawled_refs"] == "2"
    assert rows[0]["num_arxiv_refs"] == "1"
    assert rows[1]["arxiv_id"] == "2501.00002"
    assert rows[1]["ref_status"] == "success"
    assert rows[1]["num_crawled_refs"] == "0"
    assert rows[2]["arxiv_id"] == "2501.00003"
    assert rows[2]["ref_status"] == "not_found"

    # Second run should skip everything as verified_existing / known_not_found
    second_run = collect_raw_references(
        root,
        ids=["2501.00001", "2501.00002", "2501.00003"],
        max_papers=3,
        batch_size=100,
        dry_run=False,
        client=client,
    )
    assert second_run["summary"]["verified_existing"] == 2
    assert second_run["summary"]["known_not_found"] == 1
    assert len(client.calls) == 1  # No new API calls!


def test_prune_unverified_references_cleans_mismatches(tmp_path):
    root = data(tmp_path)
    # Valid paper
    valid_record = {
        "arxiv_id": "2501_00001", "version": None, "version_attribution": "unknown_s2_publication_level",
        "ref_index": 0, "ref_s2_id": "s1", "ref_arxiv_id": "2401_00001", "ref_title": "T1",
        "ref_authors": ["A"], "ref_submission_date": "", "external_ids": {}, "retrieved_at": "",
        "source_endpoint": "", "raw_cited_paper": {}, "raw_reference_entry": {"citedPaper": {"paperId": "s1"}},
    }
    # Corrupt paper with missing/mismatched rows
    corrupt_record = {
        "arxiv_id": "2501_00002", "version": None, "version_attribution": "unknown_s2_publication_level",
        "ref_index": 0, "ref_s2_id": "s2", "ref_arxiv_id": "2401_00002", "ref_title": "T2",
        "ref_authors": ["B"], "ref_submission_date": "", "external_ids": {}, "retrieved_at": "",
        "source_endpoint": "", "raw_cited_paper": {}, "raw_reference_entry": {"citedPaper": {"paperId": "s2"}},
    }
    (root / "references_raw.jsonl").write_text(
        json.dumps(valid_record) + "\n" + json.dumps(corrupt_record) + "\n",
        encoding="utf-8",
    )
    from src.crawl_references_module.raw_reference_collector import _digest
    (root / "references_raw_ledger.jsonl").write_text(
        json.dumps({
            "arxiv_id": "2501_00001", "status": "ok", "raw_refs_total": 1,
            "payload_sha256": _digest([valid_record]),
        }) + "\n" +
        json.dumps({
            "arxiv_id": "2501_00002", "status": "ok", "raw_refs_total": 5,  # Mismatch: expected 5, only 1 in file!
            "payload_sha256": "fake_sha256",
        }) + "\n",
        encoding="utf-8",
    )

    pruned = prune_unverified_references(root)
    assert pruned == ["2501_00002"]

    # Valid paper preserved
    raw_lines = [json.loads(x) for x in (root / "references_raw.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(raw_lines) == 1
    assert raw_lines[0]["arxiv_id"] == "2501_00001"

    ledger_lines = [json.loads(x) for x in (root / "references_raw_ledger.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(ledger_lines) == 1
    assert ledger_lines[0]["arxiv_id"] == "2501_00001"

