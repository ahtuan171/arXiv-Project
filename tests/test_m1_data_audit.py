"""Tests for the read-only Milestone 1 audit."""
import csv
import json

from src.scripts.audit_m1 import audit, canonical_id, valid_id


def _write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def _write_csv(path, fields, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def test_normalize_ids():
    assert canonical_id("arXiv:2501.12345v3") == "2501_12345"
    assert canonical_id("cs/0101001v2") == "cs_0101001"
    assert valid_id("2501_12345")
    assert valid_id("cs_0101001")
    assert not valid_id("missing")


def test_audit_uses_scope_and_keeps_data_readonly(tmp_path):
    _write_jsonl(tmp_path / "papers.jsonl", [
        {"arxiv_id": "2501_00001", "title": "A", "versions": ["v1", "v2"]},
        {"arxiv_id": "2501_00002", "error": "not_found"},
    ])
    _write_jsonl(tmp_path / "references.jsonl", [
        {"arxiv_id": "2501_00001", "ref_arxiv_id": "2401_00001"},
    ])
    _write_csv(tmp_path / "csv" / "m1_discovery_stats.csv",
               ["arxiv_id", "discovery_status", "num_versions"],
               [{"arxiv_id": "2501.00001", "discovery_status": "success", "num_versions": "2"},
                {"arxiv_id": "2501.00002", "discovery_status": "not_found", "num_versions": "0"}])
    _write_csv(tmp_path / "csv" / "m1_download_stats.csv",
               ["arxiv_id", "download_status"], [])
    _write_csv(tmp_path / "csv" / "m1_reference_stats.csv",
               ["arxiv_id", "ref_status"], [])
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    result = audit(tmp_path, "2501.00001", "2501.00001")
    after = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert result["discovery_success_in_scope"] == 1
    assert result["expected_versions_in_scope"] == 2
    assert result["multi_version_publications_in_scope"] == 1
    assert result["published_reference_pairs_unique"] == 1
    assert result["issues"] == []
    assert before == after


def test_audit_detects_duplicate_edges(tmp_path):
    _write_jsonl(tmp_path / "papers.jsonl", [])
    ref = {"arxiv_id": "2501_00001", "ref_arxiv_id": "2401_00001"}
    _write_jsonl(tmp_path / "references.jsonl", [ref, ref])
    for name in ("m1_discovery_stats.csv", "m1_download_stats.csv", "m1_reference_stats.csv"):
        _write_csv(tmp_path / "csv" / name, ["arxiv_id"], [])
    result = audit(tmp_path)
    assert result["duplicate_reference_edges"] == 1
    assert any("duplicate published reference edges" in s for s in result["issues"])
