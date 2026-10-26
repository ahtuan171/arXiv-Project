"""End-to-end integration test for the Milestone 1 unified pipeline."""

from __future__ import annotations

import io
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

from src.benchmark import SystemBenchmarkTracker
from src.crawl_references_module.crawler import crawl_references_range
from src.crawl_references_module.s2_client import S2Paper
from src.download_module.downloader import download_papers_range
from src.metadata_module.crawl_papers import crawl_papers
from src.utils import parse_arxiv_range


def test_milestone1_pipeline_end_to_end(tmp_path: Path, monkeypatch):
    """Verify that the full Milestone 1 pipeline generates all 4 tables with valid schemas."""
    data_dir = tmp_path / "data"
    raw_dir = data_dir / "raw"
    csv_dir = data_dir / "csv"
    papers_jsonl = data_dir / "papers.jsonl"
    refs_jsonl = data_dir / "references.jsonl"

    start_id = "2502.13235"
    end_id = "2502.13236"
    id_list = parse_arxiv_range(start_id, end_id)
    assert id_list == ["2502.13235", "2502.13236"]

    # 1. Start benchmark tracker
    tracker = SystemBenchmarkTracker(
        storage_dirs=[raw_dir, csv_dir, data_dir],
        sample_interval_sec=0.02,
    )
    tracker.start()

    # 2. Mock arXiv metadata fetch
    mock_paper1 = MagicMock()
    mock_paper1.entry_id = "http://arxiv.org/abs/2502.13235v1"
    mock_paper1.title = "Paper 1"
    author1 = MagicMock()
    author1.name = "Author 1"
    mock_paper1.authors = [author1]
    mock_paper1.summary = "Abstract 1"
    mock_paper1.categories = ["cs.AI"]
    mock_paper1.primary_category = "cs.AI"
    mock_paper1.published = datetime(2025, 2, 18, tzinfo=timezone.utc)
    mock_paper1.updated = None
    mock_paper1.doi = None
    mock_paper1.journal_ref = None
    mock_paper1.comment = None

    mock_paper2 = MagicMock()
    mock_paper2.entry_id = "http://arxiv.org/abs/2502.13236v2"
    mock_paper2.title = "Paper 2"
    author2 = MagicMock()
    author2.name = "Author 2"
    mock_paper2.authors = [author2]
    mock_paper2.summary = "Abstract 2"
    mock_paper2.categories = ["cs.CL"]
    mock_paper2.primary_category = "cs.CL"
    mock_paper2.published = datetime(2025, 2, 19, tzinfo=timezone.utc)
    mock_paper2.updated = datetime(2025, 2, 21, tzinfo=timezone.utc)
    mock_paper2.doi = None
    mock_paper2.journal_ref = None
    mock_paper2.comment = None

    def mock_fetch_arxiv(cid, *a, **kw):
        if "13235" in cid:
            return mock_paper1
        return mock_paper2

    monkeypatch.setattr("src.metadata_module.crawl_papers.try_fetch_arxiv", mock_fetch_arxiv)

    meta_stats = crawl_papers(
        id_list=id_list,
        out_path=papers_jsonl,
        csv_path=csv_dir / "m1_discovery_stats.csv",
        rate_limiter=None,
    )
    assert meta_stats["success"] == 2

    # 3. Mock source archive download (strict .tex/.bib)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in [
            ("main.tex", b"\\documentclass{article}\n\\begin{document}Body\\end{document}"),
            ("refs.bib", b"@article{a, title={b}}"),
            ("image.png", b"fake binary image"),
            ("notes.txt", b"plain text notes"),
        ]:
            ti = tarfile.TarInfo(name=name)
            ti.size = len(content)
            tar.addfile(ti, io.BytesIO(content))
    tar_bytes = buf.getvalue()

    class MockDownloadResponse:
        def __init__(self, content, status_code=200):
            self.content = content
            self.status_code = status_code
            self.headers = {"Content-Type": "application/x-eprint-tar"}

    monkeypatch.setattr("requests.get", lambda url, *a, **kw: MockDownloadResponse(tar_bytes))
    monkeypatch.setattr("requests.Session.get", lambda self, url, *a, **kw: MockDownloadResponse(tar_bytes))

    df_downloads = download_papers_range(
        start_id=start_id,
        end_id=end_id,
        raw_dir=raw_dir,
        csv_dir=csv_dir,
        delay=0.0,
    )
    assert len(df_downloads) == 2

    # Verify only .tex and .bib exist in raw_dir
    for paper_folder in raw_dir.iterdir():
        if paper_folder.is_dir():
            files = [f.name for f in paper_folder.rglob("*") if f.is_file()]
            assert set(files) == {"main.tex", "refs.bib"}

    # 4. Mock Semantic Scholar reference crawler
    class MockS2Client:
        async def start(self, tabs=1):
            return self
        async def close(self):
            pass
        async def get_references(self, page, arxiv_id):
            return [
                S2Paper("p1", "1706.03762", "Attention Is All You Need", ["Vaswani"]),
                S2Paper("p2", "", "Non-arXiv Reference", ["Author"]),
            ]

    df_refs = crawl_references_range(
        id_list=id_list,
        out_path=refs_jsonl,
        csv_path=csv_dir / "m1_reference_stats.csv",
        client=MockS2Client(),
    )
    assert len(df_refs) == 2

    # 5. Stop benchmark tracker
    df_bench = tracker.stop(
        total_papers=len(id_list),
        csv_path=csv_dir / "m1_system_benchmark.csv",
    )

    # 6. Verify all 4 CSV tables exist and strictly match table_description.md schemas
    # Table 1: m1_discovery_stats
    t1_path = csv_dir / "m1_discovery_stats.csv"
    assert t1_path.exists()
    t1_df = pd.read_csv(t1_path, dtype={"arxiv_id": str})
    assert list(t1_df.columns) == ["arxiv_id", "discovery_status", "num_versions", "discovery_time_sec"]
    assert len(t1_df) == 2

    # Table 2: m1_download_stats
    t2_path = csv_dir / "m1_download_stats.csv"
    assert t2_path.exists()
    t2_df = pd.read_csv(t2_path, dtype={"arxiv_id": str})
    assert list(t2_df.columns) == [
        "arxiv_id", "download_status", "has_tex",
        "raw_size_bytes", "cleaned_size_bytes", "num_tex_files", "download_time_sec"
    ]
    assert len(t2_df) == 2
    assert (t2_df["num_tex_files"] == 1).all()

    # Table 3: m1_reference_stats
    t3_path = csv_dir / "m1_reference_stats.csv"
    assert t3_path.exists()
    t3_df = pd.read_csv(t3_path, dtype={"arxiv_id": str})
    assert list(t3_df.columns) == ["arxiv_id", "ref_status", "num_crawled_refs", "num_arxiv_refs", "ref_time_sec"]
    assert len(t3_df) == 2

    # Table 4: m1_system_benchmark
    t4_path = csv_dir / "m1_system_benchmark.csv"
    assert t4_path.exists()
    t4_df = pd.read_csv(t4_path)
    assert list(t4_df.columns) == [
        "total_papers", "total_wall_time_sec", "peak_ram_mb",
        "average_ram_mb", "peak_disk_mb", "final_storage_mb"
    ]
    assert len(t4_df) == 1
    assert t4_df.loc[0, "total_papers"] == 2
    assert t4_df.loc[0, "total_wall_time_sec"] > 0
