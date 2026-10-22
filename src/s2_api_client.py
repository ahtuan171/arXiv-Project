"""Semantic Scholar official Graph API client.

Unlike :mod:`src.s2_client`, this client talks to the documented JSON API over
plain HTTPS. It needs ``S2_API_KEY`` for the keyed rate limit, but works without
a key at the much lower anonymous limit.

Endpoints used:

* ``GET /paper/arXiv:<id>`` resolves an arXiv ID to a paper ID.
* ``GET /paper/arXiv:<id>/references`` pages through a paper's references.
* ``POST /paper/batch`` fetches metadata for up to 1000 references at once.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional
from urllib.parse import quote

import httpx

from src import config
from src.s2_client import (
    S2AuthError,
    S2Error,
    S2NotFound,
    S2Paper,
    S2PermanentError,
    S2RateLimited,
    S2TransientError,
)
from src.utils import AsyncRateLimiter, normalize_arxiv_id

logger = logging.getLogger(__name__)


class S2ApiError(S2Error):
    """Base class for official-API errors."""


class S2ApiNotFound(S2ApiError, S2NotFound):
    """The paper or reference is not indexed by Semantic Scholar (HTTP 404)."""


class S2ApiAuthError(S2ApiError, S2AuthError):
    """The API key is missing, invalid or lacks permission (HTTP 401/403)."""


class S2ApiRateLimited(S2ApiError, S2RateLimited):
    """The API quota is exhausted (HTTP 429); ``retry_after`` is honoured."""


class S2ApiTransientError(S2ApiError, S2TransientError):
    """A temporary failure (network, 5xx, malformed body)."""


class S2ApiPermanentError(S2ApiError, S2PermanentError):
    """A request that will fail again unchanged (other 4xx)."""


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return min(max(seconds, 0.0), config.MAX_RETRY_AFTER)


def _authors(value: Any) -> list[str]:
    authors: list[str] = []
    for item in value or []:
        if isinstance(item, str):
            name = item.strip()
        elif isinstance(item, dict):
            name = str(item.get("name") or "").strip()
        else:
            name = ""
        if name:
            authors.append(name)
    return authors


def _paper_from_api(data: dict) -> Optional[S2Paper]:
    """Convert one Graph API paper object into :class:`S2Paper`."""
    paper_id = str(data.get("paperId") or "")
    if not paper_id:
        return None
    external = data.get("externalIds")
    arxiv_id = None
    if isinstance(external, dict):
        raw_arxiv = external.get("ArXiv") or external.get("arxiv")
        arxiv_id = normalize_arxiv_id(raw_arxiv) or None
    publication_date = str(data.get("publicationDate") or "")
    year = data.get("year")
    if not isinstance(year, int):
        year = int(year) if isinstance(year, str) and year.isdigit() else None
    if year is None and len(publication_date) >= 4 and publication_date[:4].isdigit():
        year = int(publication_date[:4])
    return S2Paper(
        paper_id=paper_id,
        arxiv_id=arxiv_id,
        title=str(data.get("title") or ""),
        authors=_authors(data.get("authors")),
        year=year,
        publication_date=publication_date or None,
    )


class S2ApiClient:
    """Concurrent-safe client for the Semantic Scholar Graph API."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        min_interval: Optional[float] = None,
        timeout: float = config.S2_API_TIMEOUT,
        page_size: int = config.S2_API_PAGE_SIZE,
        batch_size: int = config.S2_API_BATCH_SIZE,
        max_retries: int = config.S2_API_MAX_RETRIES,
        base_url: str = config.S2_API_BASE,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.api_key = (api_key if api_key is not None else config.S2_API_KEY).strip()
        if min_interval is None:
            min_interval = config.S2_API_MIN_INTERVAL
        self.rate_limiter = AsyncRateLimiter(max(0.0, float(min_interval)))
        self.timeout = timeout
        self.page_size = max(1, min(int(page_size), 1000))
        self.batch_size = max(1, min(int(batch_size), 1000))
        self.max_retries = max(1, int(max_retries))
        self.base_url = base_url.rstrip("/")
        self.pages: list[Any] = []
        self.api_calls = 0
        self.auth_errors = 0
        self.rate_limit_hits = 0
        self._http = http_client
        self._owns_http = http_client is None
        self._paper_id_cache: dict[str, str] = {}
        self._ref_cache: dict[str, S2Paper] = {}
        if not self.api_key:
            logger.warning(
                "S2_API_KEY is not set; using the anonymous Semantic Scholar quota"
            )

    async def start(self, tabs: int = 1) -> "S2ApiClient":
        """Create the HTTP client and expose ``tabs`` placeholder pages."""
        if self._http is None:
            headers = {"Accept": "application/json"}
            if self.api_key:
                headers["x-api-key"] = self.api_key
            self._http = httpx.AsyncClient(
                base_url=self.base_url,
                headers=headers,
                timeout=self.timeout,
            )
        count = max(1, int(tabs or 1))
        self.pages = [None] * count
        logger.info(
            "Using Semantic Scholar API at %.2fs/request (page_size=%d, batch_size=%d)",
            self.rate_limiter.min_interval, self.page_size, self.batch_size,
        )
        return self

    async def close(self) -> None:
        """Close the HTTP client if this instance created it."""
        if self._http is not None and self._owns_http:
            try:
                await self._http.aclose()
            except Exception:
                pass
        self._http = None
        self.pages = []

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        payload: Optional[dict] = None,
    ) -> Any:
        """Perform one API request, retrying transient failures.

        404 and 401/403 are never retried. 429 honours ``Retry-After`` and
        pauses every worker through the shared rate limiter.
        """
        if self._http is None:
            raise S2ApiTransientError("S2ApiClient.start() was not called")

        last_error: Optional[S2ApiTransientError] = None
        for attempt in range(1, self.max_retries + 1):
            await self.rate_limiter.wait()
            self.api_calls += 1
            retry_after: Optional[float] = None
            try:
                response = await self._http.request(
                    method, path, params=params, json=payload,
                )
            except httpx.HTTPError as error:
                last_error = S2ApiTransientError(f"API request failed: {error}")
            else:
                status = response.status_code
                logger.debug("S2 API %s %s -> %d", method, path, status)
                if status == 404:
                    raise S2ApiNotFound(f"not found: {path}")
                if status in (401, 403):
                    self.auth_errors += 1
                    raise S2ApiAuthError(f"HTTP {status} from Semantic Scholar API")
                if status == 429:
                    self.rate_limit_hits += 1
                    retry_after = _parse_retry_after(response.headers.get("retry-after"))
                    last_error = S2ApiRateLimited(
                        f"rate limited (Retry-After={retry_after})",
                        retry_after=retry_after,
                    )
                elif 500 <= status < 600:
                    last_error = S2ApiTransientError(f"HTTP {status} from Semantic Scholar API")
                elif status >= 400:
                    body = response.text[:200].replace("\n", " ")
                    raise S2ApiPermanentError(f"HTTP {status} from Semantic Scholar API: {body}")
                else:
                    try:
                        data = response.json()
                    except ValueError:
                        last_error = S2ApiTransientError("invalid JSON from Semantic Scholar API")
                    else:
                        if not isinstance(data, (dict, list)):
                            last_error = S2ApiTransientError("unexpected API response shape")
                        else:
                            return data

            if attempt == self.max_retries:
                assert last_error is not None
                raise last_error

            if retry_after is not None:
                delay = float(retry_after)
            else:
                delay = config.BACKOFF_BASE ** attempt
            if retry_after is not None:
                self.rate_limiter.block(delay)
            delay = min(max(delay, 0.0), config.MAX_BACKOFF)
            delay += config.RETRY_JITTER
            logger.warning(
                "%s failed (%s); retry %d/%d in %.1fs",
                path, last_error, attempt, self.max_retries, delay,
            )
            await asyncio.sleep(delay)

        raise last_error or S2ApiTransientError("API request failed")

    async def resolve_paper_id(self, arxiv_id: str) -> Optional[str]:
        """Resolve an arXiv ID to an S2 paper ID, or ``None`` if not indexed."""
        clean_id = self._clean_id(arxiv_id)
        if clean_id in self._paper_id_cache:
            return self._paper_id_cache[clean_id]
        try:
            data = await self._request(
                "GET",
                f"/paper/arXiv:{quote(clean_id, safe='/')}",
                params={"fields": "paperId"},
            )
        except S2ApiNotFound:
            return None
        paper_id = str(data.get("paperId") or "") if isinstance(data, dict) else ""
        if not paper_id:
            raise S2ApiPermanentError(f"no paperId for arXiv:{clean_id}")
        self._paper_id_cache[clean_id] = paper_id
        logger.debug("Resolved arXiv:%s -> %s", clean_id, paper_id)
        return paper_id

    async def fetch_papers_batch(self, paper_ids: list[str]) -> list[S2Paper]:
        """Fetch metadata for many paper IDs, using the cache when possible."""
        unique = [pid for pid in dict.fromkeys(str(pid) for pid in paper_ids if pid)]
        pending = [pid for pid in unique if pid not in self._ref_cache]
        for start in range(0, len(pending), self.batch_size):
            batch = pending[start:start + self.batch_size]
            data = await self._request(
                "POST",
                "/paper/batch",
                params={"fields": config.S2_API_PAPER_FIELDS},
                payload={"ids": batch},
            )
            if not isinstance(data, list):
                raise S2ApiTransientError("unexpected batch response from Semantic Scholar API")
            for item in data:
                if not isinstance(item, dict):
                    continue
                paper = _paper_from_api(item)
                if paper is not None:
                    self._ref_cache[paper.paper_id] = paper
            logger.debug(
                "Batch resolved %d/%d paper(s)",
                sum(1 for pid in batch if pid in self._ref_cache), len(batch),
            )
        return [self._ref_cache[pid] for pid in unique if pid in self._ref_cache]

    async def get_papers_batch(self, paper_ids: list[str]) -> list[S2Paper]:
        """Public alias for :meth:`fetch_papers_batch`."""
        return await self.fetch_papers_batch(paper_ids)

    async def get_paper_by_arxiv(self, arxiv_id: str) -> Optional[S2Paper]:
        """Fetch one paper's metadata; return ``None`` when S2 lacks it."""
        paper_id = await self.resolve_paper_id(arxiv_id)
        if paper_id is None:
            return None
        if paper_id in self._ref_cache:
            return self._ref_cache[paper_id]
        papers = await self.fetch_papers_batch([paper_id])
        return papers[0] if papers else None

    async def get_references(self, page: Any, arxiv_id: str) -> list[S2Paper]:
        """Fetch every reference for one paper.

        ``page`` is accepted and ignored so the API client can be dropped into
        the crawler's tab-oriented worker loop.
        """
        del page
        clean_id = self._clean_id(arxiv_id)
        paper_ids: list[str] = []
        seen: set[str] = set()
        offset = 0

        while True:
            data = await self._request(
                "GET",
                f"/paper/arXiv:{quote(clean_id, safe='/')}/references",
                params={
                    "fields": "paperId",
                    "offset": offset,
                    "limit": self.page_size,
                },
            )
            if not isinstance(data, dict):
                raise S2ApiTransientError("unexpected references response from Semantic Scholar API")
            items = data.get("data")
            if not isinstance(items, list):
                raise S2ApiTransientError("unexpected references payload from Semantic Scholar API")
            for item in items:
                if not isinstance(item, dict):
                    continue
                item_id = str(item.get("paperId") or "")
                if item_id and item_id not in seen:
                    seen.add(item_id)
                    paper_ids.append(item_id)

            next_offset = data.get("next")
            if not items:
                break
            if isinstance(next_offset, int) and next_offset > offset:
                offset = next_offset
                continue
            offset += len(items)
            if len(items) < self.page_size:
                break

        await self.fetch_papers_batch(paper_ids)

        refs: list[S2Paper] = []
        for paper_id in paper_ids:
            cached = self._ref_cache.get(paper_id)
            if cached is not None:
                refs.append(cached)
            else:
                refs.append(S2Paper(
                    paper_id=paper_id,
                    arxiv_id=None,
                    title="",
                    authors=[],
                    year=None,
                    publication_date=None,
                ))
        return refs

    @staticmethod
    def _clean_id(arxiv_id: str) -> str:
        clean_id = normalize_arxiv_id(arxiv_id)
        if not clean_id:
            raise ValueError("empty arXiv ID")
        return clean_id
