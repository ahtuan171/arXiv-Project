"""Metadata module for crawling and processing arXiv papers metadata."""

from __future__ import annotations

import importlib
from typing import Any

__all__ = [
    "build_record",
    "crawl_papers",
    "error_record",
    "extract_version_count",
    "try_fetch_arxiv",
    "get_paper",
    "get_refs",
    "iter_arxiv_ref_ids",
    "load_papers",
    "load_references",
    "papers_index",
    "refs_index",
    "summarize",
    "parse_arxiv_range",
]

_MAPPING = {
    "build_record": "crawl_papers",
    "crawl_papers": "crawl_papers",
    "error_record": "crawl_papers",
    "extract_version_count": "crawl_papers",
    "try_fetch_arxiv": "crawl_papers",
    "get_paper": "discovery",
    "get_refs": "discovery",
    "iter_arxiv_ref_ids": "discovery",
    "load_papers": "discovery",
    "load_references": "discovery",
    "papers_index": "discovery",
    "refs_index": "discovery",
    "summarize": "discovery",
    "parse_arxiv_range": "src.utils",
}


def __getattr__(name: str) -> Any:
    if name in _MAPPING:
        target = _MAPPING[name]
        if target.startswith("src."):
            mod = importlib.import_module(target)
        else:
            mod = importlib.import_module(f"src.metadata_module.{target}")
        val = getattr(mod, name)
        globals()[name] = val
        return val
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
