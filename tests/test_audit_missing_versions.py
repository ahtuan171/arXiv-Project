"""Lightweight version audit tests: snapshots/ledgers only, no raw/ traversal."""
import csv
import json

from src.scripts.audit_missing_versions import audit_missing_versions


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["arxiv_id", "discovery_status", "num_versions"] if "discovery" in path.name else ["arxiv_id", "download_status"])
        writer.writeheader()
        writer.writerows(rows)


def seed(root):
    write_jsonl(root / "papers.jsonl", [
        {"arxiv_id": "2501_00001", "title": "A", "versions": ["v1", "v2", "v3"]},
        {"arxiv_id": "2501_00002", "title": "B", "versions": ["v1"]},
        {"arxiv_id": "2501_00003", "error": "404"},
    ])
    write_csv(root / "csv" / "m1_discovery_stats.csv", [
        {"arxiv_id": "2501.00001", "discovery_status": "success", "num_versions": "3"},
        {"arxiv_id": "2501.00002", "discovery_status": "success", "num_versions": "1"},
        {"arxiv_id": "2501.00003", "discovery_status": "not_found", "num_versions": "0"},
    ])
    write_csv(root / "csv" / "m1_download_stats.csv", [
        {"arxiv_id": "2501_00001", "download_status": "success"},
        {"arxiv_id": "2501_00002", "download_status": "failed"},
    ])


def test_audit_counts_and_read_only(tmp_path):
    root = tmp_path / "data"
    seed(root)
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    report = audit_missing_versions(root)
    s = report["summary"]
    assert s["valid_metadata_papers"] == 2
    assert s["expected_versions"] == 4
    assert s["multi_version_papers"] == 1
    assert s["earlier_versions_candidates"] == 2
    assert s["latest_needs_attention"] == 1
    assert s["legacy_latest_unverified"] == 1
    assert s["candidate_missing_versions"] == 3
    assert {p: p.read_bytes() for p in root.rglob("*") if p.is_file()} == before


def test_ledger_overrides_download_csv_and_scope(tmp_path):
    root = tmp_path / "data"
    seed(root)
    write_jsonl(root / "csv" / "m1_download_stats.ledger.jsonl", [
        {"arxiv_id": "2501_00001", "status": "error"},
    ])
    report = audit_missing_versions(root, ids=["2501.00001"])
    assert report["summary"]["requested_ids"] == 1
    assert report["summary"]["legacy_latest_unverified"] == 0
    assert report["summary"]["candidate_missing_versions"] == 3
    assert report["summary"]["legacy_download_statuses"]["failed"] == 1


def test_rejects_untrusted_versions(tmp_path):
    root = tmp_path / "data"
    seed(root)
    write_jsonl(root / "papers.jsonl.journal", [
        {"arxiv_id": "2501_00001", "title": "A", "versions": ["v1", "v3"]},
    ])
    report = audit_missing_versions(root, ids=["2501.00001"])
    assert report["summary"]["unknown_version_papers"] == 1
    assert report["summary"]["candidate_missing_versions"] == 0
    assert any("not consecutive" in w for w in report["warnings"])


def test_missing_metadata_file_fails_without_side_effect(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    try:
        audit_missing_versions(root)
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("Expected missing metadata error")
    assert list(root.iterdir()) == []
