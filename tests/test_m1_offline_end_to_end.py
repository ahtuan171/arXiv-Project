"""Offline cross-month pipeline regression. No Drive, no external network."""
import io
import json
import socket
import tarfile
import pytest

from src.crawl_references_module.raw_reference_collector import collect_raw_references
from src.download_module.version_downloader import run_version_downloads
from src.scripts.audit_version_downloads import audit_version_downloads, export_version_csv
from src.scripts.build_version_metadata import build_version_metadata, export_version_metadata
from src.scripts.audit_m1_integrity import audit_m1_integrity


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Unexpected network access in offline M1 test")
    monkeypatch.setattr(socket.socket, "connect", blocked)


def fixture_data(tmp_path):
    d = tmp_path / "data"
    (d / "raw" / "2501_00001").mkdir(parents=True)
    (d / "raw" / "2501_00001" / "legacy.tex").write_text("DO NOT MODIFY")
    (d / "papers.jsonl").write_text("\n".join(json.dumps(p) for p in [
        {"arxiv_id": "2501_00001", "title": "Jan", "versions": ["v1", "v2"],
         "submission_date": "2025-01-01", "revised_dates": ["2025-01-03"]},
        {"arxiv_id": "2502_00002", "title": "Feb", "versions": ["v1"],
         "submission_date": "2025-02-01"},
    ]) + "\n")
    (d / "references.jsonl").write_text(json.dumps({
        "arxiv_id": "2501_00001", "ref_arxiv_id": "2401_00002",
    }) + "\n")
    return d


def archive():
    b = io.BytesIO()
    data = b"\\documentclass{article}\\begin{document}Offline\\end{document}"
    with tarfile.open(fileobj=b, mode="w:gz") as tar:
        info = tarfile.TarInfo("main.tex")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return b.getvalue()


class FakeS2:
    page_size = 2
    def __init__(self):
        self.calls = []
    async def _request(self, method, path, *, params):
        assert method == "GET"
        key = path.split("/paper/arXiv:", 1)[-1].split("/references", 1)[0]
        offset = params["offset"]
        self.calls.append((key, offset))
        if key == "2501.00001" and offset == 0:
            return {"total": 2, "next": 2, "data": [
                {"citedPaper": {"paperId": "A", "externalIds": {"ArXiv": "2401.00002"}}},
                {"citedPaper": {"paperId": "B", "title": "No arXiv ID"}},
            ]}
        if key == "2501.00001" and offset == 2:
            return {"total": 2, "next": None, "data": []}
        if key == "2502.00002" and offset == 0:
            return {"total": 0, "next": None, "data": []}
        raise AssertionError(f"Unplanned request: {key} offset={offset}")


def test_offline_full_pipeline_resumes_without_duplicates(tmp_path):
    root = fixture_data(tmp_path)
    ids = ["2501.00001", "2502.00002"]
    before = {p.relative_to(root).as_posix(): p.read_bytes()
              for p in root.rglob("*") if p.is_file()}
    assert run_version_downloads(root, ids=ids, dry_run=True)["plan"]["summary"]["to_download"] == 3
    assert collect_raw_references(root, ids=ids, dry_run=True)["summary"]["to_fetch"] == 2
    assert before == {p.relative_to(root).as_posix(): p.read_bytes()
                      for p in root.rglob("*") if p.is_file()}

    calls = []
    def fake_source(url):
        calls.append(url)
        return "ok", archive(), "application/gzip", ""
    downloaded = run_version_downloads(
        root, ids=ids, dry_run=False, max_downloads=3, scratch_parent=tmp_path, fetch=fake_source
    )
    assert downloaded["summary"]["success"] == 3
    client = FakeS2()
    collected = collect_raw_references(root, ids=ids, max_papers=2, dry_run=False, client=client)
    assert collected["summary"]["ok"] == 2
    raw = [json.loads(x) for x in (root / "references_raw.jsonl").read_text().splitlines()]
    assert len(raw) == 2
    assert raw[1]["ref_arxiv_id"] == ""
    assert all(r["version"] is None for r in raw)

    export_version_metadata(build_version_metadata(root))
    export_version_csv(audit_version_downloads(root))
    summary = audit_m1_integrity(root)
    assert summary["status"] == "structurally_consistent"
    assert summary["coverage"]["verified_source_versions"] == 3
    assert summary["raw_references"]["verified_papers"] == 2
    assert summary["raw_references"]["without_arxiv_id"] == 1
    assert summary["snapshots"]["metadata_versions_current"]
    assert summary["snapshots"]["download_versions_csv_current"]

    old_manifest = (root / "versions_manifest.jsonl").read_bytes()
    old_raw = (root / "references_raw.jsonl").read_bytes()
    old_ledger = (root / "references_raw_ledger.jsonl").read_bytes()
    again = run_version_downloads(
        root, ids=ids, max_downloads=3, dry_run=False, scratch_parent=tmp_path, fetch=fake_source
    )
    assert again["plan"]["summary"]["verified_existing"] == 3
    assert collect_raw_references(
        root, ids=ids, max_papers=2, dry_run=False, client=client
    )["summary"]["verified_existing"] == 2
    assert len(calls) == 3
    assert len(client.calls) == 3
    assert (root / "versions_manifest.jsonl").read_bytes() == old_manifest
    assert (root / "references_raw.jsonl").read_bytes() == old_raw
    assert (root / "references_raw_ledger.jsonl").read_bytes() == old_ledger
    for name, raw_bytes in before.items():
        assert (root / name).read_bytes() == raw_bytes
