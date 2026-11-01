"""Offline tests for opt-in raw reference capture and resumability."""
import json
import pytest

from src.crawl_references_module.raw_reference_collector import (
    _fetch_all_pages, collect_raw_references,
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
