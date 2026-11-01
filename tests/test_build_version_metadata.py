"""Version metadata projection must preserve provenance and old files."""
import json

import pytest

from src.scripts.build_version_metadata import build_version_metadata, export_version_metadata


def fixture_data(tmp_path, records):
    root = tmp_path / "data"
    root.mkdir()
    (root / "papers.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )
    (root / "references.jsonl").write_text('{"keep":"untouched"}\n')
    (root / "raw").mkdir()
    return root


def test_first_and_latest_dates_but_no_middle_guess(tmp_path):
    root = fixture_data(tmp_path, [{
        "arxiv_id": "2501_00001", "title": "Example",
        "versions": ["v1", "v2", "v3", "v4"],
        "submission_date": "2025-01-02",
        "revised_dates": ["2025-03-04"],
    }])
    report = build_version_metadata(root)
    assert [(r["version"], r["version_date"], r["date_source"]) for r in report["rows"]] == [
        ("v1", "2025-01-02", "paper_submission_date"),
        ("v2", "", "unknown"),
        ("v3", "", "unknown"),
        ("v4", "2025-03-04", "paper_latest_updated_date"),
    ]
    assert report["summary"]["unknown_version_dates"] == 2
    assert not (root / "metadata_versions.jsonl").exists()


def test_complete_revision_date_list_is_mapped_by_position(tmp_path):
    root = fixture_data(tmp_path, [{
        "arxiv_id": "2502_00002", "title": "Two",
        "versions": ["v1", "v2", "v3"],
        "submission_date": "2025-02-01",
        "revised_dates": ["2025-02-05", "2025-02-11"],
    }])
    rows = build_version_metadata(root)["rows"]
    assert [r["version_date"] for r in rows] == ["2025-02-01", "2025-02-05", "2025-02-11"]
    assert rows[-1]["date_source"] == "paper_revised_dates_ordered"


def test_invalid_date_never_becomes_fact(tmp_path):
    root = fixture_data(tmp_path, [{
        "arxiv_id": "2501_00001", "title": "Broken date",
        "versions": ["v1", "v2"],
        "submission_date": "2025-02-30",
        "revised_dates": ["not-a-date"],
    }])
    rows = build_version_metadata(root)["rows"]
    assert all(row["version_date"] == "" and not row["date_known"] for row in rows)


def test_ambiguous_revised_dates_left_unknown(tmp_path):
    root = fixture_data(tmp_path, [{
        "arxiv_id": "2501_00001", "title": "Dates",
        "versions": ["v1", "v2", "v3", "v4"],
        "submission_date": "2025-01-01",
        "revised_dates": ["2025-01-02", "2025-02-02"],
    }])
    r = build_version_metadata(root)
    assert r["summary"]["ambiguous_revised_dates"] == 1
    assert [row["version_date"] for row in r["rows"]] == ["2025-01-01", "", "", ""]


def test_full_dataset_export_is_idempotent_and_preserves_legacy(tmp_path):
    root = fixture_data(tmp_path, [
        {"arxiv_id": "2501_00001", "title": "Jan", "versions": ["v1"]},
        {"arxiv_id": "2502_00002", "title": "Feb", "versions": ["v1", "v2"]},
    ])
    report = build_version_metadata(root)
    assert report["summary"]["expected_versions"] == 3
    p = export_version_metadata(report)
    first = p.read_bytes()
    export_version_metadata(build_version_metadata(root))
    assert p.read_bytes() == first
    assert {json.loads(line)["arxiv_id"][:4] for line in p.read_text().splitlines()} == {"2501", "2502"}
    assert (root / "references.jsonl").read_text() == '{"keep":"untouched"}\n'


def test_invalid_versions_not_exported(tmp_path):
    root = fixture_data(tmp_path, [
        {"arxiv_id": "2501_00001", "title": "Okay", "versions": ["v1"]},
        {"arxiv_id": "2502_00002", "title": "Invalid", "versions": ["v2"]},
    ])
    r = build_version_metadata(root)
    assert r["summary"]["invalid_version_metadata"] == 1
    assert r["summary"]["expected_versions"] == 1


def test_export_rejects_symlink(tmp_path):
    root = fixture_data(tmp_path, [
        {"arxiv_id": "2501_00001", "title": "One", "versions": ["v1"]},
    ])
    (root / "metadata_versions.jsonl").symlink_to(root / "papers.jsonl")
    with pytest.raises(ValueError):
        export_version_metadata(build_version_metadata(root))
    assert json.loads((root / "papers.jsonl").read_text().splitlines()[0])["title"] == "One"


def test_journal_updates_versions_without_modifying_snapshot(tmp_path):
    root = fixture_data(tmp_path, [
        {"arxiv_id": "2501_00001", "title": "Old", "versions": ["v1"]},
    ])
    original = (root / "papers.jsonl").read_bytes()
    (root / "papers.jsonl.journal").write_text(json.dumps({
        "arxiv_id": "2501_00001", "title": "New", "versions": ["v1", "v2"]
    }) + "\n")
    report = build_version_metadata(root)
    assert report["summary"]["expected_versions"] == 2
    assert (root / "papers.jsonl").read_bytes() == original
