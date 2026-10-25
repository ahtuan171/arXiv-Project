"""Core downloader module for arXiv LaTeX sources.

Downloads arXiv paper source packages, unpacks archives, prunes figure
binaries to save disk storage, and records statistics matching Table 2
(m1_download_stats).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
import requests
import pandas as pd


@dataclass
class DownloadStats:
    """Statistics for an arXiv paper download matching Table 2 (m1_download_stats).

    Attributes:
        arxiv_id: arXiv identifier (Primary Key).
        download_status: 'success', 'failed', or 'pdf_only'.
        has_tex: True if LaTeX source (.tex) is present, False otherwise.
        raw_size_bytes: Size of the downloaded archive file before image filtering.
        cleaned_size_bytes: Size on disk after extracting and removing all image/figure files.
        download_time_sec: Total elapsed wall time (seconds) for downloading, extracting, and cleaning.
    """

    arxiv_id: str
    download_status: str
    has_tex: bool
    raw_size_bytes: int
    cleaned_size_bytes: int
    download_time_sec: float

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary matching the Table 2 CSV schema."""
        return {
            "arxiv_id": self.arxiv_id,
            "download_status": self.download_status,
            "has_tex": self.has_tex,
            "raw_size_bytes": int(self.raw_size_bytes),
            "cleaned_size_bytes": int(self.cleaned_size_bytes),
            "download_time_sec": round(float(self.download_time_sec), 4),
        }


def clean_arxiv_id(arxiv_id: str) -> str:
    """Passthrough to src.utils.clean_arxiv_id for convenience."""
    from src.utils import clean_arxiv_id as _clean
    return _clean(arxiv_id)


import gzip
import io
import logging
import os
import shutil
import tarfile

logger = logging.getLogger(__name__)

# Common figure/image extensions in LaTeX submissions
IMAGE_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".bmp",
    ".tiff",
    ".tif",
    ".pdf",
    ".eps",
    ".ps",
    ".svg",
    ".ai",
    ".webp",
    ".pbm",
    ".pgm",
    ".ppm",
}


def prune_figures(directory: Path) -> tuple[int, int]:
    """Delete all figure/image binary files from directory to minimize disk space.

    Traverses `directory` recursively and deletes any file whose extension
    matches common image/figure formats (case-insensitive). Also removes
    empty subdirectories left behind.

    Args:
        directory: Root path of the paper's extracted source files.

    Returns:
        tuple (num_deleted_files, total_deleted_bytes).
    """
    if not directory.exists() or not directory.is_dir():
        return 0, 0

    num_deleted = 0
    bytes_deleted = 0

    for file_path in list(directory.rglob("*")):
        if file_path.is_file():
            if file_path.suffix.lower() in IMAGE_EXTENSIONS:
                try:
                    file_size = file_path.stat().st_size
                    file_path.unlink()
                    num_deleted += 1
                    bytes_deleted += file_size
                except OSError as err:
                    logger.warning("Failed to delete figure %s: %s", file_path, err)

    # Clean up empty subdirectories
    for dir_path in sorted(directory.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if dir_path.is_dir() and not any(dir_path.iterdir()):
            try:
                dir_path.rmdir()
            except OSError:
                pass

    return num_deleted, bytes_deleted


def extract_source_archive(archive_bytes: bytes, target_dir: Path) -> tuple[bool, bool]:
    """Unpack downloaded source archive into `target_dir`.

    Supports `.tar.gz`, `.tar`, and single `.gz` compressed files.
    Ensures safe extraction preventing directory traversal.

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
            for member in tar.getmembers():
                # Prevent directory traversal
                member_path = (target_dir / member.name).resolve()
                if not member_path.is_relative_to(target_dir_resolved):
                    logger.warning("Skipping malicious tar member: %s", member.name)
                    continue

                if member.name.lower().endswith(".tex"):
                    has_tex = True

                # Safe extraction
                if hasattr(tarfile, "data_filter"):
                    tar.extract(member, path=target_dir, filter="data")
                else:
                    tar.extract(member, path=target_dir)

            return True, has_tex
    except (tarfile.TarError, EOFError):
        pass

    # Attempt 2: Single gzipped file (.gz containing plain TeX)
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(archive_bytes)) as gz:
            decompressed = gz.read()
            # If successfully decompressed and has reasonable content
            out_file = target_dir / "main.tex"
            out_file.write_bytes(decompressed)
            has_tex = b"\\documentclass" in decompressed or b"\\begin" in decompressed or True
            return True, has_tex
    except (gzip.BadGzipFile, OSError, EOFError):
        pass

    # Attempt 3: Plain uncompressed text / LaTeX
    if b"\\documentclass" in archive_bytes or b"\\begin{" in archive_bytes or archive_bytes.startswith(b"%"):
        out_file = target_dir / "main.tex"
        out_file.write_bytes(archive_bytes)
        return True, True

    return False, False


import time
from src import config, utils
from src.utils import RateLimiter

ARXIV_SRC_URL = "https://export.arxiv.org/src/{arxiv_id}"
DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) arXiv-Project/1.0 (academic research)"

# Default rate limiter enforcing the mandatory 3-second delay
_default_arxiv_limiter = RateLimiter(config.ARXIV_MIN_INTERVAL)


def download_single_paper(
    arxiv_id: str,
    raw_dir: Optional[Path] = None,
    rate_limiter: Optional[Any] = _default_arxiv_limiter,
    session: Optional[requests.Session] = None,
    timeout: int = config.REQUEST_TIMEOUT,
    user_agent: str = DEFAULT_USER_AGENT,
) -> DownloadStats:
    """Download, extract, and clean the source files of a single arXiv paper.

    Measures performance and attributes matching Table 2 (m1_download_stats):
    - arxiv_id (Primary Key)
    - download_status ('success', 'failed', 'pdf_only')
    - has_tex (True / False)
    - raw_size_bytes (size of downloaded compressed archive)
    - cleaned_size_bytes (size after unpacking and deleting all image files)
    - download_time_sec (elapsed time in seconds)

    Args:
        arxiv_id: Paper identifier, e.g. '2504.12523' or '2504.12523v1'.
        raw_dir: Root directory to save the paper files. Defaults to config.RAW_DIR.
        rate_limiter: Optional RateLimiter instance enforcing minimum request interval.
        session: Optional requests.Session for connection reuse.
        timeout: HTTP request timeout in seconds.
        user_agent: Custom User-Agent header for arXiv politeness.

    Returns:
        DownloadStats object recording all attributes required by Table 2.
    """
    if raw_dir is None:
        raw_dir = config.RAW_DIR
    raw_dir.mkdir(parents=True, exist_ok=True)

    if rate_limiter is not None:
        rate_limiter.wait()

    t0 = time.perf_counter()
    url = ARXIV_SRC_URL.format(arxiv_id=arxiv_id)
    headers = {"User-Agent": user_agent}

    try:
        get_func = session.get if session is not None else requests.get
        response = get_func(url, headers=headers, timeout=timeout)
    except Exception as err:
        logger.error("Network error downloading %s: %s", arxiv_id, err)
        return DownloadStats(
            arxiv_id=arxiv_id,
            download_status="failed",
            has_tex=False,
            raw_size_bytes=0,
            cleaned_size_bytes=0,
            download_time_sec=time.perf_counter() - t0,
        )

    if response.status_code != 200:
        logger.warning("HTTP %d for arXiv ID %s", response.status_code, arxiv_id)
        return DownloadStats(
            arxiv_id=arxiv_id,
            download_status="failed",
            has_tex=False,
            raw_size_bytes=0,
            cleaned_size_bytes=0,
            download_time_sec=time.perf_counter() - t0,
        )

    content = response.content
    raw_size_bytes = len(content)
    content_type = response.headers.get("Content-Type", "").lower()
    is_pdf = "application/pdf" in content_type or content.startswith(b"%PDF")

    paper_folder_name = clean_arxiv_id(arxiv_id)
    paper_dir = raw_dir / paper_folder_name

    if is_pdf:
        # Paper submitted as PDF only. Keep an empty record folder per crawling.md
        paper_dir.mkdir(parents=True, exist_ok=True)
        return DownloadStats(
            arxiv_id=arxiv_id,
            download_status="pdf_only",
            has_tex=False,
            raw_size_bytes=raw_size_bytes,
            cleaned_size_bytes=0,
            download_time_sec=time.perf_counter() - t0,
        )

    # Extract source archive
    extracted_ok, _ = extract_source_archive(content, paper_dir)
    if not extracted_ok:
        logger.warning("Failed to unpack source archive for %s", arxiv_id)
        return DownloadStats(
            arxiv_id=arxiv_id,
            download_status="failed",
            has_tex=False,
            raw_size_bytes=raw_size_bytes,
            cleaned_size_bytes=0,
            download_time_sec=time.perf_counter() - t0,
        )

    # Prune figures/images to save disk storage
    prune_figures(paper_dir)

    # Check for TeX files
    tex_files = list(paper_dir.rglob("*.tex")) + list(paper_dir.rglob("*.TEX"))
    has_tex = len(tex_files) > 0

    # Calculate cleaned size (sum of remaining non-image files)
    cleaned_size_bytes = sum(f.stat().st_size for f in paper_dir.rglob("*") if f.is_file())

    download_status = "success" if has_tex else "failed"

    return DownloadStats(
        arxiv_id=arxiv_id,
        download_status=download_status,
        has_tex=has_tex,
        raw_size_bytes=raw_size_bytes,
        cleaned_size_bytes=cleaned_size_bytes,
        download_time_sec=time.perf_counter() - t0,
    )


def parse_arxiv_range(
    start_id: str | int,
    end_id: str | int,
    month: Optional[str] = None,
    month_sizes: Optional[dict[str, int]] = None,
) -> list[str]:
    """Generate a list of arXiv IDs from start_id to end_id inclusive.

    Supports:
    - String IDs formatted as 'YYMM.NNNNN' (e.g. '2504.12523' to '2504.12530')
    - Integer IDs with explicit `month` parameter (e.g. 12523 to 12530 with month='2504')
    """
    if isinstance(start_id, str) and "." in start_id and isinstance(end_id, str) and "." in end_id:
        s_m, s_n = start_id.strip().split(".", 1)
        e_m, e_n = end_id.strip().split(".", 1)
        rng = config.ArxivRange(s_m, int(s_n), e_m, int(e_n))
        return list(rng.iter_ids(month_sizes=month_sizes))
    elif month is not None:
        rng = config.ArxivRange(month, int(start_id), month, int(end_id))
        return list(rng.iter_ids(month_sizes=month_sizes))
    else:
        raise ValueError("Provide arXiv IDs in 'YYMM.NNNNN' format or specify the month parameter.")


def download_papers_range(
    start_id: str | int,
    end_id: str | int,
    month: Optional[str] = None,
    raw_dir: Optional[Path] = None,
    csv_dir: Optional[Path] = None,
    delay: float = config.ARXIV_MIN_INTERVAL,
    resume: bool = True,
    show_progress: bool = True,
) -> pd.DataFrame:
    """Download, extract, and clean a range of arXiv papers, recording Table 2 statistics.

    Enforces minimum 3-second delay between requests as mandated in crawling.md.
    Extracts TeX source archives, deletes figure binaries to conserve disk space,
    and exports summary statistics to `m1_download_stats.csv`.

    Args:
        start_id: First arXiv ID in the range (e.g. '2504.12523' or integer ID).
        end_id: Last arXiv ID in the range (inclusive).
        month: Required if start_id and end_id are integers without month prefix.
        raw_dir: Path to directory where raw files will be saved. Defaults to config.RAW_DIR.
        csv_dir: Path to directory where CSV summary will be saved. Defaults to DATA_DIR / "csv".
        delay: Delay in seconds between requests (minimum 3.0s per crawling.md).
        resume: If True, skips IDs already recorded in previous runs.
        show_progress: Whether to display a tqdm progress bar.

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

    id_list = parse_arxiv_range(start_id, end_id, month=month)

    done_ids: set[str] = set()
    records: list[dict[str, Any]] = []

    if resume:
        # Load completed IDs from jsonl or csv
        if jsonl_file.exists():
            done_ids = utils.load_done_ids(jsonl_file, key="arxiv_id")
            for record in utils.read_jsonl(jsonl_file):
                if record.get("arxiv_id") in done_ids:
                    records.append(record)
        elif csv_file.exists():
            try:
                existing_df = pd.read_csv(csv_file)
                done_ids = set(existing_df["arxiv_id"].astype(str))
                records = existing_df.to_dict(orient="records")
            except Exception as e:
                logger.warning("Could not read existing CSV for resume: %s", e)

    limiter = RateLimiter(delay) if delay > 0 else None
    session = requests.Session()

    from tqdm import tqdm
    iterator = tqdm(id_list, desc="Downloading arXiv sources", disable=not show_progress)

    for arxiv_id in iterator:
        if resume and arxiv_id in done_ids:
            continue

        stats = download_single_paper(
            arxiv_id=arxiv_id,
            raw_dir=raw_dir,
            rate_limiter=limiter,
            session=session,
        )
        rec = stats.to_dict()
        records.append(rec)
        done_ids.add(arxiv_id)

        # Incrementally persist to jsonl for fault-tolerance
        utils.append_jsonl(jsonl_file, rec)

    session.close()

    # Reorder and filter records to match requested columns
    expected_cols = [
        "arxiv_id",
        "download_status",
        "has_tex",
        "raw_size_bytes",
        "cleaned_size_bytes",
        "download_time_sec",
    ]
    df = pd.DataFrame(records)
    if not df.empty:
        # Deduplicate on arxiv_id preserving latest
        df = df.drop_duplicates(subset=["arxiv_id"], keep="last")
        # Ensure all columns exist
        for col in expected_cols:
            if col not in df.columns:
                df[col] = None
        df = df[expected_cols]
    else:
        df = pd.DataFrame(columns=expected_cols)

    df.to_csv(csv_file, index=False)
    logger.info("Saved %d records to %s", len(df), csv_file)

    return df

