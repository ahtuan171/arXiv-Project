import pytest
"""Offline tests for read-only legacy source comparison."""
import io
import json
import tarfile

from src.download_module.version_downloader import run_version_downloads
from src.scripts.audit_legacy_matches import audit_legacy_matches


def source_archive(contents: bytes) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(mode="w:gz", fileobj=buf) as tar:
        item = tarfile.TarInfo("main.tex")
        item.size = len(contents)
        tar.addfile(item, io.BytesIO(contents))
    return buf.getvalue()


def dataset(tmp_path):
    data = tmp_path / "data"
    (data / "raw" / "2501_00001").mkdir(parents=True)
    (data / "raw" / "2502_00002").mkdir(parents=True)
    papers = [
        {"arxiv_id": "2501_00001", "title": "Jan", "versions": ["v1", "v2"]},
        {"arxiv_id": "2502_00002", "title": "Feb", "versions": ["v1"]},
    ]
    (data / "papers.jsonl").write_text(
        "\n".join(json.dumps(p) for p in papers) + "\n"
    )
    (data / "raw" / "2501_00001" / "main.tex").write_bytes(
        b"\\documentclass{article}\\begin{document}ONE\\end{document}"
    )
    return data


def add_version(data, tmp_path, version_count=1, same=True):
    tex = (b"\\documentclass{article}\\begin{document}ONE\\end{document}"
           if same else
           b"\\documentclass{article}\\begin{document}DIFFERENT\\end{document}")
    return run_version_downloads(
        data, ids=["2501.00001"], max_downloads=version_count,
        dry_run=False, scratch_parent=tmp_path,
        fetch=lambda _: ("ok", source_archive(tex), "application/gzip", ""),
    )


def test_exact_match_without_modifications(tmp_path):
    data = dataset(tmp_path)
    add_version(data, tmp_path)
    before = {p.relative_to(data).as_posix(): p.read_bytes()
              for p in data.rglob("*") if p.is_file()}
    result = audit_legacy_matches(data, ids=["2501.00001"], max_papers=20)
    assert result["summary"]["exact_matches"] == 1
    assert result["rows"][0]["matched_versions"] == ["v1"]
    assert before == {p.relative_to(data).as_posix(): p.read_bytes()
                      for p in data.rglob("*") if p.is_file()}


def test_different_source_does_not_match(tmp_path):
    data = dataset(tmp_path)
    add_version(data, tmp_path, same=False)
    result = audit_legacy_matches(data, ids=["2501.00001"])
    assert result["summary"]["different"] == 1
    assert result["rows"][0]["matched_versions"] == []


def test_multiple_identical_versions_are_ambiguous(tmp_path):
    data = dataset(tmp_path)
    add_version(data, tmp_path, version_count=2)
    report = audit_legacy_matches(data, ids=["2501.00001"])
    assert report["summary"]["ambiguous_matches"] == 1
    assert report["rows"][0]["matched_versions"] == ["v1", "v2"]


def test_legacy_without_version_proof_is_not_automatically_assigned(tmp_path):
    data = dataset(tmp_path)
    report = audit_legacy_matches(data, ids=["2501.00001"])
    assert report["summary"]["no_verified_version"] == 1
    assert report["rows"][0]["matched_versions"] == []
    assert not (data / "raw" / "2501_00001" / "v2").exists()


def test_only_version_folders_do_not_count_as_legacy_source(tmp_path):
    data = dataset(tmp_path)
    add_version(data, tmp_path)
    (data / "raw" / "2501_00001" / "main.tex").unlink()
    result = audit_legacy_matches(data, ids=["2501.00001"])
    assert result["summary"]["no_legacy"] == 1


def test_pdf_only_legacy_is_not_proof_of_version(tmp_path):
    data = dataset(tmp_path)
    (data / "raw" / "2501_00001" / "main.tex").unlink()
    (data / "raw" / "2501_00001" / ".source-status.json").write_text(
        json.dumps({"status": "pdf_only"})
    )
    result = audit_legacy_matches(data, ids=["2501.00001"])
    assert result["summary"]["legacy_pdf_only"] == 1
    assert result["summary"]["exact_matches"] == 0


def test_mixed_month_scope_and_limit(tmp_path):
    data = dataset(tmp_path)
    report = audit_legacy_matches(data, max_papers=1)
    assert report["summary"]["requested_papers"] == 2
    assert report["summary"]["examined_papers"] == 1
    both = audit_legacy_matches(data, max_papers=0)
    assert both["summary"]["examined_papers"] == 2


def test_symlink_or_extra_file_cannot_prove_match(tmp_path):
    data = dataset(tmp_path)
    add_version(data, tmp_path)
    legacy = data / "raw" / "2501_00001"
    (legacy / "unexpected.zip").write_bytes(b"bad")
    assert audit_legacy_matches(data, ids=["2501.00001"])["summary"]["untrusted"] == 1
    (legacy / "unexpected.zip").unlink()
    try:
        (legacy / "linked.tex").symlink_to(legacy / "main.tex")
        assert audit_legacy_matches(data, ids=["2501.00001"])["summary"]["untrusted"] == 1
    except OSError:
        pytest.skip("Symlink creation not permitted on this system")


def test_unverified_version_does_not_prove_match(tmp_path):
    data = dataset(tmp_path)
    untrusted = data / "raw" / "2501_00001" / "v1"
    untrusted.mkdir()
    (untrusted / "main.tex").write_bytes(
        (data / "raw" / "2501_00001" / "main.tex").read_bytes()
    )
    report = audit_legacy_matches(data, ids=["2501.00001"])
    assert report["summary"]["no_verified_version"] == 1
