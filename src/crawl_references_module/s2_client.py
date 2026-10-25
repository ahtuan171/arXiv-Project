"""Playwright-based Semantic Scholar website client.

The public HTML paper/search pages are protected by AWS WAF, but the site's
own JSON endpoints are reachable without an API key:

* ``https://api.semanticscholar.org/arXiv:<id>`` redirects to the paper URL and
  therefore resolves an arXiv ID to a Semantic Scholar paper ID.
* ``/api/1/paper/<id>/citations?citationType=citedPapers`` returns references.
* ``/api/1/paper/batch-v2`` resolves reference metadata, including arXiv links.

One Chromium instance owns several tabs. Each tab is a Playwright request
context, so the browser handles cookies/TLS while the crawler gets structured
data. Tabs are reused until the crawl finishes and are closed by :meth:`close`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import quote, unquote, urlparse

from src import config
from src.utils import AsyncRateLimiter, normalize_arxiv_id

logger = logging.getLogger(__name__)


class S2Error(Exception):
    """Base class for Semantic Scholar client errors."""


class S2NotFound(S2Error):
    """The paper is not indexed by Semantic Scholar (HTTP 404)."""


class S2AuthError(S2Error):
    """Semantic Scholar rejected the request (HTTP 401/403)."""


class S2PermanentError(S2Error):
    """A request that will fail again if retried unchanged (other 4xx)."""


class S2TransientError(S2Error):
    """A temporary failure (network, 5xx, malformed body, 429)."""

    def __init__(self, message: str, retry_after: Optional[float] = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class S2RateLimited(S2TransientError):
    """Semantic Scholar answered HTTP 429."""


class S2WafChallenge(S2TransientError):
    """AWS WAF returned a challenge instead of the requested JSON."""


@dataclass
class S2Paper:
    paper_id: str
    arxiv_id: Optional[str]
    title: str
    authors: list[str]
    year: Optional[int]
    publication_date: Optional[str]


_PAPER_PATH_RE = re.compile(r"/paper/([0-9a-f]{40})")


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return min(max(seconds, 0.0), config.MAX_RETRY_AFTER)


def _text(value: Any) -> str:
    """Extract text from the website's ``{"text": ...}`` wrappers."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return _text(value.get("text"))
    return ""


def _year(value: Any) -> Optional[int]:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    text = _text(value)
    return int(text) if text.isdigit() else None


def _authors(value: Any) -> list[str]:
    """Parse both website shapes: ``[{name}]`` and ``[[{name}, ...], ...]``."""
    authors: list[str] = []
    for item in value or []:
        if isinstance(item, str):
            name = item.strip()
        elif isinstance(item, dict):
            name = _text(item.get("name"))
        elif isinstance(item, list):
            name = ""
            for part in item:
                if isinstance(part, dict) and part.get("name"):
                    name = _text(part.get("name"))
                    break
        else:
            name = ""
        if name:
            authors.append(name)
    return authors


def _extract_arxiv_id(data: dict) -> Optional[str]:
    """Find an arXiv ID in the paper's primary/alternate/link URLs."""
    urls: list[str] = []
    primary = data.get("primaryPaperLink")
    if isinstance(primary, dict) and primary.get("url"):
        urls.append(str(primary["url"]))
    for key in ("links", "alternatePaperLinks"):
        for link in data.get(key) or []:
            if isinstance(link, dict) and link.get("url"):
                urls.append(str(link["url"]))

    for url in urls:
        parsed = urlparse(url)
        if "arxiv.org" not in parsed.netloc:
            continue
        for marker in ("/abs/", "/pdf/"):
            if marker not in parsed.path:
                continue
            candidate = unquote(parsed.path.split(marker, 1)[1])
            candidate = candidate.split("?", 1)[0].strip("/")
            if candidate.endswith(".pdf"):
                candidate = candidate[:-4]
            clean = normalize_arxiv_id(candidate)
            if clean:
                return clean
    return None


class S2BrowserClient:
    """Chromium-backed client that reuses a fixed number of tabs."""

    BASE_URL = "https://www.semanticscholar.org"
    REDIRECT_URL = "https://api.semanticscholar.org"

    def __init__(
        self,
        min_interval: Optional[float] = None,
        timeout: int = config.REQUEST_TIMEOUT,
        page_size: int = config.S2_PAGE_SIZE,
        tabs: int = config.S2_TABS,
        headless: bool = config.S2_HEADLESS,
    ):
        if min_interval is None:
            min_interval = config.S2_WEB_MIN_INTERVAL
        self.rate_limiter = AsyncRateLimiter(min_interval)
        self.timeout = timeout
        self.page_size = max(1, min(page_size, config.S2_PAGE_SIZE))
        self.tabs = max(1, int(tabs))
        self.headless = headless
        self.pages: list[Any] = []
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._started = False
        self.request_count = 0
        self._paper_id_cache: dict[str, str] = {}
        self._ref_cache: dict[str, S2Paper] = {}

    async def start(self, tabs: Optional[int] = None) -> "S2BrowserClient":
        """Launch one browser and create ``tabs`` reusable pages."""
        if self._started:
            return self
        count = max(1, int(tabs if tabs is not None else self.tabs))
        try:
            from playwright.async_api import async_playwright
        except ImportError as error:  # pragma: no cover - depends on environment
            raise S2Error(
                "Playwright is not installed. Run: pip install playwright && playwright install chromium"
            ) from error

        try:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=self.headless,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            self._context = await self._browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
                ),
                locale="en-US",
                extra_http_headers={
                    "Accept": "application/json",
                    "Referer": "https://www.semanticscholar.org/",
                },
            )
            self.pages = [await self._context.new_page() for _ in range(count)]
            # One homepage load gives the context the site's normal cookies.
            try:
                response = await self.pages[0].goto(
                    "https://www.semanticscholar.org/",
                    wait_until="domcontentloaded",
                    timeout=self.timeout * 1000,
                )
                if response is not None and response.status != 200:
                    logger.warning("Semantic Scholar homepage returned HTTP %d", response.status)
            except Exception as error:
                logger.warning("Could not warm up Semantic Scholar cookies: %s", error)
        except Exception:
            await self.close()
            raise
        self._started = True
        self.tabs = count
        logger.info("Opened Chromium with %d tab(s) (headless=%s)", count, self.headless)
        return self

    async def close(self) -> None:
        """Close every tab, then the context/browser. Safe to call twice."""
        for page in self.pages:
            try:
                await page.close()
            except Exception:
                pass
        self.pages = []
        for resource, method_name in (
            (self._context, "close"),
            (self._browser, "close"),
            (self._playwright, "stop"),
        ):
            if resource is None:
                continue
            try:
                await getattr(resource, method_name)()
            except Exception:
                pass
        self._context = self._browser = self._playwright = None
        self._started = False

    async def _request(
        self,
        page: Any,
        method: str,
        url: str,
        *,
        params: Optional[dict] = None,
        payload: Optional[dict] = None,
        max_redirects: Optional[int] = None,
    ) -> tuple[int, str, Optional[dict]]:
        """Perform one JSON request, retrying transient failures.

        Returns ``(status, location, payload)``. A 3xx response is returned to
        the caller instead of being followed, which is how arXiv ID resolution
        reads the paper ID from the redirect target.
        """
        last_error: Optional[S2TransientError] = None

        for attempt in range(1, config.MAX_RETRIES + 1):
            await self.rate_limiter.wait()
            self.request_count += 1
            try:
                if method == "GET":
                    kwargs: dict[str, Any] = {"timeout": self.timeout * 1000}
                    if params:
                        kwargs["params"] = params
                    if max_redirects is not None:
                        kwargs["max_redirects"] = max_redirects
                    response = await page.request.get(url, **kwargs)
                else:
                    response = await page.request.post(
                        url,
                        data=json.dumps(payload) if payload is not None else None,
                        headers={"Content-Type": "application/json"},
                        timeout=self.timeout * 1000,
                    )

                status = response.status
                logger.debug("S2 %s %s -> %d", method, url, status)
                headers = response.headers or {}
                if 300 <= status < 400:
                    return status, str(headers.get("location") or ""), None
                if status == 404:
                    raise S2NotFound(f"paper not found: {url}")
                if status in (401, 403):
                    raise S2AuthError(f"HTTP {status} from Semantic Scholar")
                if status == 429:
                    retry_after = _parse_retry_after(headers.get("retry-after"))
                    raise S2RateLimited(
                        f"rate limited (Retry-After={retry_after})", retry_after=retry_after,
                    )
                if status == 202:
                    body = (await response.text())[:120].replace("\n", " ")
                    raise S2WafChallenge(f"AWS WAF challenge: {body or 'empty body'}")
                if 500 <= status < 600:
                    raise S2TransientError(f"HTTP {status} from Semantic Scholar")
                if status >= 400:
                    body = (await response.text())[:200].replace("\n", " ")
                    raise S2PermanentError(f"HTTP {status} from Semantic Scholar: {body}")

                data = await response.json()
                if not isinstance(data, dict):
                    raise S2TransientError("unexpected Semantic Scholar response shape")
                return status, "", data
            except (S2NotFound, S2AuthError, S2PermanentError):
                raise
            except S2TransientError as error:
                last_error = error
            except Exception as error:
                last_error = S2TransientError(f"browser request failed: {error}")

            if attempt == config.MAX_RETRIES:
                assert last_error is not None
                raise last_error

            if last_error.retry_after is not None:
                delay = float(last_error.retry_after)
            else:
                delay = config.BACKOFF_BASE ** attempt
            if isinstance(last_error, S2WafChallenge):
                delay = max(delay, config.S2_WAF_COOLDOWN)
                self.rate_limiter.block(delay)
            elif isinstance(last_error, S2RateLimited):
                delay = max(delay, config.S2_RATE_LIMIT_COOLDOWN)
                self.rate_limiter.block(delay)
            delay = min(max(delay, 0.0), config.MAX_BACKOFF)
            delay += config.RETRY_JITTER
            logger.warning(
                "%s failed (%s); retry %d/%d in %.1fs",
                url, last_error, attempt, config.MAX_RETRIES, delay,
            )
            await asyncio.sleep(delay)

        raise last_error or S2TransientError("request failed")

    async def resolve_paper_id(self, page: Any, arxiv_id: str) -> Optional[str]:
        """Resolve an arXiv ID to an S2 paper ID, or ``None`` if not indexed."""
        clean_id = self._clean_id(arxiv_id)
        if clean_id in self._paper_id_cache:
            return self._paper_id_cache[clean_id]

        url = f"{self.REDIRECT_URL}/arXiv:{quote(clean_id, safe='/')}"
        try:
            status, location, _ = await self._request(
                page, "GET", url, max_redirects=0,
            )
        except S2NotFound:
            return None
        if not (300 <= status < 400) or not location:
            raise S2TransientError(f"unexpected arXiv redirect response for {clean_id}")
        match = _PAPER_PATH_RE.search(location)
        if not match:
            raise S2PermanentError(f"could not find a paper ID in {location!r}")
        paper_id = match.group(1)
        self._paper_id_cache[clean_id] = paper_id
        logger.debug("Resolved arXiv:%s -> %s", clean_id, paper_id)
        return paper_id

    async def _fetch_citation_page(self, page: Any, paper_id: str, offset: int) -> dict:
        status, _, data = await self._request(
            page,
            "GET",
            f"{self.BASE_URL}/api/1/paper/{paper_id}/citations",
            params={
                "citationType": "citedPapers",
                "offset": offset,
                "citationsPageSize": self.page_size,
            },
        )
        if data is None:
            raise S2TransientError("empty citations response")
        return data

    async def _fetch_papers(self, page: Any, paper_ids: list[str]) -> list[S2Paper]:
        if not paper_ids:
            return []
        status, _, data = await self._request(
            page,
            "POST",
            f"{self.BASE_URL}/api/1/paper/batch-v2",
            payload={"ids": paper_ids, "model": "Paper"},
        )
        if data is None:
            raise S2TransientError("empty batch response")
        papers: list[S2Paper] = []
        for item in data.get("papers") or []:
            if isinstance(item, dict):
                paper = self._parse_web_paper(item)
                if paper is not None:
                    papers.append(paper)
        logger.debug("Batch resolved %d/%d paper(s)", len(papers), len(paper_ids))
        return papers

    async def get_paper_by_arxiv(self, page: Any, arxiv_id: str) -> Optional[S2Paper]:
        """Fetch one paper's metadata; return ``None`` when S2 lacks it."""
        paper_id = await self.resolve_paper_id(page, arxiv_id)
        if paper_id is None:
            return None
        cached = self._ref_cache.get(paper_id)
        if cached is not None:
            return cached
        papers = await self._fetch_papers(page, [paper_id])
        if papers:
            self._ref_cache[paper_id] = papers[0]
            return papers[0]
        return None

    async def get_references(self, page: Any, arxiv_id: str) -> list[S2Paper]:
        """Fetch every reference page for one paper using the given tab."""
        paper_id = await self.resolve_paper_id(page, arxiv_id)
        if paper_id is None:
            raise S2NotFound(f"paper not indexed by S2: {arxiv_id}")

        citations: list[dict] = []
        seen_ids: set[str] = set()
        offset = 0

        while True:
            data = await self._fetch_citation_page(page, paper_id, offset)
            items = data.get("citations") or []
            if not isinstance(items, list):
                raise S2TransientError("unexpected citations payload from Semantic Scholar")
            for item in items:
                if not isinstance(item, dict):
                    continue
                item_id = item.get("id")
                if item_id and item_id in seen_ids:
                    continue
                if item_id:
                    seen_ids.add(item_id)
                citations.append(item)

            try:
                total = int(data.get("totalCitations") or 0)
            except (TypeError, ValueError):
                total = 0
            offset += len(items)
            if not items or (total and offset >= total):
                break
            if offset >= config.S2_MAX_OFFSET:
                logger.warning(
                    "Stopping at S2 offset limit (%d) for %s; %d references collected",
                    config.S2_MAX_OFFSET, arxiv_id, len(citations),
                )
                break

        pending_ids = [
            str(item["id"])
            for item in citations
            if item.get("id") and str(item["id"]) not in self._ref_cache
        ]
        pending_ids = list(dict.fromkeys(pending_ids))
        for start in range(0, len(pending_ids), config.S2_BATCH_SIZE):
            batch = pending_ids[start:start + config.S2_BATCH_SIZE]
            for paper in await self._fetch_papers(page, batch):
                self._ref_cache[paper.paper_id] = paper

        refs: list[S2Paper] = []
        for item in citations:
            item_id = str(item.get("id") or "")
            cached = self._ref_cache.get(item_id) if item_id else None
            refs.append(cached if cached is not None else self._parse_citation(item))
        return refs

    def _parse_web_paper(self, data: dict) -> Optional[S2Paper]:
        paper_id = str(data.get("id") or data.get("paperId") or "")
        if not paper_id:
            return None
        publication_date = str(data.get("pubDate") or data.get("publicationDate") or "")
        year = _year(data.get("year"))
        if year is None and len(publication_date) >= 4 and publication_date[:4].isdigit():
            year = int(publication_date[:4])
        authors = _authors(data.get("authors"))
        if not authors:
            authors = _authors(data.get("structuredAuthors"))
        return S2Paper(
            paper_id=paper_id,
            arxiv_id=_extract_arxiv_id(data),
            title=_text(data.get("title")),
            authors=authors,
            year=year,
            publication_date=publication_date or None,
        )

    def _parse_citation(self, data: dict) -> S2Paper:
        paper_id = str(data.get("id") or "")
        return S2Paper(
            paper_id=paper_id,
            arxiv_id=None,
            title=_text(data.get("title")),
            authors=_authors(data.get("authors")),
            year=_year(data.get("year")),
            publication_date=None,
        )

    @staticmethod
    def _clean_id(arxiv_id: str) -> str:
        clean_id = normalize_arxiv_id(arxiv_id)
        if not clean_id:
            raise ValueError("empty arXiv ID")
        return clean_id


S2Client = S2BrowserClient
