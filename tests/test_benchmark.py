import time
from pathlib import Path
from src import benchmark

def test_disk_is_not_scanned_on_every_ram_sample(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        benchmark, "calculate_storage_mb", lambda paths: calls.append(1) or 0.0
    )
    tracker = benchmark.SystemBenchmarkTracker(
        storage_dirs=[tmp_path],
        sample_interval_sec=0.01,
        disk_sample_interval_sec=10.0,
    ).start()
    time.sleep(0.12)
    tracker.stop(total_papers=1, csv_path=tmp_path / "bench.csv")
    # 1 call at start + 1 call at stop, should NOT call on every 0.01s tick (which would be ~12 times)
    assert len(calls) <= 3
