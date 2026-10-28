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
