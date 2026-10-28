"""Core downloader module for arXiv LaTeX sources.

Downloads arXiv paper source packages, unpacks archives extracting strictly .tex and .bib
files, prunes any extraneous binaries to save disk storage, handles rate-limiting
and transient network/server errors with exponential backoff, and records statistics
matching Table 2 (m1_download_stats).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional
import gzip
import http.client
import io
import logging
import os
import random
import shutil
import tarfile
import time
import tempfile
import json
import zlib

import pandas as pd
import requests

from src import config, utils, recovery
from src.utils import RateLimiter, parse_arxiv_range

logger = logging.getLogger(__name__)

ARXIV_SRC_URL = "https://export.arxiv.org/src/{arxiv_id}"
DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) arXiv-Project/1.0 (academic research)"

# Default rate limiter enforcing the mandatory 3-second delay
_default_arxiv_limiter = RateLimiter(config.ARXIV_MIN_INTERVAL)


@dataclass
class DownloadStats:
    """Statistics for an arXiv paper download matching Table 2 (m1_download_stats).

    Attributes:
        arxiv_id: arXiv identifier (Primary Key).
        download_status: 'success', 'failed', or 'pdf_only'.
        has_tex: True if LaTeX source (.tex) is present, False otherwise.
        raw_size_bytes: Size of the downloaded archive file before filtering.
        cleaned_size_bytes: Size on disk after extracting only .tex and .bib files.
        num_tex_files: Number of .tex files in the paper.
        download_time_sec: Total elapsed wall time (seconds) for downloading, extracting, and cleaning.
    """

    arxiv_id: str
    download_status: str
    has_tex: bool
    raw_size_bytes: int
    cleaned_size_bytes: int
    num_tex_files: int
    download_time_sec: float
    # Operational detail stays in JSONL/ledger; Table 2 columns are unchanged.
    error_type: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return Table 2 fields plus error detail for the JSONL history."""
        return {
            "error_type": self.error_type,
            "error": self.error,
            "arxiv_id": self.arxiv_id,
            "download_status": self.download_status,
            "has_tex": self.has_tex,
            "raw_size_bytes": int(self.raw_size_bytes),
            "cleaned_size_bytes": int(self.cleaned_size_bytes),
            "num_tex_files": int(self.num_tex_files),
            "download_time_sec": round(float(self.download_time_sec), 4),
        }


def clean_arxiv_id(arxiv_id: str) -> str:
    """Passthrough to src.utils.clean_arxiv_id for convenience."""
    return utils.clean_arxiv_id(arxiv_id)


def extract_source_archive(archive_bytes: bytes, target_dir: Path) -> tuple[bool, bool]:
    """Unpack downloaded source archive into `target_dir`, keeping STRICTLY .tex and .bib files.

    Supports `.tar.gz`, `.tar`, and single `.gz` compressed files.
    Ensures safe extraction preventing directory traversal.
    Completely ignores image/figure files, .sty, .cls, .pdf, and other binaries.

    Args:
        archive_bytes: Raw bytes of the downloaded archive.
        target_dir: Directory where extracted files will be placed.

    Returns:
        tuple (extracted_ok: bool, has_tex: bool).
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    target_dir_resolved = target_dir.resolve()

    # Attempt 1: Tar archive (.tar.gz, .tar, .tar.bz2)
    try:
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:*") as tar:
            has_tex = False
            extracted_any = False
            for member in tar.getmembers():
                # Prevent directory traversal
                member_path = (target_dir / member.name).resolve()
                if not member_path.is_relative_to(target_dir_resolved):
                    logger.warning("Skipping malicious tar member: %s", member.name)
                    continue

                lower_name = member.name.lower()
                # Strictly keep ONLY .tex and .bib files
                if not member.isfile() or not (lower_name.endswith(".tex") or lower_name.endswith(".bib")):
                    continue

                if lower_name.endswith(".tex"):
                    has_tex = True

                # Safe extraction
                if hasattr(tarfile, "data_filter"):
                    tar.extract(member, path=target_dir, filter="data")
                else:
                    tar.extract(member, path=target_dir)
                extracted_any = True

            return True, has_tex
    except (tarfile.TarError, EOFError, zlib.error):
        pass

    # Decompress separately: disk write errors must reach the caller, rather
    # than being mistaken for an unsupported gzip response.
    decompressed = None
    try:
        decompressed = gzip.decompress(archive_bytes)
    except (gzip.BadGzipFile, OSError, EOFError, zlib.error):
        pass
    if decompressed is not None and b"\x00" not in decompressed and (b"\\documentclass" in decompressed or
            b"\\begin" in decompressed or decompressed.startswith(b"%")):
        (target_dir / "main.tex").write_bytes(decompressed)
        return True, True

    # Attempt 3: Plain uncompressed text / LaTeX
    # A broken tar can still contain readable TeX. Its binary header must not
    # fall through and become a fake successful main.tex containing the tar.
    if b"\x00" not in archive_bytes and (b"\\documentclass" in archive_bytes or b"\\begin{" in archive_bytes or archive_bytes.startswith(b"%")):
        out_file = target_dir / "main.tex"
        out_file.write_bytes(archive_bytes)
        return True, True

    return False, False


def _has_source(directory: Path) -> bool:
    """Completion requires a nonempty TeX file, not just an existing folder."""
    return directory.is_dir() and any(
        f.is_file() and f.suffix.lower() == ".tex" and f.stat().st_size > 0
        for f in directory.rglob("*")
    )


def _recover_source(directory: Path, *, cleanup_staging: bool = False) -> None:
    """Repair an interrupted two-rename publish before checking resume state."""
    if cleanup_staging:
        # The caller holds the per-paper lock, so these cannot be another
        # active writer's files. They were left by a hard stop in an older run.
        for stale in directory.parent.glob(f".{directory.name}.staging-*"):
            if stale.is_dir():
                shutil.rmtree(stale)
    backup = directory.with_name(f".{directory.name}.backup")
    if not backup.exists():
        return
    marker = directory / ".source-status.json"
    pdf_only = False
    if marker.exists():
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
            pdf_only = isinstance(payload, dict) and payload.get("status") == "pdf_only"
        except (ValueError, UnicodeDecodeError):
            pass
    valid = _has_source(directory) or pdf_only
    if directory.exists() and valid:
        shutil.rmtree(backup)
    else:
        if directory.exists():
            shutil.rmtree(directory)
        os.replace(backup, directory)


def _publish_source(staged: Path, directory: Path) -> None:
    """Preserve the old folder until a validated new folder has been installed."""
    _recover_source(directory)
    backup = directory.with_name(f".{directory.name}.backup")
    if directory.exists():
        os.replace(directory, backup)
    try:
        os.replace(staged, directory)
    except BaseException:
        if backup.exists() and not directory.exists():
            os.replace(backup, directory)
        raise
    if backup.exists():
        shutil.rmtree(backup)


def download_single_paper(
    arxiv_id: str,
    raw_dir: Optional[Path] = None,
    rate_limiter: Optional[Any] = _default_arxiv_limiter,
    session: Optional[requests.Session] = None,
    timeout: int = config.REQUEST_TIMEOUT,
    user_agent: str = DEFAULT_USER_AGENT,
    max_retries: int = config.MAX_RETRIES,
    backoff_base: float = config.BACKOFF_BASE,
    max_backoff: float = config.MAX_BACKOFF,
    jitter: float = config.JITTER,
) -> DownloadStats:
    """Download, extract, and clean the source files of a single arXiv paper.

    Retries on:
    - HTTP 429 Too Many Requests (enforces rate-limit and honours Retry-After header)
    - HTTP 500, 502, 503, 504 (server overload/gateway errors)
    - Connection timeout or network drops (requests.exceptions.Timeout, ConnectionError, IncompleteRead)
    Fails fast on HTTP 404 (not found).

    Measures performance and attributes matching Table 2 (m1_download_stats):
    - arxiv_id (Primary Key)
    - download_status ('success', 'failed', 'pdf_only')
    - has_tex (True / False)
    - raw_size_bytes (size of downloaded compressed archive)
    - cleaned_size_bytes (size of extracted .tex and .bib files)
    - num_tex_files (count of .tex files)
    - download_time_sec (elapsed time in seconds)

    Args:
        arxiv_id: Paper identifier, e.g. '2504.12523' or '2504.12523v1'.
        raw_dir: Root directory to save the paper files. Defaults to config.RAW_DIR.
        rate_limiter: Optional RateLimiter instance enforcing minimum request interval.
        session: Optional requests.Session for connection reuse.
        timeout: HTTP request timeout in seconds.
        user_agent: Custom User-Agent header for arXiv politeness.
        max_retries: Maximum attempts for transient network or server errors.
        backoff_base: Exponential backoff base multiplier (seconds).
        max_backoff: Maximum delay cap for exponential backoff (seconds).
        jitter: Random jitter added to backoff (seconds).

    Returns:
        DownloadStats object recording all attributes required by Table 2.
    """
    if raw_dir is None:
        raw_dir = config.RAW_DIR
    raw_dir.mkdir(parents=True, exist_ok=True)

    if max_retries < 1:
        raise ValueError("max_retries must be at least 1")
    if rate_limiter is not None:
        rate_limiter.wait()
    with utils.file_lock(raw_dir / f".{clean_arxiv_id(arxiv_id)}.download.lock"):
        t0 = time.perf_counter()
        paper_dir = raw_dir / clean_arxiv_id(arxiv_id)
        _recover_source(paper_dir, cleanup_staging=True)

        def failed(error_type: str, message: str, size: int = 0) -> DownloadStats:
            return DownloadStats(arxiv_id, "failed", False, size, 0, 0,
                                 time.perf_counter() - t0, error_type, message)

        url = ARXIV_SRC_URL.format(arxiv_id=arxiv_id)
        headers = {"User-Agent": user_agent}
        for attempt in range(1, max_retries + 1):
            retry_after = None
            try:
                res = (session.get if session is not None else requests.get)(
                    url, headers=headers, timeout=timeout)
                if res.status_code == 200:
                    # Materialize body here so a truncated transfer receives retries.
                    content = res.content
                    content_type = res.headers.get("Content-Type", "").lower()
                    break
                message = f"HTTP {res.status_code}"
                if res.status_code == 404:
                    return failed("not_found", message)
                if res.status_code != 429 and not 500 <= res.status_code < 600:
                    return failed("blocked", message)
                retry_after = recovery.parse_retry_after(res.headers.get("Retry-After"))
            except (requests.exceptions.RequestException, http.client.IncompleteRead) as exc:
                message = f"Network error: {exc}"
            if attempt == max_retries:
                logger.error("arXiv download %s failed after %d attempts: %s", arxiv_id, attempt, message)
                return failed("transient", message)
            delay = retry_after if retry_after is not None else min(backoff_base ** attempt, max_backoff)
            if jitter > 0:
                delay += random.uniform(0.0, jitter)
            logger.warning("arXiv download %s failed (%s); retry %d/%d in %.2fs",
                           arxiv_id, message, attempt, max_retries, delay)
            time.sleep(max(0.0, delay))

        size = len(content)
        is_pdf = "application/pdf" in content_type or content.startswith(b"%PDF")
        # Extract outside the HTTP retry loop: archive errors are classified for a
        # later run; local disk errors propagate and never commit a success ledger.
        with tempfile.TemporaryDirectory(prefix=f".{paper_dir.name}.staging-", dir=raw_dir) as temp:
            staged = Path(temp)
            if is_pdf:
                (staged / ".source-status.json").write_text(json.dumps({"status": "pdf_only"}))
                _publish_source(staged, paper_dir)
                return DownloadStats(arxiv_id, "pdf_only", False, size, 0, 0, time.perf_counter() - t0)
            extracted_ok, _ = extract_source_archive(content, staged)
            if not extracted_ok or not _has_source(staged):
                return failed("invalid_archive", "Source archive is corrupt, unsupported, or has no nonempty TeX", size)
            files = [f for f in staged.rglob("*") if f.is_file()]
            num_tex = sum(f.suffix.lower() == ".tex" and f.stat().st_size > 0 for f in files)
            cleaned_size = sum(f.stat().st_size for f in files)
            for file in files:
                with file.open("rb") as handle:
                    os.fsync(handle.fileno())
            _publish_source(staged, paper_dir)
        return DownloadStats(arxiv_id, "success", True, size, cleaned_size, num_tex, time.perf_counter() - t0)


def download_papers_range(
    start_id: Optional[str | int] = None,
    end_id: Optional[str | int] = None,
    month: Optional[str] = None,
    raw_dir: Optional[Path] = None,
    csv_dir: Optional[Path] = None,
    delay: float = config.ARXIV_MIN_INTERVAL,
    resume: bool = True,
    show_progress: bool = True,
    max_retries: int = config.MAX_RETRIES,
    backoff_base: float = config.BACKOFF_BASE,
    retry_errors: bool = False,
    retry_not_found: bool = False,
    retry_partial: bool = False,
    id_list: Optional[Iterable[str]] = None,
) -> pd.DataFrame:
    """Download, extract, and clean a range of arXiv papers, recording Table 2 statistics.

    Enforces minimum 3-second delay between requests as mandated in crawling.md.
    Extracts strictly .tex and .bib source files and exports summary statistics
    to `m1_download_stats.csv`.

    Args:
        start_id: First arXiv ID in the range (e.g. '2504.12523' or integer ID).
        end_id: Last arXiv ID in the range (inclusive).
        month: Required if start_id and end_id are integers without month prefix.
        raw_dir: Path to directory where raw files will be saved. Defaults to config.RAW_DIR.
        csv_dir: Path to directory where CSV summary will be saved. Defaults to DATA_DIR / "csv".
        delay: Delay in seconds between requests (minimum 3.0s per crawling.md).
        resume: Skip completed outputs and errors still in cooldown.
        retry_errors: Retry failed/blocked papers immediately.
        retry_not_found: Recheck HTTP 404 results.
        retry_partial: Shared pipeline option; source has no partial state.
        show_progress: Whether to display a tqdm progress bar.
        max_retries: Maximum attempts per paper download for transient errors.
        backoff_base: Base multiplier for exponential backoff (seconds).
        id_list: Explicit list or iterable of arXiv IDs to process.

    Returns:
        pd.DataFrame containing the Table 2 statistics (m1_download_stats).
    """
    if raw_dir is None:
        raw_dir = config.RAW_DIR
    raw_dir.mkdir(parents=True, exist_ok=True)

    if csv_dir is None:
        csv_dir = config.DATA_DIR / "csv"
    csv_dir.mkdir(parents=True, exist_ok=True)

    csv_file = csv_dir / "m1_download_stats.csv"
    jsonl_file = csv_dir / "m1_download_stats.jsonl"

    if id_list is not None:
        target_ids = [str(i).strip() for i in id_list if str(i).strip()]
    elif start_id is not None and end_id is not None:
        target_ids = parse_arxiv_range(start_id, end_id, month=month)
    else:
        raise ValueError("Must provide either id_list or both start_id and end_id")

    records = recovery.load_stats(jsonl_file, csv_file)
    ledger = recovery.RecoveryLedger(csv_file.with_suffix(".ledger.jsonl"))
    pending = []
    for arxiv_id in dict.fromkeys(target_ids):
        key = clean_arxiv_id(arxiv_id)
        paper_dir = raw_dir / key
        with utils.file_lock(raw_dir / f".{key}.download.lock"):
            _recover_source(paper_dir)
        previous = ledger.get(arxiv_id)
        old = records.get(key, {})
        if previous is None:
            status = {"success": "ok", "pdf_only": "pdf_only"}.get(old.get("download_status"), "error")
            previous = {"status": status}
        if previous.get("status") == "ok" and not _has_source(paper_dir):
            previous = None
        if not resume or not recovery.should_skip(previous, retry_errors=retry_errors,
                retry_not_found=retry_not_found, retry_partial=retry_partial):
            pending.append(arxiv_id)

    columns = ["arxiv_id", "download_status", "has_tex", "raw_size_bytes",
               "cleaned_size_bytes", "num_tex_files", "download_time_sec"]
    limiter = RateLimiter(delay) if delay > 0 else None
    session = requests.Session()
    from tqdm import tqdm
    try:
        for arxiv_id in tqdm(pending, desc="Downloading arXiv sources", disable=not show_progress):
            stats = download_single_paper(arxiv_id=arxiv_id, raw_dir=raw_dir,
                rate_limiter=limiter, session=session, max_retries=max_retries,
                backoff_base=backoff_base)
            rec = stats.to_dict()
            recovery.durable_append(jsonl_file, rec)
            records[clean_arxiv_id(arxiv_id)] = rec
            status = {"success": "ok", "pdf_only": "pdf_only"}.get(stats.download_status, "error")
            if stats.error_type in {"not_found", "blocked"}:
                status = stats.error_type
            ledger.record(arxiv_id, status, error_type=stats.error_type, error=stats.error)
    finally:
        session.close()
    df = recovery.export_stats(records, columns, csv_file)
    logger.info("Saved %d records to %s", len(df), csv_file)
    return df
