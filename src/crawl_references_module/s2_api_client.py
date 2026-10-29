"""Semantic Scholar official Graph API client.

Unlike :mod:`src.crawl_references_module.s2_client`, this client talks to the documented JSON API over
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
import random
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Iterable, Optional
from urllib.parse import quote

import httpx

from src import config
from src.crawl_references_module.s2_client import (
    S2AuthError,
    S2Error,
    S2NotFound,
    S2Paper,
    S2PermanentError,
    S2RateLimited,
    S2TransientError,
)
from src.utils import AsyncRateLimiter, is_valid_arxiv_id, normalize_arxiv_id

logger = logging.getLogger(__name__)


class S2ApiError(S2Error):
    """Base class for official-API errors."""


class S2ApiNotFound(S2ApiError, S2NotFound):
    """The paper or reference is not indexed by Semantic Scholar (HTTP 404)."""


class S2ApiAuthError(S2ApiError, S2AuthError):
    """The API key is missing, invalid or lacks permission (HTTP 401/403)."""


class S2ApiRateLimited(S2ApiError, S2RateLimited):
    """The API quota is exhausted (HTTP 429); retried with exponential backoff and jitter."""


class S2ApiPayloadTooLarge(S2ApiError, S2TransientError):
    """The request or response payload exceeded the size limit (HTTP 413 or >10MB)."""


class S2ApiTransientError(S2ApiError, S2TransientError):
    """A temporary failure (network, 5xx, malformed body)."""


class S2ApiPermanentError(S2ApiError, S2PermanentError):
    """A request that will fail again unchanged (other 4xx)."""


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
    paper_id = str(data.get("paperId") or "").strip()
    external = data.get("externalIds")
    arxiv_id = None
    if isinstance(external, dict):
        raw_arxiv = external.get("ArXiv") or external.get("arxiv")
        candidate = normalize_arxiv_id(raw_arxiv) or None
        if candidate and is_valid_arxiv_id(candidate):
            arxiv_id = candidate
    # Problem B1: If paper_id is missing but arxiv_id is valid, retain it
    if not paper_id and not arxiv_id:
        return None
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


def is_sufficiently_complete(paper: S2Paper, raw_data: Optional[dict] = None) -> bool:
    """Check if an S2Paper record has sufficient metadata for the project schema.

    A record is considered sufficiently complete when:
    1. It has a non-empty title.
    2. If raw_data is provided, externalIds was returned in the response.
    3. If it has an arXiv ID, it has authors and publication date (or year).
    """
    if not paper.title:
        return False
    if raw_data is not None:
        if "externalIds" not in raw_data:
            return False
    if paper.arxiv_id:
        if not paper.publication_date and paper.year is None:
            return False
        if not paper.authors:
            return False
    return True


def merge_paper(base: Optional[S2Paper], update: Optional[S2Paper]) -> Optional[S2Paper]:
    """Merge fallback paper metadata into base without overwriting valid data with empty/None."""
    if base is None:
        return update
    if update is None:
        return base
    return S2Paper(
        paper_id=base.paper_id or update.paper_id or "",
        arxiv_id=base.arxiv_id or update.arxiv_id,
        title=base.title if base.title else update.title,
        authors=list(base.authors) if base.authors else list(update.authors),
        year=base.year if base.year is not None else update.year,
        publication_date=base.publication_date if base.publication_date else update.publication_date,
    )


class ReferenceResult(list):
    """List of S2Paper references with per-paper API diagnostics and metrics."""

    def __init__(
        self,
        papers: Iterable[S2Paper] = (),
        *,
        raw_refs_received: int = 0,
        refs_with_paper_id: int = 0,
        refs_with_arxiv_id: int = 0,
        refs_unresolved: int = 0,
        unresolved_details: Optional[list[dict]] = None,
        fallback_requests: int = 0,
    ) -> None:
        super().__init__(papers)
        self.raw_refs_received = raw_refs_received
        self.refs_with_paper_id = refs_with_paper_id
        self.refs_with_arxiv_id = refs_with_arxiv_id
        self.refs_unresolved = refs_unresolved
        self.unresolved_details = list(unresolved_details or [])
        self.fallback_requests = fallback_requests


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
            min_interval = config.S2_API_MIN_INTERVAL if self.api_key else config.S2_API_ANONYMOUS_MIN_INTERVAL
        self.rate_limiter = AsyncRateLimiter(max(0.0, float(min_interval)))
        self.timeout = timeout
        self.page_size = max(1, min(int(page_size), 1000))
        self.batch_size = max(1, min(int(batch_size), config.S2_API_MAX_BATCH_SIZE))
        self.max_retries = max(1, int(max_retries))
        self.base_url = base_url.rstrip("/")
        self.pages: list[Any] = []
        self.api_calls = 0
        self.auth_errors = 0
        self.rate_limit_hits = 0
        self.fallback_requests = 0
        self._http = http_client
        self._owns_http = http_client is None
        self._paper_id_cache: dict[str, str] = {}
        self._ref_cache: dict[str, S2Paper] = {}
        if not self.api_key:
            logger.warning(
                "S2_API_KEY is not set; using the anonymous Semantic Scholar quota (%.2fs/req)",
                self.rate_limiter.min_interval,
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

        404, 401/403, and 413 (>10MB payload) are never retried directly.
        429 pauses every worker using exponential backoff with jitter and ceiling.
        """
        if self._http is None:
            raise S2ApiTransientError("S2ApiClient.start() was not called")

        last_error: Optional[S2ApiTransientError] = None
        for attempt in range(1, self.max_retries + 1):
            await self.rate_limiter.wait()
            self.api_calls += 1
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
                if status == 413:
                    raise S2ApiPayloadTooLarge(f"HTTP 413 from Semantic Scholar API: payload too large for {path}")
                if status == 429:
                    self.rate_limit_hits += 1
                    last_error = S2ApiRateLimited("rate limited (HTTP 429)")
                elif 500 <= status < 600:
                    last_error = S2ApiTransientError(f"HTTP {status} from Semantic Scholar API")
                elif status >= 400:
                    body = response.text[:200].replace("\n", " ")
                    raise S2ApiPermanentError(f"HTTP {status} from Semantic Scholar API: {body}")
                else:
                    content_len_hdr = response.headers.get("content-length")
                    if content_len_hdr and content_len_hdr.isdigit() and int(content_len_hdr) > config.S2_API_MAX_PAYLOAD_BYTES:
                        raise S2ApiPayloadTooLarge(
                            f"response Content-Length ({content_len_hdr} bytes) exceeds limit ({config.S2_API_MAX_PAYLOAD_BYTES} bytes)"
                        )
                    if len(response.content) > config.S2_API_MAX_PAYLOAD_BYTES:
                        raise S2ApiPayloadTooLarge(
                            f"response body length ({len(response.content)} bytes) exceeds limit ({config.S2_API_MAX_PAYLOAD_BYTES} bytes)"
                        )
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

            is_rate_limited = isinstance(last_error, S2ApiRateLimited)
            if is_rate_limited:
                base_wait = max(config.S2_RATE_LIMIT_COOLDOWN, config.BACKOFF_BASE)
                delay = base_wait * (config.BACKOFF_BASE ** (attempt - 1))
            else:
                delay = config.BACKOFF_BASE ** attempt

            # Capped exponential backoff (thời gian trần) + random jitter
            delay = min(max(delay, 0.0), config.MAX_BACKOFF)
            delay += random.uniform(0.0, config.RETRY_JITTER)

            if is_rate_limited:
                self.rate_limiter.block(delay)

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

    async def _fetch_chunk_with_subdivision(
        self,
        chunk: list[str],
        fields_str: str,
    ) -> list[Optional[dict]]:
        """Fetch a chunk of paper IDs (<= 500), recursively subdividing on 413, >10MB, or timeout."""
        if not chunk:
            return []
        try:
            data = await self._request(
                "POST",
                "/paper/batch",
                params={"fields": fields_str},
                payload={"ids": chunk},
            )
            if not isinstance(data, list):
                raise S2ApiTransientError("unexpected batch response from Semantic Scholar API")
            return data
        except (S2ApiPayloadTooLarge, httpx.TimeoutException) as err:
            if len(chunk) <= 1:
                logger.warning(
                    "Single paper batch %r failed with %s; returning empty result",
                    chunk[0] if chunk else "", err,
                )
                return [None] * len(chunk)
            mid = len(chunk) // 2
            logger.warning(
                "Batch of %d papers encountered %s; subdividing into %d and %d",
                len(chunk), type(err).__name__, mid, len(chunk) - mid,
            )
            left = await self._fetch_chunk_with_subdivision(chunk[:mid], fields_str)
            right = await self._fetch_chunk_with_subdivision(chunk[mid:], fields_str)
            return left + right

    async def fetch_papers_batch(
        self,
        paper_ids: list[str],
        fields: Optional[str] = None,
    ) -> list[S2Paper]:
        """Fetch metadata for many paper IDs, supporting up to 500 per request,
        flexible identifiers (ARXIV:, CorpusId:, SHA-40, etc.), adaptive subdivision
        under 10MB, and in-memory caching.
        """
        unique = [pid for pid in dict.fromkeys(str(pid).strip() for pid in paper_ids if pid and str(pid).strip())]
        fields_str = fields if fields is not None else config.S2_API_PAPER_FIELDS

        if fields is None:
            pending = [pid for pid in unique if pid not in self._ref_cache]
        else:
            pending = list(unique)

        for start in range(0, len(pending), self.batch_size):
            batch = pending[start:start + self.batch_size]
            items = await self._fetch_chunk_with_subdivision(batch, fields_str)
            for query_id, item in zip(batch, items):
                if not isinstance(item, dict):
                    continue
                paper = _paper_from_api(item)
                if paper is not None:
                    cached = self._ref_cache.get(paper.paper_id) if paper.paper_id else None
                    merged = merge_paper(cached, paper) if cached else paper
                    if merged is not None:
                        # Cache under original query identifier
                        self._ref_cache[query_id] = merged
                        # Cache under S2 paperId
                        if merged.paper_id:
                            self._ref_cache[merged.paper_id] = merged
                        # Cache under arXiv variations if available
                        if merged.arxiv_id:
                            self._ref_cache[f"ARXIV:{merged.arxiv_id}"] = merged
                            self._ref_cache[f"arxiv:{merged.arxiv_id}"] = merged
                            self._ref_cache[merged.arxiv_id] = merged

            logger.debug(
                "Batch resolved %d/%d paper(s)",
                sum(1 for pid in batch if pid in self._ref_cache), len(batch),
            )

        resolved: list[S2Paper] = []
        seen_keys: set[str] = set()
        for pid in unique:
            p = self._ref_cache.get(pid)
            if p is not None:
                key = p.paper_id or p.arxiv_id or pid
                if key not in seen_keys:
                    seen_keys.add(key)
                    resolved.append(p)
        return resolved

    async def fetch_papers_batch_raw(
        self,
        paper_ids: list[str],
        fields: Optional[str] = None,
    ) -> list[Optional[dict]]:
        """Fetch raw metadata dictionaries matching input order 1-to-1."""
        unique = [str(pid).strip() for pid in paper_ids if pid and str(pid).strip()]
        fields_str = fields if fields is not None else config.S2_API_PAPER_FIELDS
        results: list[Optional[dict]] = []
        for start in range(0, len(unique), self.batch_size):
            batch = unique[start:start + self.batch_size]
            items = await self._fetch_chunk_with_subdivision(batch, fields_str)
            results.extend(items)
        return results

    async def get_papers_batch(
        self,
        paper_ids: list[str],
        fields: Optional[str] = None,
    ) -> list[S2Paper]:
        """Public alias for :meth:`fetch_papers_batch`."""
        return await self.fetch_papers_batch(paper_ids, fields=fields)

    async def get_paper_by_arxiv(self, arxiv_id: str) -> Optional[S2Paper]:
        """Fetch one paper's metadata; return ``None`` when S2 lacks it."""
        paper_id = await self.resolve_paper_id(arxiv_id)
        if paper_id is None:
            return None
        if paper_id in self._ref_cache:
            return self._ref_cache[paper_id]
        papers = await self.fetch_papers_batch([paper_id])
        return papers[0] if papers else None

    async def get_references(self, page: Any, arxiv_id: str) -> ReferenceResult:
        """Fetch every reference for one paper.

        ``page`` is accepted and ignored so the API client can be dropped into
        the crawler's tab-oriented worker loop.
        """
        del page
        clean_id = self._clean_id(arxiv_id)

        raw_refs_received = 0
        refs_with_paper_id = 0
        refs_unresolved = 0
        unresolved_details: list[dict] = []

        paper_keys: list[str] = []
        seen_keys: set[str] = set()
        direct_papers: dict[str, S2Paper] = {}
        needs_fallback: list[str] = []

        initial_fallback_count = self.fallback_requests
        offset = 0

        while True:
            data = await self._request(
                "GET",
                f"/paper/arXiv:{quote(clean_id, safe='/')}/references",
                params={
                    "fields": config.S2_API_REF_FIELDS,
                    "offset": offset,
                    "limit": self.page_size,
                },
            )
            if not isinstance(data, dict):
                raise S2ApiTransientError("unexpected references response from Semantic Scholar API")
            items = data.get("data")
            if not isinstance(items, list):
                raise S2ApiTransientError("unexpected references payload from Semantic Scholar API")

            raw_refs_received += len(items)

            for item in items:
                if not isinstance(item, dict):
                    refs_unresolved += 1
                    unresolved_details.append({
                        "source_arxiv_id": clean_id,
                        "title": "",
                        "year": None,
                        "external_ids": None,
                        "reason": f"non_dict_entry:{type(item).__name__}",
                    })
                    logger.warning("Ignoring non-dict reference entry for arXiv:%s: %r", clean_id, type(item))
                    continue

                cited = item.get("citedPaper")
                if not isinstance(cited, dict):
                    refs_unresolved += 1
                    unresolved_details.append({
                        "source_arxiv_id": clean_id,
                        "title": "",
                        "year": None,
                        "external_ids": None,
                        "reason": "missing_or_null_cited_paper",
                    })
                    logger.warning(
                        "Ignoring reference entry without valid citedPaper dict for arXiv:%s: %r",
                        clean_id,
                        item,
                    )
                    continue

                raw_id = str(cited.get("paperId") or "").strip()
                external = cited.get("externalIds")
                raw_arxiv = None
                if isinstance(external, dict):
                    raw_arxiv = external.get("ArXiv") or external.get("arxiv")
                arxiv_id = normalize_arxiv_id(raw_arxiv) or None

                if raw_id:
                    refs_with_paper_id += 1
                    item_key = raw_id
                elif arxiv_id and is_valid_arxiv_id(arxiv_id):
                    # Problem B1: missing paperId but externalIds has valid arXiv ID
                    item_key = f"arxiv:{arxiv_id}"
                else:
                    # Problem B1: unresolvable reference entry
                    refs_unresolved += 1
                    unresolved_details.append({
                        "source_arxiv_id": clean_id,
                        "title": str(cited.get("title") or ""),
                        "year": cited.get("year"),
                        "external_ids": external if isinstance(external, dict) else None,
                        "reason": "invalid_arxiv_id" if arxiv_id else "missing_paper_id_and_arxiv_id",
                    })
                    logger.warning("Ignoring unresolvable reference for arXiv:%s: %r", clean_id, item)
                    continue

                if item_key not in seen_keys:
                    seen_keys.add(item_key)
                    paper_keys.append(item_key)

                    paper = _paper_from_api(cited)
                    if paper is not None:
                        direct_papers[item_key] = paper
                        if is_sufficiently_complete(paper, cited):
                            self._ref_cache[item_key] = paper
                        elif raw_id:
                            needs_fallback.append(raw_id)
                    elif raw_id:
                        needs_fallback.append(raw_id)

            next_offset = data.get("next")
            if not items:
                break
            if isinstance(next_offset, int) and next_offset > offset:
                offset = next_offset
                continue
            offset += len(items)
            if len(items) < self.page_size:
                break

        # Problem B2: Fallback batch lookup only for incomplete records with paperId
        pending_fallback = [pid for pid in needs_fallback if pid not in self._ref_cache]
        if pending_fallback:
            self.fallback_requests += 1
            batch_results = await self.fetch_papers_batch(pending_fallback)
            batch_map = {p.paper_id: p for p in batch_results if p.paper_id}
            for pid in pending_fallback:
                base = direct_papers.get(pid)
                fallback_paper = batch_map.get(pid)
                merged = merge_paper(base, fallback_paper)
                if merged is not None:
                    self._ref_cache[pid] = merged

        refs: list[S2Paper] = []
        refs_with_arxiv_id = 0
        for key in paper_keys:
            cached = self._ref_cache.get(key) or direct_papers.get(key)
            if cached is not None:
                refs.append(cached)
                if cached.arxiv_id:
                    refs_with_arxiv_id += 1
            else:
                p = S2Paper(
                    paper_id=key if not key.startswith("arxiv:") else "",
                    arxiv_id=key[len("arxiv:"):] if key.startswith("arxiv:") else None,
                    title="",
                    authors=[],
                    year=None,
                    publication_date=None,
                )
                refs.append(p)
                if p.arxiv_id:
                    refs_with_arxiv_id += 1

        per_paper_fallback = self.fallback_requests - initial_fallback_count

        return ReferenceResult(
            refs,
            raw_refs_received=raw_refs_received,
            refs_with_paper_id=refs_with_paper_id,
            refs_with_arxiv_id=refs_with_arxiv_id,
            refs_unresolved=refs_unresolved,
            unresolved_details=unresolved_details,
            fallback_requests=per_paper_fallback,
        )

    @staticmethod
    def _clean_id(arxiv_id: str) -> str:
        clean_id = normalize_arxiv_id(arxiv_id)
        if not clean_id:
            raise ValueError("empty arXiv ID")
        return clean_id
