from pathlib import Path
from unittest.mock import MagicMock
from concurrent.futures import ThreadPoolExecutor
import pandas as pd
import pytest

from src.download_module.downloader import DownloadStats, download_papers_range
from src.crawl_references_module.crawler import crawl_references_range
from src.crawl_references_module.s2_client import S2Paper


def _fake_download_stats(arxiv_id):
    return DownloadStats(arxiv_id, "success", True, 2048, 1024, 2, 0.05)


class MockS2Client:
    async def get_references(self, page, arxiv_id):
        ref1 = S2Paper(
            paper_id="ref_1",
            arxiv_id="2502.99999",
            title="Ref Paper 1",
            authors=["Alice"],
            year=2024,
            publication_date="2024-01-01",
        )
        return [ref1]


def test_concurrent_download_and_references(tmp_path, monkeypatch):
    test_ids = ["2502.00001", "2502.00002"]

    monkeypatch.setattr(
        "src.download_module.downloader.download_single_paper",
        lambda arxiv_id, **kw: _fake_download_stats(arxiv_id),
    )

    raw_dir = tmp_path / "raw"
    csv_dir = tmp_path / "csv"
    raw_dir.mkdir(parents=True, exist_ok=True)
    csv_dir.mkdir(parents=True, exist_ok=True)

    client = MockS2Client()

    with ThreadPoolExecutor(max_workers=2) as executor:
        f_down = executor.submit(
            download_papers_range,
            id_list=test_ids,
            raw_dir=raw_dir,
            csv_dir=csv_dir,
            delay=0.0,
            resume=False,
            show_progress=False,
        )
        f_refs = executor.submit(
            crawl_references_range,
            id_list=test_ids,
            out_path=tmp_path / "references.jsonl",
            csv_path=csv_dir / "m1_reference_stats.csv",
            client=client,
            resume=False,
        )

        df_down = f_down.result()
        df_refs = f_refs.result()

    assert len(df_down) == 2
    assert (df_down["download_status"] == "success").all()
    assert len(df_refs) == 2
    assert (df_refs["ref_status"] == "success").all()
    assert (df_refs["num_arxiv_refs"] == 1).all()
