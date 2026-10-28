"""Download module for crawling and extracting arXiv source files."""

from src.download_module.downloader import (
    DownloadStats,
    clean_arxiv_id,
    download_papers_range,
    download_single_paper,
    extract_source_archive,
    parse_arxiv_range,
)

__all__ = [
    "DownloadStats",
    "clean_arxiv_id",
    "download_papers_range",
    "download_single_paper",
    "extract_source_archive",
    "parse_arxiv_range",
]
