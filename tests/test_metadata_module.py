import time
from pathlib import Path
from unittest.mock import MagicMock
import pandas as pd
import pytest

from src.metadata_module.crawl_papers import crawl_papers

class _SlowLimiter:
    def wait(self):
        time.sleep(0.3)

def _mock_paper():
    paper = MagicMock()
    paper.entry_id = "http://arxiv.org/abs/2502.00001v1"
    paper.published = None
    paper.updated = None
    paper.title = "Mock Paper Title"
    paper.authors = []
    paper.summary = "Summary"
    paper.categories = ["cs.LG"]
    paper.primary_category = "cs.LG"
    paper.doi = None
    paper.journal_ref = None
    paper.comment = None
    return paper

def test_discovery_time_excludes_rate_limit_wait(tmp_path, monkeypatch):
    import src.metadata_module.crawl_papers as cp_module
    paper = _mock_paper()
    monkeypatch.setattr(cp_module._client, "results", lambda search: iter([paper]))

    csv_path = tmp_path / "m1_discovery_stats.csv"
    crawl_papers(
        id_list=["2502.00001"],
        out_path=tmp_path / "papers.jsonl",
        csv_path=csv_path,
        resume=False,
        rate_limiter=_SlowLimiter(),
    )
    df = pd.read_csv(csv_path)
    assert df.loc[0, "discovery_time_sec"] < 0.25


def test_crawl_papers_batching_success_and_not_found(tmp_path, monkeypatch):
    import src.metadata_module.crawl_papers as cp_module
    from src import utils

    wait_calls = []
    class _CountingLimiter:
        def wait(self):
            wait_calls.append(1)

    paper1 = MagicMock()
    paper1.entry_id = "http://arxiv.org/abs/2502.00001v1"
    paper1.title = "Paper 1"
    paper1.authors = []
    paper1.summary = "Summary 1"
    paper1.categories = ["cs.AI"]
    paper1.primary_category = "cs.AI"
    paper1.published = None
    paper1.updated = None
    paper1.doi = None
    paper1.journal_ref = None
    paper1.comment = None

    paper3 = MagicMock()
    paper3.entry_id = "http://arxiv.org/abs/2502.00003v2"
    paper3.title = "Paper 3"
    paper3.authors = []
    paper3.summary = "Summary 3"
    paper3.categories = ["cs.CV"]
    paper3.primary_category = "cs.CV"
    paper3.published = None
    paper3.updated = None
    paper3.doi = None
    paper3.journal_ref = None
    paper3.comment = None

    def mock_results(search):
        # Emulate arXiv API returning only found papers
        found = []
        for pid in search.id_list:
            if "2502.00001" in pid:
                found.append(paper1)
            elif "2502.00003" in pid:
                found.append(paper3)
        return iter(found)

    monkeypatch.setattr(cp_module._client, "results", mock_results)

    csv_path = tmp_path / "m1_discovery_stats.csv"
    out_jsonl = tmp_path / "papers.jsonl"
    stats = crawl_papers(
        id_list=["2502.00001", "2502.00002", "2502.00003"],
        out_path=out_jsonl,
        csv_path=csv_path,
        batch_size=3,
        resume=False,
        rate_limiter=_CountingLimiter(),
    )

    # 1 batch of 3 items should only call limiter.wait() ONCE
    assert len(wait_calls) == 1
    assert stats["success"] == 2
    assert stats["error"] == 1

    df = pd.read_csv(csv_path, dtype={"arxiv_id": str})
    assert len(df) == 3
    row1 = df[df["arxiv_id"] == "2502.00001"].iloc[0]
    row2 = df[df["arxiv_id"] == "2502.00002"].iloc[0]
    row3 = df[df["arxiv_id"] == "2502.00003"].iloc[0]

    assert row1["discovery_status"] == "success"
    assert row2["discovery_status"] == "not_found"
    assert row3["discovery_status"] == "success"
    assert row3["num_versions"] == 2


def test_crawl_papers_does_not_scan_disk(tmp_path, monkeypatch):
    import src.metadata_module.crawl_papers as cp_module
    from src import utils

    dir_size_calls = []
    monkeypatch.setattr(utils, "dir_size_mb", lambda path: dir_size_calls.append(path) or 0.0)
    paper = _mock_paper()
    monkeypatch.setattr(cp_module._client, "results", lambda search: iter([paper]))

    crawl_papers(
        id_list=["2502.00001"],
        out_path=tmp_path / "papers.jsonl",
        csv_path=tmp_path / "m1_discovery_stats.csv",
        resume=False,
    )

    # crawl_papers must NOT invoke dir_size_mb (which causes severe freeze on large data dirs)
    assert len(dir_size_calls) == 0


