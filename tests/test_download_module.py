from pathlib import Path
from unittest.mock import MagicMock
import pytest
from src.download_module.downloader import DownloadStats, download_papers_range

def _fake_stats(arxiv_id):
    return DownloadStats(arxiv_id, "success", True, 100, 50, 1, 0.01)

def test_download_papers_range_accepts_explicit_id_list(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(
        "src.download_module.downloader.download_single_paper",
        lambda arxiv_id, **kw: seen.append(arxiv_id) or _fake_stats(arxiv_id),
    )
    download_papers_range(
        raw_dir=tmp_path / "raw",
        csv_dir=tmp_path / "csv",
        id_list=["2502.00001", "2502.00005"],
        resume=False,
    )
    # Must only crawl the 2 explicitly listed papers, not 00001 through 00005
    assert seen == ["2502.00001", "2502.00005"]


class _SlowLimiter:
    def wait(self):
        import time
        time.sleep(0.3)


def test_download_time_excludes_rate_limit_wait(tmp_path, monkeypatch):
    from src.download_module.downloader import download_single_paper
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"Content-Type": "application/pdf"}
    mock_resp.content = b"%PDF-1.4 test pdf content"
    monkeypatch.setattr("requests.get", lambda *a, **kw: mock_resp)

    stats = download_single_paper(
        "2502.00001",
        raw_dir=tmp_path,
        rate_limiter=_SlowLimiter(),
    )
    assert stats.download_time_sec < 0.25
