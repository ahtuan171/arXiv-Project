"""Notebook retains main-branch Vietnamese layout with twelve code cells."""
import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_colab_form_and_original_section_flow():
    nb = json.loads((ROOT / "src/milestone1_pipeline.ipynb").read_text())
    markdown = "\n".join("".join(c["source"]) for c in nb["cells"] if c["cell_type"] == "markdown")
    cells = ["".join(c["source"]) for c in nb["cells"] if c["cell_type"] == "code"]
    assert len(cells) == 12
    for title in (
        "## 1. Kết nối Google Drive", "## 2. Khai báo khoảng arXiv-ID",
        "## 4. Bước 1: Cào Metadata", "## 5. Bước 2: Tải",
        "## 9. Kiểm tra và Trích xuất", "### Chạy lại và phục hồi",
    ):
        assert title in markdown
    assert "#@title Cấu hình dải ID" in cells[3]
    assert "#@param" in cells[3]
    for i, code in enumerate(cells, 1):
        ast.parse(code, filename=f"main-pipeline-cell-{i}")


def test_summary_displays_four_legacy_tables_separately():
    nb = json.loads((ROOT / "src/milestone1_pipeline.ipynb").read_text())
    final = "".join([c for c in nb["cells"] if c["cell_type"] == "code"][-1]["source"])
    for file in (
        "m1_discovery_stats.csv", "m1_download_stats.csv",
        "m1_reference_stats.csv", "m1_system_benchmark.csv",
        "m1_download_versions.csv",
    ):
        assert file in final
    assert "legacy" in final


def test_colab_form_shows_only_everyday_controls():
    nb = json.loads((ROOT / "src/milestone1_pipeline.ipynb").read_text())
    form = "".join([c for c in nb["cells"] if c["cell_type"] == "code"][3]["source"])
    visible = [line.split("=")[0].strip() for line in form.splitlines() if "#@param" in line]
    assert visible == [
        "start_id", "end_id", "LIMIT", "DOWNLOAD_VERSIONS",
        "VERSION_LIMIT", "FETCH_RAW_REFERENCES", "RAW_REF_LIMIT",
    ]
    assert "METADATA_RETRY_ERRORS = False" in form
    assert "EXPORT_M1_REPORT = False" in form
