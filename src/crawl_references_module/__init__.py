"""Crawl references module: crawl, parse, and monitor references from Semantic Scholar."""

from src.crawl_references_module.crawler import crawl_references, crawl_references_range, paths_for
from src.crawl_references_module.read_ref import load_references
from src.crawl_references_module.s2_api_client import S2ApiClient
from src.crawl_references_module.s2_client import S2BrowserClient
from src.crawl_references_module.stats import CrawlStats

__all__ = [
    "crawl_references",
    "crawl_references_range",
    "paths_for",
    "load_references",
    "S2ApiClient",
    "S2BrowserClient",
    "CrawlStats",
]
