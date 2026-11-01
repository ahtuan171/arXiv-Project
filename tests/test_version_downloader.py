"""Offline tests for append-only per-version download and recovery."""
import io
import json
import tarfile

from src.download_module.version_downloader import plan_version_downloads, run_version_downloads


def fixture_data(tmp_path):
    data = tmp_path / "data"
    (data / "raw" / "2501_00001").mkdir(parents=True)
    (data / "raw" / "2501_00001" / "legacy.tex").write_text("DO NOT CHANGE")
    (data / "papers.jsonl").write_text(json.dumps({
        "arxiv_id": "2501_00001", "title": "Example", "versions": ["v1", "v2"]
    }) + "\n")
    (data / "references.jsonl").write_text('{"reference":"old"}\n')
    return data


def tex_archive():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b"\\documentclass{article}\\begin{document}Hello\\end{document}"
        info = tarfile.TarInfo("main.tex")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_dry_run_is_read_only(tmp_path):
    data = fixture_data(tmp_path)
    before = {p.relative_to(data).as_posix(): p.read_bytes() for p in data.rglob("*") if p.is_file()}
    report = run_version_downloads(data, ids=["2501.00001"], dry_run=True)
    assert report["plan"]["summary"]["to_download"] == 2
    assert report["summary"]["downloaded"] == 0
    assert before == {p.relative_to(data).as_posix(): p.read_bytes() for p in data.rglob("*") if p.is_file()}


def test_publishes_two_versions_then_skips_without_overwriting(tmp_path):
    data = fixture_data(tmp_path)
    calls = []
    def fetch(url):
        calls.append(url)
        return "ok", tex_archive(), "application/gzip", ""
    first = run_version_downloads(data, ids=["2501.00001"], dry_run=False, max_downloads=2,
                                  scratch_parent=tmp_path, fetch=fetch)
    assert len(calls) == 2
    assert calls == ["https://arxiv.org/src/2501.00001v1", "https://arxiv.org/src/2501.00001v2"]
    assert first["summary"]["success"] == 2
    assert (data / "raw" / "2501_00001" / "legacy.tex").read_text() == "DO NOT CHANGE"
    assert (data / "references.jsonl").read_text() == '{"reference":"old"}\n'
    assert (data / "raw" / "2501_00001" / "v1" / "main.tex").is_file()
    assert (data / "raw" / "2501_00001" / "v2" / ".version-status.json").is_file()
    second = run_version_downloads(data, ids=["2501.00001"], dry_run=False, max_downloads=2,
                                   scratch_parent=tmp_path, fetch=fetch)
    assert second["plan"]["summary"]["verified_existing"] == 2
    assert len(calls) == 2


def test_existing_unverified_version_reports_conflict(tmp_path):
    data = fixture_data(tmp_path)
    folder = data / "raw" / "2501_00001" / "v1"
    folder.mkdir()
    (folder / "main.tex").write_text("untrusted")
    plan = plan_version_downloads(data, ids=["2501.00001"])
    assert plan["summary"]["conflicts"] == 1
    report = run_version_downloads(data, ids=["2501.00001"], dry_run=False,
                                   max_downloads=1, scratch_parent=tmp_path,
                                   fetch=lambda _: ("ok", tex_archive(), "", ""))
    assert (folder / "main.tex").read_text() == "untrusted"
    assert report["summary"]["conflicts"] == 1


def test_corrupt_verified_version_never_overwritten(tmp_path):
    data = fixture_data(tmp_path)
    run_version_downloads(data, ids=["2501.00001"], max_downloads=1, dry_run=False,
                          scratch_parent=tmp_path,
                          fetch=lambda _: ("ok", tex_archive(), "", ""))
    v1 = data / "raw" / "2501_00001" / "v1" / "main.tex"
    v1.write_text("tampered")
    plan = plan_version_downloads(data, ids=["2501.00001"])
    assert plan["summary"]["conflicts"] == 1
    assert plan["summary"]["to_download"] == 1
    assert v1.read_text() == "tampered"


def test_pdf_only_marker_and_resume(tmp_path):
    data = fixture_data(tmp_path)
    report = run_version_downloads(data, ids=["2501.00001"], max_downloads=1,
                                   dry_run=False, scratch_parent=tmp_path,
                                   fetch=lambda _: ("ok", b"%PDF-1.4", "application/pdf", ""))
    assert report["summary"]["pdf_only"] == 1
    assert plan_version_downloads(data, ids=["2501.00001"])["summary"]["verified_existing"] == 1


def test_network_failure_does_not_create_version_folder(tmp_path):
    data = fixture_data(tmp_path)
    result = run_version_downloads(data, ids=["2501.00001"], max_downloads=1,
                                   dry_run=False, scratch_parent=tmp_path,
                                   fetch=lambda _: ("not_found", b"", "", "404"))
    assert result["summary"]["not_found"] == 1
    assert not (data / "raw" / "2501_00001" / "v1").exists()
    assert (data / "versions_manifest.jsonl").is_file()


def test_progress_callback_tracks_actual_attempts(tmp_path):
    data = fixture_data(tmp_path)
    progress = []
    def fetch(url):
        if url.endswith("v1"):
            return "ok", b"%PDF-1.4", "application/pdf", ""
        return "not_found", b"", "", "404"

    report = run_version_downloads(
        data, ids=["2501.00001"], dry_run=False, max_downloads=2,
        scratch_parent=tmp_path, fetch=fetch,
        progress_callback=lambda completed, total, event: progress.append(
            (completed, total, event["version"], event["status"])
        ),
    )
    assert progress == [
        (1, 2, "v1", "pdf_only"),
        (2, 2, "v2", "not_found"),
    ]
    assert report["summary"]["network_attempts"] == 2
    assert report["summary"]["pdf_only"] == 1
    assert report["summary"]["not_found"] == 1


def test_dry_run_never_invokes_progress_callback(tmp_path):
    data = fixture_data(tmp_path)
    progress = []
    run_version_downloads(
        data, ids=["2501.00001"], dry_run=True, max_downloads=2,
        progress_callback=lambda *_: progress.append("called"),
    )
    assert progress == []
    assert not (data / "versions_manifest.jsonl").exists()


def test_progress_callback_failure_does_not_lose_download(tmp_path):
    data = fixture_data(tmp_path)
    def broken_callback(*args):
        raise RuntimeError("display error")

    report = run_version_downloads(
        data, ids=["2501.00001"], dry_run=False, max_downloads=1,
        scratch_parent=tmp_path,
        fetch=lambda _: ("ok", b"%PDF-1.4", "application/pdf", ""),
        progress_callback=broken_callback,
    )
    assert report["summary"]["network_attempts"] == 1
    assert (data / "raw" / "2501_00001" / "v1" / ".version-status.json").exists()
    assert (data / "versions_manifest.jsonl").is_file()


def test_callback_total_respects_available_candidates(tmp_path):
    data = fixture_data(tmp_path)
    progress = []
    run_version_downloads(
        data, ids=["2501.00001"], dry_run=False, max_downloads=3,
        scratch_parent=tmp_path,
        fetch=lambda _: ("not_found", b"", "", "404"),
        progress_callback=lambda i, n, _: progress.append((i, n)),
    )
    assert progress == [(1, 2), (2, 2)]
