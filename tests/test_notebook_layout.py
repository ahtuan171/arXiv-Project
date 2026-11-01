"""The primary Colab M1 notebook has twelve safe, parseable code cells."""
import ast
import json
from pathlib import Path

import pytest

from src.scripts import audit_m1_integrity as module
from src.scripts.audit_version_downloads import audit_version_downloads
from src.scripts.build_version_metadata import build_version_metadata


ROOT = Path(__file__).resolve().parent.parent


def cells():
    n = json.loads((ROOT / "src/milestone1_pipeline.ipynb").read_text())
    return ["".join(c["source"]) for c in n["cells"] if c["cell_type"] == "code"]


def test_twelve_python_cells_no_legacy_execution():
    items = cells()
    assert len(items) == 12
    for index, item in enumerate(items, 1):
        ast.parse(item, filename=f"notebook-cell-{index}")
        assert "ENABLE_LEGACY_CRAWL" not in item
        assert "download_papers_range" not in item


def test_safe_default_network_and_export_flags():
    config = cells()[3]
    for flag in (
        "DOWNLOAD_VERSIONS", "FETCH_RAW_REFERENCES", "EXPORT_VERSION_CSV",
        "EXPORT_VERSION_METADATA", "EXPORT_M1_INTEGRITY_REPORT", "EXPORT_M1_REPORT"
    ):
        assert f"{flag} = False" in config


def test_single_pass_integrity_reuses_precomputed_audits(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    (root / "papers.jsonl").write_text(json.dumps({
        "arxiv_id": "2501_00001", "title": "Test", "versions": ["v1"]
    }) + "\n")
    (root / "references.jsonl").write_text(json.dumps({
        "arxiv_id": "2501_00001", "ref_arxiv_id": "2301_00001"
    }) + "\n")
    version = audit_version_downloads(root)
    metadata = build_version_metadata(root)
    def fail(*_args, **_kwargs):
        raise AssertionError("Redundant SHA/metadata pass")
    monkeypatch.setattr(module, "audit_version_downloads", fail)
    monkeypatch.setattr(module, "build_version_metadata", fail)
    result = module.audit_m1_integrity(
        root, version_report=version, metadata_report=metadata
    )
    assert result["status"] == "in_progress"
    assert result["coverage"]["expected_versions"] == 1


def test_rejects_reports_from_another_dataset(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    with pytest.raises(ValueError, match="another dataset"):
        module.audit_m1_integrity(root, version_report={
            "data_dir": str(tmp_path / "unrelated")
        })
