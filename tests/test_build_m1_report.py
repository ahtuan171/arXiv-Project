"""No fabricated benchmark or missing-data percentages in M1 report."""
import csv
import json

import pytest

from src.scripts.build_m1_report import build_m1_report, export_m1_report


def fixture_data(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    (d / "papers.jsonl").write_text("\n".join(json.dumps(p) for p in [
        {"arxiv_id": "2501_00001", "title": "Jan", "versions": ["v1", "v2"]},
        {"arxiv_id": "2502_00002", "title": "Feb", "versions": ["v1"]},
    ]) + "\n")
    (d / "references.jsonl").write_text(
        json.dumps({"arxiv_id": "2501_00001", "ref_arxiv_id": "2301_00001"}) + "\n"
    )
    return d


def test_default_report_is_read_only_and_missing_metrics_are_null(tmp_path):
    d = fixture_data(tmp_path)
    before = {p.relative_to(d).as_posix(): p.read_bytes() for p in d.rglob("*") if p.is_file()}
    report = build_m1_report(d)
    assert report["coverage"]["expected_versions"] == 3
    assert report["integrity_status"] == "in_progress"
    assert report["source_file_statistics"] is None
    assert report["raw_reference_sample"]["mean_raw_references_per_verified_paper"] is None
    assert report["benchmark_evidence"]["official_version_pipeline_metrics"] is None
    assert report["submission_ready"] is False
    assert before == {p.relative_to(d).as_posix(): p.read_bytes() for p in d.rglob("*") if p.is_file()}


def test_report_uses_only_fresh_version_csv_for_file_sizes(tmp_path):
    from src.scripts.audit_version_downloads import audit_version_downloads, export_version_csv
    from src.scripts.audit_m1_integrity import audit_m1_integrity
    d = fixture_data(tmp_path)
    csvfile = export_version_csv(audit_version_downloads(d))
    integrity = audit_m1_integrity(d)
    report = build_m1_report(d, integrity_report=integrity)
    assert report["source_file_statistics"]["verified_tex_versions_in_size_sample"] == 0
    assert report["source_file_statistics"]["mean_downloaded_archive_bytes_per_tex_version"] is None
    assert report["source_file_statistics"]["complete_source_publications"] == 0
    assert report["source_file_statistics"]["publications_in_version_csv"] == 2
    csvfile.write_text(csvfile.read_text().replace("pending", "failed"))
    stale = build_m1_report(d)
    assert stale["source_file_statistics"] is None


def test_legacy_benchmark_is_not_passed_off_as_new_version_measurement(tmp_path):
    d = fixture_data(tmp_path)
    csvdir = d / "csv"
    csvdir.mkdir()
    with (csvdir / "m1_system_benchmark.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=[
            "total_papers", "total_wall_time_sec", "peak_ram_mb",
            "average_ram_mb", "peak_disk_mb", "final_storage_mb",
        ])
        writer.writeheader()
        writer.writerow({"total_papers": 3, "total_wall_time_sec": 99,
                         "peak_ram_mb": 123, "average_ram_mb": 98,
                         "peak_disk_mb": 35, "final_storage_mb": 14})
    report = build_m1_report(d)
    assert report["benchmark_evidence"]["legacy_run_count"] == 1
    assert report["benchmark_evidence"]["official_version_pipeline_metrics"] is None
    assert any("CPU-only" in s for s in report["missing_evidence"])


def test_export_is_only_new_report_file_and_idempotent(tmp_path):
    d = fixture_data(tmp_path)
    report = build_m1_report(d)
    published = (d / "references.jsonl").read_bytes()
    filename = export_m1_report(report)
    first = filename.read_bytes()
    export_m1_report(report)
    assert filename.read_bytes() == first
    assert filename.name == "m1_summary.json"
    assert (d / "references.jsonl").read_bytes() == published


def test_integrity_from_another_drive_path_is_refused(tmp_path):
    d = fixture_data(tmp_path)
    report = build_m1_report(d)
    with pytest.raises(ValueError):
        build_m1_report(tmp_path, integrity_report={
            "data_dir": str(d), "source_versions": report["source_statuses"],
            "coverage": report["coverage"],
        })


def test_invalid_legacy_statistics_are_not_assumed_valid(tmp_path):
    d = fixture_data(tmp_path)
    (d / "csv").mkdir()
    (d / "csv" / "m1_discovery_stats.csv").write_text("not-an-id,status\nhello,success\n")
    with pytest.raises(ValueError, match="Missing columns"):
        build_m1_report(d)
