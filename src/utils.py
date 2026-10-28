"""Shared helpers: logging, retry, rate limiting, JSONL I/O, performance measurement."""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import random
import re
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

try:  # ``fcntl`` is not available on Windows.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


# --- Identifiers -----------------------------------------------------------
_ARXIV_VERSION_RE = re.compile(r"v\d+$", re.IGNORECASE)
_ARXIV_ID_PATTERN = re.compile(
    r"^(?:\d{4}[._]\d{4,5}|[a-zA-Z\-]+(?:\.[a-zA-Z\-]+)?[/_]\d{7})$"
)


def normalize_arxiv_id(arxiv_id: Optional[str]) -> str:
    """Return an arXiv ID without ``arXiv:`` prefix or version suffix.

    ``arXiv:2301.12345v3`` and ``2301.12345`` both become ``2301.12345``, so
    citation edges match regardless of the version used by Semantic Scholar.
    """
    if arxiv_id is None:
        return ""
    text = str(arxiv_id).strip()
    if text.lower().startswith("arxiv:"):
        text = text[len("arxiv:"):].strip()
    return _ARXIV_VERSION_RE.sub("", text)


def is_valid_arxiv_id(arxiv_id: Optional[str]) -> bool:
    """Return True if arxiv_id matches modern (YYMM.NNNN[N]) or legacy format."""
    if not arxiv_id:
        return False
    norm = normalize_arxiv_id(arxiv_id)
    return bool(_ARXIV_ID_PATTERN.match(norm))


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


def canonical_arxiv_id(arxiv_id: Optional[str]) -> str:
    """Normalize a version and then make it safe for storage."""
    return clean_arxiv_id(normalize_arxiv_id(arxiv_id))


# --- Logging ---------------------------------------------------------------
def setup_logging(name: str = "arxiv", log_file: Optional[Path] = None, level: int = logging.INFO) -> logging.Logger:
    """Configure the root logger so every ``src.*`` module is captured.

    ``s2_client`` and ``utils.retry`` log through their own module loggers.
    Configuring only the caller's logger (the previous behaviour) silently sent
    the most important warnings to ``lastResort`` instead of the log file.
    """
    root = logging.getLogger()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    if not getattr(root, "_arxiv_configured", False):
        root.setLevel(level)
        if not root.handlers:
            stream = logging.StreamHandler(sys.stderr)
            stream.setFormatter(formatter)
            root.addHandler(stream)
        root._arxiv_configured = True  # type: ignore[attr-defined]

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        configured_files = getattr(root, "_arxiv_log_files", set())
        resolved = str(log_file.resolve())
        if resolved not in configured_files:
            file_handler = logging.FileHandler(log_file, encoding="utf-8")
            file_handler.setFormatter(formatter)
            root.addHandler(file_handler)
            configured_files.add(resolved)
            root._arxiv_log_files = configured_files  # type: ignore[attr-defined]

    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = True
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


class AsyncRateLimiter:
    """Async variant of :class:`RateLimiter`, shared by all browser tabs."""

    def __init__(self, min_interval: float) -> None:
        self.min_interval = min_interval
        self._last_call = 0.0
        self._not_before = 0.0
        self._lock = asyncio.Lock()

    def block(self, seconds: float) -> None:
        """Pause every tab after a shared quota error."""
        self._not_before = max(self._not_before, time.monotonic() + max(0.0, seconds))

    async def wait(self) -> None:
        async with self._lock:
            target = max(self._last_call + self.min_interval, self._not_before)
            remaining = target - time.monotonic()
            if remaining > 0:
                await asyncio.sleep(remaining)
            self._last_call = time.monotonic()


def retry(
    max_attempts: int = config.MAX_RETRIES,
    backoff: float = config.BACKOFF_BASE,
    exceptions: tuple[type[BaseException], ...] = (Exception,),
    max_delay: float = config.MAX_BACKOFF,
    jitter: float = config.RETRY_JITTER,
) -> Callable[[F], F]:
    """Retry a function with capped exponential backoff and optional jitter.

    If the raised exception exposes ``retry_after`` (seconds), that value is
    honoured instead of the exponential delay. The decorator only retries the
    exception types passed in ``exceptions``; callers should use this to let
    permanent failures (404, invalid IDs) fail fast.
    """

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
                    retry_after = getattr(error, "retry_after", None)
                    if retry_after is None:
                        delay = backoff**attempt
                    else:
                        delay = float(retry_after)
                    delay = min(max(delay, 0.0), max_delay)
                    if jitter:
                        delay += random.uniform(0.0, jitter)
                    logger.warning(
                        "%s failed (%s); retry %d/%d in %.1fs",
                        func.__name__, error, attempt, max_attempts, delay,
                    )
                    time.sleep(delay)

        return wrapper  # type: ignore[return-value]

    return decorator


# --- JSON Lines I/O (UTF-8, one object per line) ---------------------------
def append_jsonl_many(path: Path, records: Iterable[dict], *, fsync: bool = False) -> int:
    """Append a batch of records with one open/write/close cycle.

    Writing the whole batch at once means a crash can only leave a truncated
    *last* line (which :func:`read_jsonl` skips), never interleaved records.
    Returns the number of records written.
    """
    records = list(records)
    if not records:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)
    # A hard stop may leave an unterminated final line. Isolate it before
    # appending so read_jsonl can skip only that line, not the next valid batch.
    with open(path, "a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell():
            handle.seek(-1, os.SEEK_END)
            if handle.read(1) != b"\n":
                handle.write(b"\n")
        handle.write(payload.encode("utf-8"))
        handle.flush()
        if fsync:
            os.fsync(handle.fileno())
    return len(records)


def append_jsonl(path: Path, record: dict, *, fsync: bool = False) -> None:
    """Append one record to a ``.jsonl`` file, creating parent folders."""
    append_jsonl_many(path, [record], fsync=fsync)


def write_jsonl(path: Path, records: Iterable[dict]) -> None:
    """Write records to a ``.jsonl`` file, replacing any existing content."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> Iterator[dict]:
    """Yield object records, tolerating a torn JSON or UTF-8 line after a crash."""
    # Decode per line: an incomplete Vietnamese character at EOF must not make
    # Python's text decoder reject the complete records preceding that line.
    with open(path, "rb") as handle:
        for raw_line in handle:
            if not raw_line.strip():
                continue
            try:
                row = json.loads(raw_line.decode("utf-8"))
                if not isinstance(row, dict):
                    raise ValueError("JSONL record must be an object")
                yield row
            except (UnicodeDecodeError, ValueError):
                logging.getLogger(__name__).warning("Skipping malformed line in %s", path)


def load_done_ids(path: Path, key: str = "arxiv_id") -> set[str]:
    """IDs already stored in ``path``; lets a scraper resume where it stopped."""
    if not path.exists():
        return set()
    return {record[key] for record in read_jsonl(path) if key in record}


def load_latest_records(path: Path, key: str = "arxiv_id") -> dict[str, dict]:
    """Return the last record per key, e.g. a progress ledger keyed by arXiv ID."""
    if not path.exists():
        return {}
    latest: dict[str, dict] = {}
    for record in read_jsonl(path):
        value = record.get(key)
        if value:
            latest[str(value)] = record
    return latest


# --- Process locking -------------------------------------------------------
class LockError(RuntimeError):
    """Another process already owns the crawler lock."""


@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """Take an exclusive non-blocking lock for one member's output directory.

    Prevents two crawler processes for the same member from appending duplicate
    edges. On platforms without ``fcntl`` this is a no-op.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+", encoding="utf-8")
    try:
        if fcntl is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise LockError(f"Another crawler is already running ({path})") from error
        yield
    finally:
        if fcntl is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


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


def parse_arxiv_range(
    start_id: str | int,
    end_id: str | int,
    month: Optional[str] = None,
    month_sizes: Optional[dict[str, int]] = None,
) -> list[str]:
    """Generate a list of arXiv IDs from start_id to end_id inclusive.

    Supports:
    - String IDs formatted as 'YYMM.NNNNN' (e.g. '2504.12523' to '2504.12530')
    - Integer IDs with explicit `month` parameter (e.g. 12523 to 12530 with month='2502')
    """
    if isinstance(start_id, str) and "." in start_id and isinstance(end_id, str) and "." in end_id:
        s_m, s_n = start_id.strip().split(".", 1)
        e_m, e_n = end_id.strip().split(".", 1)
        rng = config.ArxivRange(s_m, int(s_n), e_m, int(e_n))
        return list(rng.iter_ids(month_sizes=month_sizes))
    elif month is not None:
        rng = config.ArxivRange(str(month), int(start_id), str(month), int(end_id))
        return list(rng.iter_ids(month_sizes=month_sizes))
    else:
        raise ValueError("Provide arXiv IDs in 'YYMM.NNNNN' format or specify the month parameter.")


def get_benchmark_tracker(*args: Any, **kwargs: Any) -> Any:
    """Helper to instantiate SystemBenchmarkTracker."""
    from src.benchmark import SystemBenchmarkTracker
    return SystemBenchmarkTracker(*args, **kwargs)


