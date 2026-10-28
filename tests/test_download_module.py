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
    mock_resp.iter_content = lambda chunk_size=1024: [b"%PDF-1.4 test pdf content"]
    monkeypatch.setattr("requests.get", lambda *a, **kw: mock_resp)

    stats = download_single_paper(
        "2502.00001",
        raw_dir=tmp_path,
        rate_limiter=_SlowLimiter(),
    )
    assert stats.download_time_sec < 0.25


def test_download_429_backoff_delay(tmp_path, monkeypatch):
    from src.download_module.downloader import download_single_paper
    sleeps = []
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))
    
    resp_429 = MagicMock()
    resp_429.status_code = 429
    resp_429.headers = {}
    
    resp_200 = MagicMock()
    resp_200.status_code = 200
    resp_200.headers = {"Content-Type": "application/pdf"}
    resp_200.content = b"%PDF-1.4 test"
    resp_200.iter_content = lambda chunk_size=1024: [b"%PDF-1.4 test"]
    
    calls = [resp_429, resp_200]
    monkeypatch.setattr("requests.get", lambda *a, **kw: calls.pop(0))
    
    stats = download_single_paper("2502.00001", raw_dir=tmp_path, max_retries=2, jitter=0.0)
    assert stats.download_status == "pdf_only"
    assert len(sleeps) == 1
    # First 429 retry must wait at least 6.0 seconds
    assert sleeps[0] >= 6.0
