"""Tests for the resumable crawler and the Playwright Semantic Scholar client."""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace

import pytest

from src import config
from src.crawler import (
    CrawlPaths,
    build_reference_record,
    crawl_references,
    load_reference_pairs,
    paths_for,
)
from src.s2_api_client import S2ApiAuthError, S2ApiRateLimited
from src.s2_client import (
    S2AuthError,
    S2BrowserClient,
    S2NotFound,
    S2Paper,
    S2RateLimited,
)
from src.utils import (
    append_jsonl,
    canonical_arxiv_id,
    load_latest_records,
    normalize_arxiv_id,
    read_jsonl,
)

MEMBER = "member1"
FIRST_ID = "2501.07642"
SECOND_ID = "2501.07643"
THIRD_ID = "2501.07644"


def make_paths(tmp_path):
    return CrawlPaths(
        references=tmp_path / "references.jsonl",
        ledger=tmp_path / "ledger.jsonl",
        log=tmp_path / "crawl.log",
        stats_json=tmp_path / "stats.json",
        stats_markdown=tmp_path / "stats.md",
    )


def make_paper(paper_id="p1", arxiv_id="2401.00001", title="Paper", date="2024-01-01"):
    return S2Paper(
        paper_id=paper_id,
        arxiv_id=arxiv_id,
        title=title,
        authors=["A"],
        year=2024,
        publication_date=date,
    )


class FakePage:
    def __init__(self, page_id=0):
        self.id = page_id


class FakeClient:
    """Async stand-in for S2BrowserClient, recording tab usage."""

    def __init__(self, responses, delay=0.0):
        self.responses = responses
        self.delay = delay
        self.calls = []
        self.page_calls = []
        self.pages = []
        self.started = False
        self.started_tabs = None
        self.closed = False
        self.closed_at_active = None
        self.active = 0
        self.max_active = 0
        self.rate_limiter = SimpleNamespace(min_interval=0)

    async def start(self, tabs):
        self.started = True
        self.started_tabs = tabs
        self.pages = [FakePage(i) for i in range(tabs)]
        return self

    async def get_references(self, page, arxiv_id):
        self.calls.append(arxiv_id)
        self.page_calls.append((page.id, arxiv_id))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            value = self.responses.get(arxiv_id, [])
            if isinstance(value, Exception):
                raise value
            return value
        finally:
            self.active -= 1

    async def close(self):
        self.closed = True
        self.closed_at_active = self.active
        self.pages = []


class FakeApiClient(FakeClient):
    """Async stand-in for S2ApiClient with the API counters the crawler reads."""

    def __init__(self, responses, delay=0.0):
        super().__init__(responses, delay=delay)
        self.api_calls = 0
        self.rate_limit_hits = 0
        self.auth_errors = 0

    async def get_references(self, page, arxiv_id):
        self.api_calls += 1
        self.calls.append(arxiv_id)
        self.page_calls.append((getattr(page, "id", None), arxiv_id))
        value = self.responses.get(arxiv_id, [])
        if isinstance(value, S2ApiRateLimited):
            self.rate_limit_hits += 1
        if isinstance(value, S2ApiAuthError):
            self.auth_errors += 1
        if isinstance(value, Exception):
            raise value
        return value


class FakeResponse:
    def __init__(self, status=200, payload=None, headers=None, text=""):
        self.status = status
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self._text = text

    async def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    async def text(self):
        return self._text


class FakeAPIRequest:
    """Mimic Playwright's APIRequestContext for one tab."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def get(self, url, **kwargs):
        self.calls.append({"method": "GET", "url": url, **kwargs})
        return self._next()

    async def post(self, url, **kwargs):
        self.calls.append({"method": "POST", "url": url, **kwargs})
        return self._next()

    def _next(self):
        if not self.responses:
            raise AssertionError("unexpected Semantic Scholar request")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeRequestPage:
    def __init__(self, responses, page_id=0):
        self.id = page_id
        self.request = FakeAPIRequest(responses)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def quiet_log():
    return logging.getLogger("tests.crawler")


def read_records(path):
    return list(read_jsonl(path))


def redirect_response(paper_id="a" * 40):
    return FakeResponse(
        status=301,
        headers={"location": f"https://www.semanticscholar.org/paper/{paper_id}"},
    )


def batch_response(*papers):
    return FakeResponse(payload={"papers": list(papers), "failedPaperIds": []})


def citation(paper_id, title="Reference", year=2020):
    return {
        "id": paper_id,
        "title": {"text": title},
        "year": year,
        "authors": [[{"name": "Author"}], [{"text": "Author"}]],
    }


class TestS2Client:
    def test_parse_web_paper_extracts_metadata_and_arxiv(self):
        client = S2BrowserClient(min_interval=0)
        paper = client._parse_web_paper(
            {
                "id": "p1",
                "title": {"text": "Test Paper"},
                "authors": [[{"name": "Author One"}], [{"name": "Author Two"}]],
                "year": {"text": "2025"},
                "pubDate": "2025-01-15",
                "primaryPaperLink": {"url": "https://doi.org/10.1/example"},
                "alternatePaperLinks": [
                    {"url": "https://arxiv.org/pdf/2501.12345v2.pdf", "linkType": "arxiv"},
                ],
            }
        )
        assert paper == S2Paper(
            "p1", "2501.12345", "Test Paper", ["Author One", "Author Two"], 2025, "2025-01-15",
        )

    def test_parse_web_paper_missing_paper_id(self):
        client = S2BrowserClient(min_interval=0)
        assert client._parse_web_paper({"title": {"text": "No ID"}}) is None

    def test_parse_web_paper_tolerates_missing_optional_fields(self):
        client = S2BrowserClient(min_interval=0)
        paper = client._parse_web_paper({"id": "abc", "year": None, "authors": None})
        assert paper is not None
        assert paper.arxiv_id is None
        assert paper.authors == []
        assert paper.title == ""
        assert paper.year is None

    def test_extract_arxiv_id_variants(self):
        client = S2BrowserClient(min_interval=0)
        variants = [
            "https://arxiv.org/abs/2401.00001",
            "https://arxiv.org/pdf/2401.00001v2.pdf",
            "https://export.arxiv.org/pdf/hep-th/9901001",
        ]
        for url in variants:
            paper = client._parse_web_paper({"id": "p", "links": [{"url": url}]})
            assert paper is not None
        assert client._parse_web_paper(
            {"id": "p", "links": [{"url": "https://arxiv.org/abs/2401.00001"}]}
        ).arxiv_id == "2401.00001"
        assert client._parse_web_paper(
            {"id": "p", "links": [{"url": "https://export.arxiv.org/pdf/hep-th/9901001"}]}
        ).arxiv_id == "hep-th/9901001"

    def test_resolve_paper_id_reads_redirect_location(self):
        client = S2BrowserClient(min_interval=0)
        expected = "b" * 40
        page = FakeRequestPage([redirect_response(expected)])
        assert run(client.resolve_paper_id(page, FIRST_ID)) == expected
        call = page.request.calls[0]
        assert call["method"] == "GET"
        assert call["max_redirects"] == 0
        assert "arXiv:2501.07642" in call["url"]

    def test_resolve_paper_id_returns_none_for_404(self):
        client = S2BrowserClient(min_interval=0)
        page = FakeRequestPage([FakeResponse(status=404, text="not found")])
        assert run(client.resolve_paper_id(page, FIRST_ID)) is None

    def test_get_references_resolves_citations_and_batches(self):
        client = S2BrowserClient(min_interval=0, page_size=10)
        page = FakeRequestPage([
            redirect_response(),
            FakeResponse(payload={
                "citations": [citation("p1"), citation("p2", title="Second")],
                "totalCitations": 2,
            }),
            batch_response(
                {
                    "id": "p1",
                    "title": {"text": "Reference"},
                    "year": {"text": "2020"},
                    "pubDate": "2020-01-02",
                    "authors": [[{"name": "Author"}]],
                    "links": [{"url": "https://arxiv.org/pdf/2001.00001.pdf"}],
                },
                {
                    "id": "p2",
                    "title": {"text": "Second"},
                    "year": {"text": "2021"},
                    "pubDate": "2021-02-03",
                    "authors": [[{"name": "Other"}]],
                },
            ),
        ])
        refs = run(client.get_references(page, FIRST_ID))
        assert [paper.paper_id for paper in refs] == ["p1", "p2"]
        assert refs[0].arxiv_id == "2001.00001"
        assert refs[0].publication_date == "2020-01-02"
        assert refs[1].arxiv_id is None
        batch_call = page.request.calls[-1]
        assert batch_call["method"] == "POST"
        assert json.loads(batch_call["data"])["ids"] == ["p1", "p2"]

    def test_references_paginate_until_total(self):
        client = S2BrowserClient(min_interval=0, page_size=2)
        page = FakeRequestPage([
            redirect_response(),
            FakeResponse(payload={"citations": [citation("p1"), citation("p2")], "totalCitations": 3}),
            FakeResponse(payload={"citations": [citation("p3")], "totalCitations": 3}),
            batch_response(
                {"id": "p1", "title": {"text": "1"}},
                {"id": "p2", "title": {"text": "2"}},
                {"id": "p3", "title": {"text": "3"}},
            ),
        ])
        refs = run(client.get_references(page, FIRST_ID))
        assert [paper.paper_id for paper in refs] == ["p1", "p2", "p3"]
        offsets = [
            call["params"]["offset"]
            for call in page.request.calls
            if call["method"] == "GET" and call["url"].endswith("/citations")
        ]
        assert offsets == [0, 2]

    def test_references_retries_429_once(self, monkeypatch):
        monkeypatch.setattr(config, "RETRY_JITTER", 0.0)
        monkeypatch.setattr(config, "S2_RATE_LIMIT_COOLDOWN", 0.0)
        client = S2BrowserClient(min_interval=0, page_size=10)
        page = FakeRequestPage([
            redirect_response(),
            FakeResponse(status=429, headers={"Retry-After": "0"}),
            FakeResponse(payload={"citations": [citation("p1")], "totalCitations": 1}),
            batch_response({"id": "p1", "title": {"text": "Reference"}}),
        ])
        refs = run(client.get_references(page, FIRST_ID))
        assert [paper.paper_id for paper in refs] == ["p1"]
        assert len(page.request.calls) == 4

    def test_403_is_auth_error_and_not_retried(self):
        client = S2BrowserClient(min_interval=0)
        page = FakeRequestPage([redirect_response(), FakeResponse(status=403, text="forbidden")])
        with pytest.raises(S2AuthError):
            run(client.get_references(page, FIRST_ID))
        assert len(page.request.calls) == 2

    def test_404_is_not_retried(self):
        client = S2BrowserClient(min_interval=0)
        page = FakeRequestPage([redirect_response(), FakeResponse(status=404, text="not found")])
        with pytest.raises(S2NotFound):
            run(client.get_references(page, FIRST_ID))
        assert len(page.request.calls) == 2

    def test_get_paper_returns_none_for_404(self):
        client = S2BrowserClient(min_interval=0)
        page = FakeRequestPage([FakeResponse(status=404, text="not found")])
        assert run(client.get_paper_by_arxiv(page, FIRST_ID)) is None

    def test_navigation_failure_is_retried_and_wrapped(self, monkeypatch):
        monkeypatch.setattr(config, "RETRY_JITTER", 0.0)
        monkeypatch.setattr(config, "MAX_RETRIES", 2)
        client = S2BrowserClient(min_interval=0)
        page = FakeRequestPage([RuntimeError("net down"), RuntimeError("net down")])
        with pytest.raises(Exception, match="browser request failed"):
            run(client.get_references(page, FIRST_ID))
        assert len(page.request.calls) == 2


class TestReferenceRecord:
    def test_normalizes_and_cleans_ids(self):
        record = build_reference_record(
            FIRST_ID,
            make_paper(arxiv_id="arXiv:2401.00001v3"),
            MEMBER,
            "2026-01-01T00:00:00+00:00",
        )
        assert record["arxiv_id"] == "2501_07642"
        assert record["ref_arxiv_id"] == "2401_00001"
        assert record["ref_title"] == "Paper"
        assert record["ref_authors"] == ["A"]
        assert record["ref_submission_date"] == "2024-01-01"
        assert record["ref_s2_id"] == "p1"
        assert set(record.keys()) == {
            "arxiv_id",
            "ref_arxiv_id",
            "ref_title",
            "ref_authors",
            "ref_submission_date",
            "ref_s2_id",
        }

    def test_drops_references_without_arxiv_id(self):
        assert build_reference_record(FIRST_ID, make_paper(arxiv_id=None), MEMBER, "now") is None


class TestCrawl:
    def test_writes_edges_and_resumes_without_duplicates(self, tmp_path, quiet_log):
        paths = make_paths(tmp_path)
        refs = [
            make_paper("r1", "2401.00001"),
            make_paper("r2", None),
            make_paper("r3", "2401.00003v2"),
        ]
        client = FakeClient({FIRST_ID: refs})

        result = crawl_references(MEMBER, paths=paths, client=client, limit=1, log=quiet_log)
        assert not result.fatal
        assert result.stats.processed == 1
        assert result.stats.refs_found == 3
        assert result.stats.refs_with_arxiv == 2
        assert result.stats.refs_written == 2
        assert client.closed is True

        records = read_records(paths.references)
        assert len(records) == 2
        assert {record["ref_arxiv_id"] for record in records} == {"2401_00001", "2401_00003"}
        assert load_latest_records(paths.ledger)[canonical_arxiv_id(FIRST_ID)]["status"] == "ok"

        # A second run must skip the completed paper, even if it had refs.
        client.calls.clear()
        crawl_references(MEMBER, paths=paths, client=client, limit=1, log=quiet_log)
        assert FIRST_ID not in client.calls
        assert load_latest_records(paths.ledger)[canonical_arxiv_id(FIRST_ID)]["status"] == "ok"

    def test_zero_reference_paper_is_marked_done(self, tmp_path, quiet_log):
        paths = make_paths(tmp_path)
        client = FakeClient({FIRST_ID: []})

        crawl_references(MEMBER, paths=paths, client=client, limit=1, log=quiet_log)
        assert load_latest_records(paths.ledger)[canonical_arxiv_id(FIRST_ID)]["status"] == "ok"
        assert client.calls == [FIRST_ID]

        client.calls.clear()
        result = crawl_references(MEMBER, paths=paths, client=client, limit=1, log=quiet_log)
        assert client.calls == [SECOND_ID]
        assert result.stats.skipped == 1

    def test_partial_output_is_deduplicated_on_resume(self, tmp_path, quiet_log):
        paths = make_paths(tmp_path)
        append_jsonl(
            paths.references,
            {"arxiv_id": canonical_arxiv_id(FIRST_ID), "ref_arxiv_id": "2401_00001"},
        )
        client = FakeClient({
            FIRST_ID: [make_paper("r1", "2401.00001"), make_paper("r2", "2401.00002")],
        })

        result = crawl_references(MEMBER, paths=paths, client=client, limit=1, log=quiet_log)
        assert result.stats.refs_written == 1
        assert load_reference_pairs(paths.references) == {
            (canonical_arxiv_id(FIRST_ID), "2401_00001"),
            (canonical_arxiv_id(FIRST_ID), "2401_00002"),
        }

    def test_errors_are_retried_until_max_attempts(self, tmp_path, quiet_log, monkeypatch):
        monkeypatch.setattr(config, "MAX_PAPER_ATTEMPTS", 2)
        paths = make_paths(tmp_path)
        client = FakeClient({FIRST_ID: S2RateLimited("slow down", retry_after=0)})

        crawl_references(MEMBER, paths=paths, client=client, limit=1, log=quiet_log)
        ledger = load_latest_records(paths.ledger)[canonical_arxiv_id(FIRST_ID)]
        assert ledger["status"] == "error"
        assert ledger["attempts"] == 1

        client.calls.clear()
        crawl_references(MEMBER, paths=paths, client=client, limit=1, log=quiet_log)
        assert client.calls == [FIRST_ID]
        ledger = load_latest_records(paths.ledger)[canonical_arxiv_id(FIRST_ID)]
        assert ledger["attempts"] == 2

        client.calls.clear()
        result = crawl_references(MEMBER, paths=paths, client=client, limit=1, log=quiet_log)
        assert FIRST_ID not in client.calls
        assert result.stats.skipped == 1

    def test_not_found_is_terminal_unless_requested(self, tmp_path, quiet_log):
        paths = make_paths(tmp_path)
        client = FakeClient({FIRST_ID: S2NotFound("missing")})

        result = crawl_references(MEMBER, paths=paths, client=client, limit=1, log=quiet_log)
        assert result.stats.not_found == 1
        assert result.stats.errors == 0

        client.calls.clear()
        crawl_references(MEMBER, paths=paths, client=client, limit=1, log=quiet_log)
        assert FIRST_ID not in client.calls

        client.calls.clear()
        crawl_references(
            MEMBER, paths=paths, client=client, limit=1, retry_not_found=True, log=quiet_log,
        )
        assert client.calls == [FIRST_ID]

    def test_browser_start_failure_closes_partial_client(self, tmp_path, quiet_log):
        class BrokenStartClient(FakeClient):
            async def start(self, tabs):
                raise RuntimeError("no chromium")

        paths = make_paths(tmp_path)
        client = BrokenStartClient({FIRST_ID: []})
        with pytest.raises(RuntimeError, match="no chromium"):
            crawl_references(MEMBER, paths=paths, client=client, limit=1, log=quiet_log)
        assert client.closed is True

    def test_auth_error_is_fatal_and_closes_tabs(self, tmp_path, quiet_log):
        paths = make_paths(tmp_path)
        client = FakeClient({FIRST_ID: S2AuthError("bad key")})
        result = crawl_references(MEMBER, paths=paths, client=client, limit=1, log=quiet_log)
        assert result.fatal is True
        assert client.closed is True
        assert client.closed_at_active == 0

    def test_three_tabs_run_concurrently_and_close_after_work(self, tmp_path, quiet_log):
        paths = make_paths(tmp_path)
        client = FakeClient(
            {
                FIRST_ID: [make_paper("r1", "2401.00001")],
                SECOND_ID: [make_paper("r2", "2401.00002")],
                THIRD_ID: [make_paper("r3", "2401.00003")],
            },
            delay=0.05,
        )

        result = crawl_references(
            MEMBER, paths=paths, client=client, limit=3, tabs=3, log=quiet_log,
        )
        assert result.stats.processed == 3
        assert result.stats.refs_written == 3
        assert client.started_tabs == 3
        assert client.max_active == 3
        assert {page_id for page_id, _ in client.page_calls} == {0, 1, 2}
        assert client.closed is True
        assert client.closed_at_active == 0
        assert len(read_records(paths.references)) == 3

    def test_tabs_are_capped_by_pending_work(self, tmp_path, quiet_log):
        paths = make_paths(tmp_path)
        client = FakeClient({FIRST_ID: []})
        crawl_references(MEMBER, paths=paths, client=client, limit=1, tabs=3, log=quiet_log)
        assert client.started_tabs == 1
        assert len(client.pages) == 0  # closed after the run
        assert client.max_active == 1
        assert client.closed_at_active == 0

    def test_migrates_legacy_shared_references_file(self, tmp_path, quiet_log):
        member_dir = tmp_path / "raw" / MEMBER
        paths = CrawlPaths(
            references=member_dir / "references.jsonl",
            ledger=member_dir / "ledger.jsonl",
            log=tmp_path / "crawl.log",
            stats_json=tmp_path / "stats.json",
            stats_markdown=tmp_path / "stats.md",
        )
        legacy = tmp_path / "raw" / "references.jsonl"
        append_jsonl(legacy, {
            "arxiv_id": FIRST_ID,
            "ref_arxiv_id": "2401.00001",
            "ref_title": "Old",
            "ref_authors": ["A"],
            "ref_submission_date": "2024-01-01",
            "ref_s2_id": "s1",
        })

        client = FakeClient({})
        result = crawl_references(MEMBER, paths=paths, client=client, limit=1, log=quiet_log)
        assert FIRST_ID not in client.calls
        assert result.stats.skipped == 1
        migrated = read_records(paths.references)
        assert len(migrated) == 1
        assert migrated[0]["ref_submission_date"] == "2024-01-01"
        assert migrated[0]["ref_title"] == "Old"
        assert "member" not in migrated[0]
        assert "schema_version" not in migrated[0]
        assert "crawled_at" not in migrated[0]
        assert load_latest_records(paths.ledger)[canonical_arxiv_id(FIRST_ID)]["status"] == "ok"

    def test_dry_run_makes_no_calls_and_writes_no_data(self, tmp_path, quiet_log):
        paths = make_paths(tmp_path)
        fake = FakeClient({})
        result = crawl_references(MEMBER, paths=paths, client=fake, dry_run=True, log=quiet_log)
        assert result.dry_run is True
        assert fake.calls == []
        assert fake.started is False
        assert fake.closed is False
        assert not paths.references.exists()
        assert not paths.ledger.exists()

    def test_paths_are_isolated_per_member(self, tmp_path):
        first = paths_for("member1", tmp_path)
        second = paths_for("member2", tmp_path)
        assert first.references != second.references
        assert first.ledger != second.ledger
        assert first.log != second.log
        with pytest.raises(ValueError):
            paths_for("../escape", tmp_path)

    def test_normalize_arxiv_id(self):
        assert normalize_arxiv_id("arXiv:2401.00001v3") == "2401.00001"
        assert normalize_arxiv_id("hep-th/9901001v2") == "hep-th/9901001"
        assert normalize_arxiv_id(None) == ""


class TestApiMigration:
    def test_api_client_writes_edges_and_metrics(self, tmp_path, quiet_log):
        paths = make_paths(tmp_path)
        api = FakeApiClient({FIRST_ID: [make_paper("r1", "2401.00001")]})

        result = crawl_references(
            MEMBER, paths=paths, api_client=api, client_type="api",
            limit=1, tabs=1, log=quiet_log,
        )
        assert not result.fatal
        assert result.stats.client_type == "api"
        assert result.stats.api_calls == 1
        assert result.stats.browser_calls == 0
        assert result.stats.refs_written == 1
        assert len(read_records(paths.references)) == 1
        assert api.closed is True

    def test_api_auth_error_falls_back_to_browser(self, tmp_path, quiet_log):
        paths = make_paths(tmp_path)
        api = FakeApiClient({FIRST_ID: S2ApiAuthError("bad key")})
        browser = FakeClient({FIRST_ID: [make_paper("r1", "2401.00001")]})

        result = crawl_references(
            MEMBER, paths=paths, api_client=api, browser_client=browser,
            client_type="api", limit=1, tabs=1, log=quiet_log,
        )
        assert result.fatal is False
        assert result.stats.client_type == "api->browser"
        assert result.stats.refs_written == 1
        assert browser.calls == [FIRST_ID]
        assert browser.started_tabs == 1
        assert api.closed is True
        assert browser.closed is True
        assert load_latest_records(paths.ledger)[canonical_arxiv_id(FIRST_ID)]["status"] == "ok"

    def test_fallback_uses_browser_pages_for_later_papers(self, tmp_path, quiet_log):
        paths = make_paths(tmp_path)
        api = FakeApiClient({FIRST_ID: S2ApiAuthError("bad key"), SECOND_ID: []})
        browser = FakeClient({
            FIRST_ID: [make_paper("r1", "2401.00001")],
            SECOND_ID: [make_paper("r2", "2401.00002")],
        })

        result = crawl_references(
            MEMBER, paths=paths, api_client=api, browser_client=browser,
            client_type="api", limit=2, tabs=1, log=quiet_log,
        )
        assert result.fatal is False
        assert result.stats.processed == 2
        assert result.stats.refs_written == 2
        assert api.calls == [FIRST_ID]
        assert browser.calls == [FIRST_ID, SECOND_ID]
        assert all(page_id is not None for page_id, _ in browser.page_calls)

    def test_api_rate_limit_falls_back_to_browser(self, tmp_path, quiet_log):
        paths = make_paths(tmp_path)
        api = FakeApiClient({FIRST_ID: S2ApiRateLimited("slow down", retry_after=0)})
        browser = FakeClient({FIRST_ID: [make_paper("r1", "2401.00001")]})

        result = crawl_references(
            MEMBER, paths=paths, api_client=api, browser_client=browser,
            client_type="api", limit=1, tabs=1, log=quiet_log,
        )
        assert result.fatal is False
        assert result.stats.client_type == "api->browser"
        assert result.stats.rate_limited == 1
        assert browser.calls == [FIRST_ID]
        assert result.stats.refs_written == 1

    def test_both_clients_compare_and_return_api_edges(self, tmp_path, quiet_log, caplog):
        paths = make_paths(tmp_path)
        api = FakeApiClient({FIRST_ID: [make_paper("r1", "2401.00001")]})
        browser = FakeClient({
            FIRST_ID: [make_paper("r1", "2401.00001"), make_paper("r2", "2401.00002")],
        })

        with caplog.at_level(logging.WARNING, logger="src.crawler"):
            result = crawl_references(
                MEMBER, paths=paths, api_client=api, browser_client=browser,
                client_type="both", limit=1, tabs=1, log=quiet_log,
            )

        assert result.fatal is False
        assert result.stats.client_type == "both"
        assert result.stats.api_calls == 1
        assert result.stats.refs_written == 1
        assert browser.calls == [FIRST_ID]
        assert any("Client mismatch" in record.message for record in caplog.records)
        assert api.closed is True
        assert browser.closed is True

    def test_invalid_client_type_is_rejected(self, tmp_path, quiet_log):
        paths = make_paths(tmp_path)
        with pytest.raises(ValueError, match="client_type"):
            crawl_references(
                MEMBER, paths=paths, client_type="scraper", log=quiet_log,
            )
