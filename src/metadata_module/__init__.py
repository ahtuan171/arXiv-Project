"""Metadata module for crawling and processing arXiv papers metadata and references."""

from __future__ import annotations

import importlib
from typing import Any

__all__ = [
    "build_record",
    "crawl_papers",
    "error_record",
    "extract_version_count",
    "try_fetch_arxiv",
    "crawl_references",
    "normalize_ref_arxiv_id",
    "parse_refs",
    "try_fetch_s2",
    "get_paper",
    "get_refs",
    "iter_arxiv_ref_ids",
    "load_papers",
    "load_references",
    "papers_index",
    "refs_index",
    "summarize",
    "MONTH_MAX",
    "PARTS",
    "generate_id_list",
    "ids_in_range",
    "yymm",
]

_MAPPING = {
    "build_record": "crawl_papers",
    "crawl_papers": "crawl_papers",
    "error_record": "crawl_papers",
    "extract_version_count": "crawl_papers",
    "try_fetch_arxiv": "crawl_papers",
    "crawl_references": "crawl_references",
    "normalize_ref_arxiv_id": "crawl_references",
    "parse_refs": "crawl_references",
    "try_fetch_s2": "crawl_references",
    "get_paper": "discovery",
    "get_refs": "discovery",
    "iter_arxiv_ref_ids": "discovery",
    "load_papers": "discovery",
    "load_references": "discovery",
    "papers_index": "discovery",
    "refs_index": "discovery",
    "summarize": "discovery",
    "MONTH_MAX": "make_id_list",
    "PARTS": "make_id_list",
    "generate_id_list": "make_id_list",
    "ids_in_range": "make_id_list",
    "yymm": "make_id_list",
}


def __getattr__(name: str) -> Any:
    if name in _MAPPING:
        mod = importlib.import_module(f"src.metadata_module.{_MAPPING[name]}")
        val = getattr(mod, name)
        globals()[name] = val
        return val
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
