"""System resource and benchmark tracker matching Table 4 (m1_system_benchmark).

Automatically tracks:
- total_papers: Total number of papers processed in the batch.
- total_wall_time_sec: Wall clock execution time from start to finish.
- peak_ram_mb: Peak RAM (RSS) consumption in MB observed during the pipeline run.
- average_ram_mb: Average RAM (RSS) in MB across periodic background samples.
- peak_disk_mb: Maximum instantaneous disk size observed during extraction/processing.
- final_storage_mb: Total final disk footprint of all output files.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional, Union

import pandas as pd

from src import config

logger = logging.getLogger(__name__)


def current_process_ram_mb() -> float:
    """Return the current resident memory (RSS) in MB for this process."""
    try:
        import psutil
        return float(psutil.Process().memory_info().rss) / (1024 * 1024)
    except Exception:
        pass

    try:
        if sys.platform.startswith("linux"):
            with open("/proc/self/statm", "r") as f:
                pages = int(f.read().split()[1])
                return (pages * os.sysconf("SC_PAGE_SIZE")) / (1024 * 1024)
    except Exception:
        pass

    try:
        import resource
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024
    except Exception:
        return 0.0


def calculate_storage_mb(paths: Iterable[Union[Path, str]]) -> float:
    """Calculate total size of all files across given paths in megabytes."""
    total_bytes = 0
    for item in paths:
        p = Path(item)
        if not p.exists():
            continue
        if p.is_file():
            total_bytes += p.stat().st_size
        else:
            for child in p.rglob("*"):
                if child.is_file():
                    try:
                        total_bytes += child.stat().st_size
                    except OSError:
                        pass
    return round(total_bytes / (1024 * 1024), 4)


class SystemBenchmarkTracker:
    """Monitors CPU/RAM/Disk metrics across Milestone 1 pipeline executions."""

    def __init__(
        self,
        storage_dirs: Optional[Iterable[Union[Path, str]]] = None,
        sample_interval_sec: float = 0.5,
        disk_sample_interval_sec: float = 10.0,
    ) -> None:
        if storage_dirs is None:
            self.storage_dirs = [config.DATA_DIR]
        else:
            self.storage_dirs = [Path(d) for d in storage_dirs]

        self.sample_interval_sec = max(0.01, float(sample_interval_sec))
        self.disk_sample_interval_sec = max(0.0, float(disk_sample_interval_sec))
        self._start_time: float = 0.0
        self._end_time: float = 0.0
        self._ram_samples: list[float] = []
        self._peak_disk_mb: float = 0.0
        self._last_disk_time: float = 0.0
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self) -> "SystemBenchmarkTracker":
        """Start wall-clock timer and background resource sampling thread."""
        self._start_time = time.perf_counter()
        initial_ram = current_process_ram_mb()
        self._ram_samples = [initial_ram] if initial_ram > 0 else []
        self._peak_disk_mb = calculate_storage_mb(self.storage_dirs)
        self._last_disk_time = time.monotonic()
        self._running = True

        self._thread = threading.Thread(target=self._sample_loop, daemon=True)
        self._thread.start()
        logger.info("SystemBenchmarkTracker started (sampling every %.2fs, disk every %.1fs)", self.sample_interval_sec, self.disk_sample_interval_sec)
        return self

    def _sample_loop(self) -> None:
        while self._running:
            ram = current_process_ram_mb()
            if ram > 0:
                self._ram_samples.append(ram)
            now = time.monotonic()
            if self.disk_sample_interval_sec > 0 and (now - self._last_disk_time >= self.disk_sample_interval_sec):
                disk = calculate_storage_mb(self.storage_dirs)
                if disk > self._peak_disk_mb:
                    self._peak_disk_mb = disk
                self._last_disk_time = now
            time.sleep(self.sample_interval_sec)

    def stop(
        self,
        total_papers: int,
        csv_path: Optional[Union[Path, str]] = None,
    ) -> pd.DataFrame:
        """Stop tracking, calculate benchmark metrics, and export to CSV."""
        self._end_time = time.perf_counter()
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=1.0)

        # Record final sample
        final_ram = current_process_ram_mb()
        if final_ram > 0:
            self._ram_samples.append(final_ram)

        final_storage = calculate_storage_mb(self.storage_dirs)
        if final_storage > self._peak_disk_mb:
            self._peak_disk_mb = final_storage

        total_wall_time = max(0.0, self._end_time - self._start_time)
        peak_ram = max(self._ram_samples) if self._ram_samples else current_process_ram_mb()
        avg_ram = sum(self._ram_samples) / len(self._ram_samples) if self._ram_samples else peak_ram

        record = {
            "total_papers": int(total_papers),
            "total_wall_time_sec": round(float(total_wall_time), 4),
            "peak_ram_mb": round(float(peak_ram), 2),
            "average_ram_mb": round(float(avg_ram), 2),
            "peak_disk_mb": round(float(self._peak_disk_mb), 2),
            "final_storage_mb": round(float(final_storage), 2),
        }

        if csv_path is None:
            csv_path = config.DATA_DIR / "csv" / "m1_system_benchmark.csv"
        csv_p = Path(csv_path)
        csv_p.parent.mkdir(parents=True, exist_ok=True)

        expected_cols = [
            "total_papers",
            "total_wall_time_sec",
            "peak_ram_mb",
            "average_ram_mb",
            "peak_disk_mb",
            "final_storage_mb",
        ]

        # Load existing rows if CSV exists to support history
        rows = []
        if csv_p.exists():
            try:
                existing_df = pd.read_csv(csv_p)
                rows = existing_df.to_dict(orient="records")
            except Exception:
                pass

        rows.append(record)
        df = pd.DataFrame(rows)[expected_cols]
        df.to_csv(csv_p, index=False)
        logger.info("Saved benchmark metrics to %s:\n%s", csv_p, record)

        return pd.DataFrame([record])[expected_cols]
