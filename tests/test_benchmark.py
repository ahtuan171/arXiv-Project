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


def test_tracker_safe_restart_and_stop(tmp_path):
    tracker = benchmark.SystemBenchmarkTracker(storage_dirs=[tmp_path], sample_interval_sec=0.01)
    tracker.start()
    time.sleep(0.05)
    df1 = tracker.stop(total_papers=1, csv_path=tmp_path / "bench.csv")
    assert len(df1) == 1
    assert df1["total_wall_time_sec"].iloc[0] > 0

    # Restart without thread conflict or crash
    tracker.start()
    time.sleep(0.05)
    df2 = tracker.stop(total_papers=2, csv_path=tmp_path / "bench.csv")
    assert len(df2) == 1
    assert df2["total_wall_time_sec"].iloc[0] > 0
    assert df2["total_papers"].iloc[0] == 2


def test_dir_size_mb_file_and_dir(tmp_path):
    from src.utils import dir_size_mb
    f = tmp_path / "test.txt"
    f.write_bytes(b"x" * 1024 * 1024)  # 1 MB
    sub = tmp_path / "subdir"
    sub.mkdir()
    (sub / "subfile.bin").write_bytes(b"y" * 1024 * 1024)  # 1 MB

    # Test directory calculation
    mb_dir = dir_size_mb(tmp_path)
    assert round(mb_dir, 1) == 2.0

    # Test single file calculation
    mb_file = dir_size_mb(f)
    assert round(mb_file, 1) == 1.0

    # Test non-existent path
    assert dir_size_mb(tmp_path / "does_not_exist") == 0.0


def test_calculate_storage_mb_fallback(tmp_path, monkeypatch):
    f = tmp_path / "test.txt"
    f.write_bytes(b"x" * 1024 * 1024)
    # Force fast path to fail and trigger fallback
    monkeypatch.setattr(benchmark.subprocess, "run", lambda *a, **kw: (_ for _ in ()).throw(FileNotFoundError("No du")))
    size = benchmark.calculate_storage_mb([tmp_path])
    assert round(size, 1) == 1.0



