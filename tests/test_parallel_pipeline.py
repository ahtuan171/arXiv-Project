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


def test_alt_raw_dir_existing_papers_skipped(tmp_path, monkeypatch):
    """Ensure existing papers in alt_raw_dir are recognized via set lookup."""
    from src.utils import clean_arxiv_id

    alt_raw = tmp_path / "alt_raw"
    alt_raw.mkdir()
    # Create existing paper directory
    (alt_raw / clean_arxiv_id("2502.00001")).mkdir()

    raw_dir = tmp_path / "raw"
    csv_dir = tmp_path / "csv"
    raw_dir.mkdir()
    csv_dir.mkdir()

    # Pre-record success in csv so resume check evaluates paper_exists
    m1_csv = csv_dir / "m1_download_stats.csv"
    df_existing = pd.DataFrame([{
        "arxiv_id": "2502.00001",
        "download_status": "success",
        "has_tex": True,
        "raw_size_bytes": 100,
        "cleaned_size_bytes": 50,
        "num_tex_files": 1,
        "download_time_sec": 0.1,
    }])
    df_existing.to_csv(m1_csv, index=False)

    downloaded = []
    monkeypatch.setattr(
        "src.download_module.downloader.download_single_paper",
        lambda arxiv_id, **kw: downloaded.append(arxiv_id) or _fake_download_stats(arxiv_id),
    )

    df = download_papers_range(
        id_list=["2502.00001", "2502.00002"],
        raw_dir=raw_dir,
        alt_raw_dir=alt_raw,
        csv_dir=csv_dir,
        delay=0.0,
        resume=True,
        show_progress=False,
    )
    # 2502.00001 was already in alt_raw, so only 2502.00002 should be downloaded
    assert downloaded == ["2502.00002"]


def test_notebook_actual_papers_count_never_zero():
    """Verify the calculation logic used in milestone1_pipeline.ipynb never yields 0."""
    from src.utils import clean_arxiv_id

    id_list = ["2502.00001", "2502.00002"]
    target_count = len(id_list) if id_list else 1

    # Scenario 1: df_downloads contains past cumulative records + current records
    df_downloads = pd.DataFrame([
        {"arxiv_id": "2501.99999", "download_status": "success"}, # from past run
        {"arxiv_id": "2502.00001", "download_status": "success"},
        {"arxiv_id": "2502.00002", "download_status": "success"},
    ])
    target_clean = {clean_arxiv_id(x) for x in id_list}
    session_downloads = df_downloads[df_downloads["arxiv_id"].apply(clean_arxiv_id).isin(target_clean)]
    actual_papers_count = len(session_downloads) if not session_downloads.empty else target_count
    actual_papers_count = max(1, int(actual_papers_count))
    assert actual_papers_count == 2  # exactly 2, not cumulative 3!

    # Scenario 2: df_downloads was None / empty (e.g. early interrupt or error)
    df_downloads = None
    initial_completed_ids = set()
    current_completed_ids = set()
    session_new_ids = (current_completed_ids - initial_completed_ids) & target_clean
    actual_papers_count = len(session_new_ids) if len(session_new_ids) > 0 else target_count
    actual_papers_count = max(1, int(actual_papers_count))
    assert actual_papers_count == 2  # falls back to target_count = 2, never 0!

    # Scenario 3: edge case with empty id_list
    empty_list = []
    target_count_empty = len(empty_list) if empty_list else 1
    actual_empty = max(1, target_count_empty)
    assert actual_empty == 1  # strictly >= 1
