"""Progress callbacks report actual completed work and do not alter output."""
import json

from src.scripts.audit_version_downloads import audit_version_downloads
from src.scripts.audit_legacy_matches import audit_legacy_matches


def fixture_data(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    (d / "papers.jsonl").write_text("\n".join(json.dumps(r) for r in [
        {"arxiv_id": "2501_00001", "title": "First", "versions": ["v1", "v2"]},
        {"arxiv_id": "2502_00002", "title": "Second", "versions": ["v1"]},
    ]) + "\n")
    return d


def test_version_audit_progress_for_all_months(tmp_path):
    d = fixture_data(tmp_path)
    updates = []
    report = audit_version_downloads(
        d, progress_callback=lambda n, total, row: updates.append(
            (n, total, row["arxiv_id"], row["version"])
        ),
    )
    assert report["summary"]["expected_versions"] == 3
    assert updates == [
        (1, 3, "2501_00001", "v1"), (2, 3, "2501_00001", "v2"),
        (3, 3, "2502_00002", "v1"),
    ]
    assert not (d / "csv").exists()


def test_legacy_progress_respects_limit_and_skips_missing_metadata(tmp_path):
    d = fixture_data(tmp_path)
    updates = []
    report = audit_legacy_matches(
        d, ids=["2501.00001", "2502.00002", "2502.00003"],
        max_papers=0, progress_callback=lambda n, total, row: updates.append(
            (n, total, row["status"])
        ),
    )
    assert report["summary"]["examined_papers"] == 3
    assert updates == [(1, 3, "no_legacy"), (2, 3, "no_legacy"), (3, 3, "no_metadata")]


def test_legacy_callback_failure_does_not_interrupt_audit(tmp_path):
    d = fixture_data(tmp_path)
    result = audit_legacy_matches(
        d, max_papers=2,
        progress_callback=lambda *_: (_ for _ in ()).throw(RuntimeError("display")),
    )
    assert result["summary"]["examined_papers"] == 2
