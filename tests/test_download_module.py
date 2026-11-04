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


def test_download_resumes_via_http_range(tmp_path, monkeypatch):
    from src.download_module.downloader import download_single_paper
    import http.client
    
    # Attempt 1: Server provides first 50 bytes with Content-Length 100, then disconnects
    resp_part1 = MagicMock()
    resp_part1.status_code = 200
    resp_part1.headers = {"Content-Type": "application/pdf", "Content-Length": "100"}
    resp_part1.iter_content = lambda chunk_size=1024: [b"%PDF-1.4 " + b"A" * 41] # 50 bytes
    
    # Attempt 2: Client sends Range: bytes=50-, Server returns 206 with remaining 50 bytes
    range_headers_received = []
    
    def mock_get(url, headers, **kwargs):
        if "Range" in headers:
            range_headers_received.append(headers["Range"])
            resp_part2 = MagicMock()
            resp_part2.status_code = 206
            resp_part2.headers = {"Content-Type": "application/pdf", "Content-Range": "bytes 50-99/100"}
            resp_part2.iter_content = lambda chunk_size=1024: [b"B" * 50] # remaining 50 bytes
            return resp_part2
        return resp_part1

    monkeypatch.setattr("requests.get", mock_get)
    monkeypatch.setattr("time.sleep", lambda s: None)

    stats = download_single_paper("2502.00001", raw_dir=tmp_path, max_retries=3, jitter=0.0)
    assert stats.download_status == "pdf_only"
    assert stats.raw_size_bytes == 100
    assert range_headers_received == ["bytes=50-"]


def test_download_papers_range_skips_when_in_alt_raw_dir(tmp_path, monkeypatch):
    from src import recovery
    raw_dir = tmp_path / "scratch_raw"
    alt_raw_dir = tmp_path / "drive_raw"
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir(parents=True, exist_ok=True)

    # Simulate existing paper on Drive (alt_raw_dir)
    paper_drive = alt_raw_dir / "2502_00001"
    paper_drive.mkdir(parents=True, exist_ok=True)
    (paper_drive / "main.tex").write_text("dummy tex content")

    # Seed ledger so recovery knows it was ok
    ledger = recovery.RecoveryLedger(csv_dir / "m1_download_stats.ledger.jsonl")
    ledger.record("2502.00001", "ok")

    downloaded = []
    monkeypatch.setattr(
        "src.download_module.downloader.download_single_paper",
        lambda arxiv_id, **kw: downloaded.append(arxiv_id) or _fake_stats(arxiv_id),
    )

    df = download_papers_range(
        raw_dir=raw_dir,
        alt_raw_dir=alt_raw_dir,
        csv_dir=csv_dir,
        id_list=["2502.00001", "2502.00002"],
        resume=True,
    )
    # 2502.00001 should be skipped because it exists in alt_raw_dir!
    assert downloaded == ["2502.00002"]
