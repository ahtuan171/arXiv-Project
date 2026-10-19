"""Shared helpers: logging, retry, rate limiting, JSONL I/O, performance measurement."""

from __future__ import annotations

import functools
import json
import logging
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional, TypeVar

from src import config

F = TypeVar("F", bound=Callable[..., Any])

try:  # ``resource`` is not available on Windows.
    import resource
except ImportError:  # pragma: no cover
    resource = None  # type: ignore[assignment]


# --- Identifiers -----------------------------------------------------------
def clean_arxiv_id(arxiv_id: str) -> str:
    """Make an arXiv ID safe for file names and data fields.

    Replaces ``/``, ``:`` and ``.`` by ``config.ID_SEPARATOR`` so that
    ``cs/0101001`` becomes ``cs_0101001`` and ``2301.12345v2`` becomes
    ``2301_12345v2``. The version suffix is kept.
    """
    text = str(arxiv_id).strip()
    for char in "/:.":
        text = text.replace(char, config.ID_SEPARATOR)
    return text


# --- Logging ---------------------------------------------------------------
def setup_logging(name: str = "arxiv", log_file: Optional[Path] = None, level: int = logging.INFO) -> logging.Logger:
    """Return a logger writing to stderr and, optionally, to ``log_file``."""
    logger = logging.getLogger(name)
    if logger.handlers:  # already configured
        return logger
    logger.setLevel(level)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    for handler in handlers:
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    return logger


# --- Rate limiting and retry ----------------------------------------------
class RateLimiter:
    """Guarantee a minimum interval between consecutive calls (thread-safe).

    Use one instance per remote service, e.g. ``RateLimiter(config.ARXIV_MIN_INTERVAL)``.
    """

    def __init__(self, min_interval: float) -> None:
        self.min_interval = min_interval
        self._last_call = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        """Block until ``min_interval`` seconds passed since the previous call."""
        with self._lock:
            remaining = self._last_call + self.min_interval - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
            self._last_call = time.monotonic()

    def __call__(self, func: F) -> F:
        """Use the limiter as a decorator."""

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            self.wait()
            return func(*args, **kwargs)

        return wrapper  # type: ignore[return-value]


def retry(
    max_attempts: int = config.MAX_RETRIES,
    backoff: float = config.BACKOFF_BASE,
    exceptions: tuple[type[BaseException], ...] = (Exception,),
) -> Callable[[F], F]:
    """Retry a function with exponential backoff; re-raise after the last attempt."""

    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            logger = logging.getLogger(func.__module__)
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as error:
                    if attempt == max_attempts:
                        raise
                    delay = backoff**attempt
                    logger.warning("%s failed (%s); retry %d/%d in %.0fs", func.__name__, error, attempt, max_attempts, delay)
                    time.sleep(delay)

        return wrapper  # type: ignore[return-value]

    return decorator


# --- JSON Lines I/O (UTF-8, one object per line) ---------------------------
def append_jsonl(path: Path, record: dict) -> None:
    """Append one record to a ``.jsonl`` file, creating parent folders."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def write_jsonl(path: Path, records: Iterable[dict]) -> None:
    """Write records to a ``.jsonl`` file, replacing any existing content."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> Iterator[dict]:
    """Yield records of a ``.jsonl`` file, skipping a truncated last line."""
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                logging.getLogger(__name__).warning("Skipping malformed line in %s", path)


def load_done_ids(path: Path, key: str = "arxiv_id") -> set[str]:
    """IDs already stored in ``path``; lets a scraper resume where it stopped."""
    if not path.exists():
        return set()
    return {record[key] for record in read_jsonl(path) if key in record}


# --- Performance measurement ----------------------------------------------
@dataclass
class PerfResult:
    """Outcome of :func:`measure`; filled in when the ``with`` block exits."""

    label: str = ""
    seconds: float = 0.0
    peak_rss_mb: float = float("nan")
    disk_mb: float = float("nan")


def peak_rss_mb() -> float:
    """Peak resident memory of this process so far, in MB (NaN if unsupported)."""
    if resource is None:
        return float("nan")
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux reports kilobytes, macOS bytes.
    return peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024


def dir_size_mb(path: Path) -> float:
    """Total size of all files under ``path`` in MB."""
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / (1024 * 1024)


@contextmanager
def measure(label: str, disk_path: Optional[Path] = None) -> Iterator[PerfResult]:
    """Measure wall time, peak RAM and (optionally) disk usage of a block.

    ``peak_rss_mb`` is the process-wide peak, not limited to the block. ``disk_mb``
    is the size of ``disk_path`` after the block. Results are also logged.
    """
    result = PerfResult(label=label)
    start = time.perf_counter()
    try:
        yield result
    finally:
        result.seconds = time.perf_counter() - start
        result.peak_rss_mb = peak_rss_mb()
        if disk_path is not None and disk_path.exists():
            result.disk_mb = dir_size_mb(disk_path)
        logging.getLogger("perf").info(
            "%s: %.1fs, peak RAM %.0f MB, disk %.1f MB", label, result.seconds, result.peak_rss_mb, result.disk_mb
        )


def timed(func: F) -> F:
    """Decorator logging wall time and peak RAM of each call."""

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        with measure(func.__qualname__):
            return func(*args, **kwargs)

    return wrapper  # type: ignore[return-value]
