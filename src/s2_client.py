"""Semantic Scholar API client for fetching paper references."""

from __future__ import annotations

import os
import time
import logging
from dataclasses import dataclass
from typing import Optional

import requests

from src import config
from src.utils import RateLimiter, retry

logger = logging.getLogger(__name__)


@dataclass
class S2Paper:
    paper_id: str
    arxiv_id: Optional[str]
    title: str
    authors: list[str]
    year: Optional[int]
    publication_date: Optional[str]


class S2Client:
    """Client for Semantic Scholar Graph API."""

    BASE_URL = "https://api.semanticscholar.org/graph/v1"

    def __init__(
        self,
        api_key: str = "",
        min_interval: float = config.S2_MIN_INTERVAL,
        timeout: int = config.REQUEST_TIMEOUT,
    ):
        self.api_key = api_key or os.environ.get("S2_API_KEY", "")
        self.rate_limiter = RateLimiter(min_interval)
        self.timeout = timeout
        self.session = requests.Session()
        if self.api_key:
            self.session.headers["x-api-key"] = self.api_key

    def _get(self, endpoint: str, params: dict) -> dict:
        url = f"{self.BASE_URL}{endpoint}"
        self.rate_limiter.wait()
        resp = self.session.get(url, params=params, timeout=self.timeout)
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", "60"))
            logger.warning("Rate limited, waiting %ds", retry_after)
            time.sleep(retry_after)
            self.rate_limiter.wait()
            resp = self.session.get(url, params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    @retry(max_attempts=config.MAX_RETRIES, backoff=config.BACKOFF_BASE)
    def get_paper_by_arxiv(self, arxiv_id: str) -> Optional[S2Paper]:
        """Fetch paper metadata by arXiv ID."""
        clean_id = arxiv_id.replace("_", ".").replace("v", "v")  # keep version
        fields = "paperId,title,authors,year,publicationDate,externalIds"
        params = {"fields": fields}
        data = self._get(f"/paper/ARXIV:{clean_id}", params)
        if not data:
            return None
        return self._parse_paper(data)

    @retry(max_attempts=config.MAX_RETRIES, backoff=config.BACKOFF_BASE)
    def get_references(self, arxiv_id: str) -> list[S2Paper]:
        """Fetch all references of a paper by arXiv ID."""
        clean_id = arxiv_id.replace("_", ".").replace("v", "v")
        fields = "paperId,title,authors,year,publicationDate,externalIds"
        params = {"fields": fields, "limit": 1000}
        data = self._get(f"/paper/ARXIV:{clean_id}/references", params)
        refs = []
        for item in data.get("data", []):
            cited = item.get("citedPaper")
            if cited:
                paper = self._parse_paper(cited)
                if paper:
                    refs.append(paper)
        return refs

    def _parse_paper(self, data: dict) -> Optional[S2Paper]:
        paper_id = data.get("paperId")
        if not paper_id:
            return None
        arxiv_id = data.get("externalIds", {}).get("ArXiv")
        title = data.get("title", "")
        authors = [a.get("name", "") for a in data.get("authors", []) if a.get("name")]
        year = data.get("year")
        pub_date = data.get("publicationDate")
        return S2Paper(
            paper_id=paper_id,
            arxiv_id=arxiv_id,
            title=title,
            authors=authors,
            year=year,
            publication_date=pub_date,
        )