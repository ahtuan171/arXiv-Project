"""Offline tests for Milestone 1 read-only dataset integrity checks."""
import io
import json
import tarfile

from src.download_module.version_downloader import run_version_downloads
from src.crawl_references_module.raw_reference_collector import _digest
from src.scripts.audit_m1_integrity import audit_m1_integrity, export_integrity_report
from src.scripts.audit_version_downloads import audit_version_downloads, export_version_csv
from src.scripts.build_version_metadata import build_version_metadata, export_version_metadata


def fixture(tmp_path):
    d = tmp_path / "data"
    (d / "raw").mkdir(parents=True)
    (d / "papers.jsonl").write_text("\n".join(json.dumps(r) for r in [
        {"arxiv_id": "2501_00001", "title": "January", "versions": ["v1", "v2"],
         "submission_date": "2025-01-01", "revised_dates": ["2025-01-03"]},
        {"arxiv_id": "2502_00002", "title": "February", "versions": ["v1"],
         "submission_date": "2025-02-01"},
    ]) + "\n")
    (d / "references.jsonl").write_text(json.dumps({
        "arxiv_id": "2501_00001", "ref_arxiv_id": "2301_00001",
    }) + "\n")
    return d


def source_archive():
    buf = io.BytesIO()
    tex = b"\\documentclass{article}\\begin{document}Test\\end{document}"
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        item = tarfile.TarInfo("main.tex")
        item.size = len(tex)
        tar.addfile(item, io.BytesIO(tex))
    return buf.getvalue()


def verified_versions(root, tmp_path):
    run_version_downloads(
        root, ids=["2501.00001", "2502.00002"], max_downloads=0,
        dry_run=False, scratch_parent=tmp_path,
        fetch=lambda _: ("ok", source_archive(), "application/gzip", ""),
    )


def complete_refs(root):
    refs = [
        {"arxiv_id": "2501_00001", "version": None,
         "version_attribution": "unknown_s2_publication_level",
         "ref_index": 0, "ref_arxiv_id": "2301_00001",
         "raw_reference_entry": {"citedPaper": {"paperId": "a"}}},
        {"arxiv_id": "2501_00001", "version": None,
         "version_attribution": "unknown_s2_publication_level",
         "ref_index": 1, "ref_arxiv_id": "",
         "raw_reference_entry": {"citedPaper": None}},
    ]
    (root / "references_raw.jsonl").write_text(
        "".join(json.dumps(x) + "\n" for x in refs)
    )
    (root / "references_raw_ledger.jsonl").write_text(
        json.dumps({
            "arxiv_id": "2501_00001", "status": "ok",
            "raw_refs_total": len(refs), "payload_sha256": _digest(refs),
            "completeness": "confirmed_total",
        }) + "\n" + json.dumps({
            "arxiv_id": "2502_00002", "status": "not_found", "raw_refs_total": 0,
        }) + "\n"
    )


def test_empty_downloads_are_incomplete_not_corrupt(tmp_path):
    root = fixture(tmp_path)
    audit = audit_m1_integrity(root)
    assert audit["status"] == "in_progress"
    assert audit["blocker_count"] == 0
    assert audit["coverage"]["expected_versions"] == 3
    assert audit["coverage"]["verified_source_versions"] == 0
    assert audit["raw_references"]["unresolved_papers"] == 2
    assert not (root / "reports").exists()


def test_consistent_two_month_data_and_progress(tmp_path):
    root = fixture(tmp_path)
    verified_versions(root, tmp_path)
    complete_refs(root)
    export_version_metadata(build_version_metadata(root))
    export_version_csv(audit_version_downloads(root))
    points = []
    audit = audit_m1_integrity(root, progress_callback=lambda n, total, _: points.append((n, total)))
    assert audit["status"] == "structurally_consistent"
    assert audit["coverage"]["verified_source_versions"] == 3
    assert audit["raw_references"]["verified_papers"] == 1
    assert audit["raw_references"]["not_found_papers"] == 1
    assert audit["raw_references"]["without_arxiv_id"] == 1
    assert audit["raw_references"]["overlap_with_published_pairs"] == 1
    assert audit["snapshots"]["download_versions_csv_current"]
    assert points == [(1, 3), (2, 3), (3, 3)]


def test_tampered_tex_is_blocker(tmp_path):
    root = fixture(tmp_path)
    verified_versions(root, tmp_path)
    (root / "raw" / "2501_00001" / "v1" / "main.tex").write_text("tampered")
    audit = audit_m1_integrity(root)
    assert audit["status"] == "needs_fix"
    assert audit["source_versions"]["conflicts"] == 1


def test_raw_ledger_mismatch_is_blocker(tmp_path):
    root = fixture(tmp_path)
    complete_refs(root)
    ledger = root / "references_raw_ledger.jsonl"
    original = ledger.read_text()
    ledger.write_text(original.replace(json.loads(original.splitlines()[0])["payload_sha256"], "invalid"))
    audit = audit_m1_integrity(root)
    assert audit["status"] == "needs_fix"
    assert any("ledger count/hash" in line for line in audit["blockers"])


def test_duplicate_published_edges_is_blocker(tmp_path):
    root = fixture(tmp_path)
    p = root / "references.jsonl"
    p.write_text(p.read_text() * 2)
    audit = audit_m1_integrity(root)
    assert audit["status"] == "needs_fix"
    assert audit["published_references"]["duplicate_pairs"] == 1


def test_stale_csv_does_not_corrupt_original_data(tmp_path):
    root = fixture(tmp_path)
    csvfile = export_version_csv(audit_version_downloads(root))
    csvfile.write_text(csvfile.read_text().replace("pending", "failed"))
    audit = audit_m1_integrity(root)
    assert audit["status"] == "in_progress"
    assert audit["snapshots"]["download_versions_csv_current"] is False
    assert audit["warning_count"] >= 1


def test_raw_refs_never_claim_revision_provenance(tmp_path):
    root = fixture(tmp_path)
    complete_refs(root)
    path = root / "references_raw.jsonl"
    path.write_text(path.read_text().replace('"version": null', '"version": "v2"', 1))
    audit = audit_m1_integrity(root)
    assert audit["status"] == "needs_fix"
    assert any("unproven version" in x for x in audit["blockers"])


def test_malformed_raw_rows_fail_closed(tmp_path):
    root = fixture(tmp_path)
    (root / "references_raw.jsonl").write_text("{partial")
    audit = audit_m1_integrity(root)
    assert audit["status"] == "needs_fix"
    assert any("unreadable" in x for x in audit["blockers"])


def test_report_write_is_opt_in_and_preserves_published_files(tmp_path):
    root = fixture(tmp_path)
    previous = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    report = audit_m1_integrity(root)
    assert previous == {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    output = export_integrity_report(report)
    assert output == root / "reports" / "m1_integrity_report.json"
    assert json.loads(output.read_text())["status"] == "in_progress"
    for name, content in previous.items():
        assert (root / name).read_bytes() == content
