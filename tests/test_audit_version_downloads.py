"""Offline reconciliation tests for version-level report and CSV snapshot."""
import csv
import json

from src.download_module.version_downloader import run_version_downloads
from src.scripts.audit_version_downloads import audit_version_downloads, export_version_csv, export_download_stats_csv


def fixture_data(tmp_path):
    data = tmp_path / "data"
    (data / "raw" / "2501_00001").mkdir(parents=True)
    (data / "raw" / "2501_00001" / "legacy.tex").write_text("KEEP LEGACY")
    (data / "csv").mkdir()
    (data / "csv" / "m1_download_stats.csv").write_text("legacy-stats\n")
    (data / "papers.jsonl").write_text("\n".join(json.dumps(p) for p in [
        {"arxiv_id": "2501_00001", "title": "January", "versions": ["v1", "v2"]},
        {"arxiv_id": "2502_00002", "title": "February", "versions": ["v1"]},
    ]) + "\n")
    (data / "references.jsonl").write_text('{"reference":"old"}\n')
    return data


def test_read_only_has_one_row_per_version_across_months(tmp_path):
    data = fixture_data(tmp_path)
    before = {p.relative_to(data).as_posix(): p.read_bytes()
              for p in data.rglob("*") if p.is_file()}
    report = audit_version_downloads(data)
    assert report["summary"]["expected_versions"] == 3
    assert report["summary"]["pending"] == 3
    assert [(r["arxiv_id"], r["version"]) for r in report["rows"]] == [
        ("2501_00001", "v1"), ("2501_00001", "v2"), ("2502_00002", "v1"),
    ]
    assert before == {p.relative_to(data).as_posix(): p.read_bytes()
                      for p in data.rglob("*") if p.is_file()}


def test_reconcile_verified_pdf_and_404_as_separate_versions(tmp_path):
    data = fixture_data(tmp_path)
    run_version_downloads(data, ids=["2501.00001"], max_downloads=2,
                          dry_run=False, scratch_parent=tmp_path,
                          fetch=lambda url: ("not_found", b"", "", "HTTP 404") if url.endswith("v2")
                          else ("ok", b"%PDF-1.4", "application/pdf", ""))
    report = audit_version_downloads(data)
    by_key = {(x["arxiv_id"], x["version"]): x for x in report["rows"]}
    assert by_key[("2501_00001", "v1")]["status"] == "pdf_only"
    assert by_key[("2501_00001", "v1")]["verification"] == "verified"
    assert by_key[("2501_00001", "v1")]["raw_path"] == "raw/2501_00001/v1"
    assert by_key[("2501_00001", "v2")]["status"] == "not_found"
    assert by_key[("2502_00002", "v1")]["status"] == "pending"
    assert report["summary"]["verified_pdf_only"] == 1
    assert report["summary"]["not_found"] == 1


def test_missing_published_folder_is_not_counted_as_success(tmp_path):
    data = fixture_data(tmp_path)
    (data / "versions_manifest.jsonl").write_text(json.dumps({
        "arxiv_id": "2501_00001", "version": "v1", "status": "success",
    }) + "\n")
    report = audit_version_downloads(data)
    assert report["summary"]["missing_files"] == 1
    assert report["rows"][0]["status"] == "missing_files"
    assert report["rows"][0]["verification"] == "missing"


def test_unverified_existing_folder_is_conflict_without_overwrite(tmp_path):
    data = fixture_data(tmp_path)
    folder = data / "raw" / "2501_00001" / "v1"
    folder.mkdir()
    (folder / "main.tex").write_text("untrusted")
    report = audit_version_downloads(data)
    assert report["summary"]["conflicts"] == 1
    assert report["rows"][0]["status"] == "conflict"
    assert (folder / "main.tex").read_text() == "untrusted"


def test_marker_survives_missing_manifest(tmp_path):
    data = fixture_data(tmp_path)
    run_version_downloads(data, ids=["2501.00001"], max_downloads=1,
                          dry_run=False, scratch_parent=tmp_path,
                          fetch=lambda _: ("ok", b"%PDF-1.4", "application/pdf", ""))
    (data / "versions_manifest.jsonl").unlink()
    report = audit_version_downloads(data)
    assert report["rows"][0]["status"] == "pdf_only"
    assert report["rows"][0]["verification"] == "verified"
    assert report["rows"][0]["manifest_status"] == ""


def test_export_csv_preserves_both_months_and_legacy_files(tmp_path):
    data = fixture_data(tmp_path)
    report = audit_version_downloads(data)
    path = export_version_csv(report)
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 3
    assert {r["arxiv_id"][:4] for r in rows} == {"2501", "2502"}
    assert len({(r["arxiv_id"], r["version"]) for r in rows}) == 3
    assert (data / "csv" / "m1_download_stats.csv").read_text() == "legacy-stats\n"
    assert (data / "raw" / "2501_00001" / "legacy.tex").read_text() == "KEEP LEGACY"
    assert (data / "references.jsonl").read_text() == '{"reference":"old"}\n'
    export_version_csv(report)
    with path.open(encoding="utf-8", newline="") as handle:
        assert len(list(csv.DictReader(handle))) == 3


def test_latest_manifest_event_is_used_and_orphans_reported(tmp_path):
    data = fixture_data(tmp_path)
    (data / "versions_manifest.jsonl").write_text("\n".join(json.dumps(x) for x in [
        {"arxiv_id": "2501_00001", "version": "v2", "status": "failed", "error": "HTTP 503"},
        {"arxiv_id": "2501_00001", "version": "v2", "status": "not_found", "error": "HTTP 404"},
        {"arxiv_id": "2501_00001", "version": "v99", "status": "not_found"},
    ]) + "\n")
    report = audit_version_downloads(data)
    assert report["summary"]["not_found"] == 1
    assert report["summary"]["orphan_manifest_keys"] == 1
    assert report["rows"][1]["status"] == "not_found"

def test_export_download_stats_csv_creates_table2(tmp_path):
    data = fixture_data(tmp_path)
    (data / 'csv' / 'm1_download_stats.csv').unlink()
    report = audit_version_downloads(data)
    csv_path = export_download_stats_csv(report)
    assert csv_path == data / 'csv' / 'm1_download_stats.csv'
    assert csv_path.is_file()
    with csv_path.open(encoding='utf-8', newline='') as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 2
    assert [r['arxiv_id'] for r in rows] == ['2501.00001', '2502.00002']
    assert set(rows[0].keys()) == {
        'arxiv_id', 'download_status', 'has_tex',
        'raw_size_bytes', 'cleaned_size_bytes', 'num_tex_files', 'download_time_sec'
    }


def test_audit_version_downloads_scoped_by_ids(tmp_path):
    data = fixture_data(tmp_path)
    # When scoped to only 2501.00001, 2502.00002 should not be audited
    report = audit_version_downloads(data, ids=["2501.00001"])
    assert report["summary"]["expected_versions"] == 2
    assert [r["arxiv_id"] for r in report["rows"]] == ["2501_00001", "2501_00001"]


