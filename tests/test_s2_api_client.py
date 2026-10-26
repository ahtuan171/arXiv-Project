"""Tests for the official Semantic Scholar Graph API client."""

from __future__ import annotations

import time

import httpx
import pytest

from src import config
from src.crawl_references_module.s2_api_client import (
    S2ApiAuthError,
    S2ApiClient,
    S2ApiNotFound,
    S2ApiRateLimited,
)


def make_client(handler, **kwargs):
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(base_url=config.S2_API_BASE, transport=transport)
    client = S2ApiClient(min_interval=0, http_client=http, **kwargs)
    return client, http


def run(coro):
    import asyncio

    return asyncio.run(coro)


def paper_payload(paper_id, arxiv_id=None, title="Paper", year=2024):
    payload = {
        "paperId": paper_id,
        "title": title,
        "authors": [{"name": "Author One"}, {"name": "Author Two"}],
        "year": year,
        "publicationDate": f"{year}-01-02",
    }
    if arxiv_id:
        payload["externalIds"] = {"ArXiv": arxiv_id, "DOI": "10.1/example"}
    return payload


class TestResolve:
    def test_resolve_paper_id_and_cache(self):
        calls = []

        def handler(request):
            calls.append(request)
            assert request.url.path.endswith("/paper/arXiv:2501.07642")
            return httpx.Response(200, json={"paperId": "abc123"})

        client, http = make_client(handler)

        async def scenario():
            await client.start(1)
            first = await client.resolve_paper_id("arXiv:2501.07642v2")
            second = await client.resolve_paper_id("2501.07642")
            await http.aclose()
            return first, second

        assert run(scenario()) == ("abc123", "abc123")
        assert len(calls) == 1
        assert calls[0].url.params["fields"] == "paperId"

    def test_resolve_404_returns_none(self):
        def handler(request):
            return httpx.Response(404, json={"error": "not found"})

        client, http = make_client(handler)

        async def scenario():
            await client.start(1)
            value = await client.resolve_paper_id("2501.07642")
            await http.aclose()
            return value

        assert run(scenario()) is None


class TestReferences:
    def test_paginates_and_batches_metadata(self):
        requests = []

        def handler(request):
            requests.append(request)
            if request.method == "GET":
                offset = int(request.url.params["offset"])
                if offset == 0:
                    return httpx.Response(200, json={
                        "data": [
                            {"citedPaper": {"paperId": "p1"}},
                            {"citedPaper": {"paperId": "p2"}},
                        ],
                        "next": 2,
                    })
                return httpx.Response(200, json={
                    "data": [{"citedPaper": {"paperId": "p3"}}],
                    "next": None,
                })
            return httpx.Response(200, json=[
                paper_payload("p1", "2401.00001"),
                paper_payload("p2", None, title="No arXiv"),
                paper_payload("p3", "2402.00003v2"),
            ])

        client, http = make_client(handler, page_size=2)

        async def scenario():
            await client.start(1)
            refs = await client.get_references(None, "2501.07642")
            await http.aclose()
            return refs

        refs = run(scenario())
        assert [ref.paper_id for ref in refs] == ["p1", "p2", "p3"]
        assert [ref.arxiv_id for ref in refs] == ["2401.00001", None, "2402.00003"]
        assert refs[0].title == "Paper"
        assert refs[0].authors == ["Author One", "Author Two"]
        assert refs[0].publication_date == "2024-01-02"
        assert refs[0].year == 2024

        offset_calls = [
            request.url.params["offset"]
            for request in requests
            if request.method == "GET"
        ]
        assert offset_calls == ["0", "2"]
        assert requests[0].url.params["fields"] == config.S2_API_REF_FIELDS
        batch = [request for request in requests if request.method == "POST"]
        assert len(batch) == 1
        assert batch[0].url.path.endswith("/paper/batch")
        assert "externalIds" in batch[0].url.params["fields"]

    def test_direct_metadata_extraction_avoids_batch_lookup(self):
        requests = []

        def handler(request):
            requests.append(request)
            if request.method == "GET":
                return httpx.Response(200, json={
                    "data": [
                        {
                            "citedPaper": {
                                "paperId": "p1",
                                "externalIds": {"ArXiv": "2401.00001"},
                                "title": "Paper 1",
                                "authors": [{"name": "Author A"}],
                                "year": 2024,
                                "publicationDate": "2024-01-01",
                            }
                        },
                        {
                            "citedPaper": {
                                "paperId": "p2",
                                "externalIds": {"DOI": "10.1234/example"},
                                "title": "Paper 2",
                                "authors": [{"name": "Author B"}],
                                "year": 2023,
                                "publicationDate": "2023-05-01",
                            }
                        },
                    ],
                    "next": None,
                })
            return httpx.Response(500, text="Should not be called")

        client, http = make_client(handler)

        async def scenario():
            await client.start(1)
            refs = await client.get_references(None, "2501.07642")
            await http.aclose()
            return refs

        refs = run(scenario())
        assert len(refs) == 2
        assert refs[0].paper_id == "p1"
        assert refs[0].arxiv_id == "2401.00001"
        assert refs[0].title == "Paper 1"
        assert refs[1].paper_id == "p2"
        assert refs[1].arxiv_id is None
        assert refs[1].title == "Paper 2"

        # Verify ZERO POST requests were made!
        post_requests = [r for r in requests if r.method == "POST"]
        assert len(post_requests) == 0

    def test_empty_response(self):
        def handler(request):
            return httpx.Response(200, json={"data": [], "next": None})

        client, http = make_client(handler)

        async def scenario():
            await client.start(1)
            refs = await client.get_references(None, "2501.07642")
            await http.aclose()
            return refs

        refs = run(scenario())
        assert refs == []

    def test_missing_or_malformed_nested_fields(self):
        def handler(request):
            if request.method == "GET":
                return httpx.Response(200, json={
                    "data": [
                        "not-a-dict",
                        {"noCitedPaper": 123},
                        {"citedPaper": None},
                        {"citedPaper": "not-a-dict"},
                        {"citedPaper": {}},
                        {"citedPaper": {"paperId": ""}},
                        {"citedPaper": {"paperId": "   "}},
                        {"citedPaper": {"paperId": "valid_p1"}},
                    ],
                    "next": None,
                })
            return httpx.Response(200, json=[paper_payload("valid_p1", "2401.00001")])

        client, http = make_client(handler)

        async def scenario():
            await client.start(1)
            refs = await client.get_references(None, "2501.07642")
            await http.aclose()
            return refs

        refs = run(scenario())
        assert len(refs) == 1
        assert refs[0].paper_id == "valid_p1"
        assert refs[0].arxiv_id == "2401.00001"

    def test_duplicate_references_are_deduplicated(self):
        def handler(request):
            if request.method == "GET":
                return httpx.Response(200, json={
                    "data": [
                        {"citedPaper": {"paperId": "dup1"}},
                        {"citedPaper": {"paperId": "dup1"}},
                        {"citedPaper": {"paperId": "dup2"}},
                        {"citedPaper": {"paperId": "dup1"}},
                    ],
                    "next": None,
                })
            return httpx.Response(200, json=[
                paper_payload("dup1", "2401.00001"),
                paper_payload("dup2", "2401.00002"),
            ])

        client, http = make_client(handler)

        async def scenario():
            await client.start(1)
            refs = await client.get_references(None, "2501.07642")
            await http.aclose()
            return refs

        refs = run(scenario())
        assert [r.paper_id for r in refs] == ["dup1", "dup2"]

    def test_pagination_advances_and_terminates(self):
        requests = []

        def handler(request):
            requests.append(request)
            if request.method == "GET":
                offset = int(request.url.params["offset"])
                if offset == 0:
                    return httpx.Response(200, json={
                        "data": [{"citedPaper": {"paperId": "p1"}}],
                        "next": 1,
                    })
                if offset == 1:
                    return httpx.Response(200, json={
                        "data": [{"citedPaper": {"paperId": "p2"}}],
                        "next": 2,
                    })
                return httpx.Response(200, json={
                    "data": [],
                    "next": None,
                })
            return httpx.Response(200, json=[
                paper_payload("p1", "2401.00001"),
                paper_payload("p2", "2401.00002"),
            ])

        client, http = make_client(handler, page_size=1)

        async def scenario():
            await client.start(1)
            refs = await client.get_references(None, "2501.07642")
            await http.aclose()
            return refs

        refs = run(scenario())
        assert [r.paper_id for r in refs] == ["p1", "p2"]
        offsets = [r.url.params["offset"] for r in requests if r.method == "GET"]
        assert offsets == ["0", "1", "2"]

    def test_missing_batch_metadata_is_a_placeholder(self):
        def handler(request):
            if request.method == "GET":
                return httpx.Response(200, json={
                    "data": [
                        {"citedPaper": {"paperId": "p1"}},
                        {"citedPaper": {"paperId": "p2"}},
                    ],
                    "next": None,
                })
            return httpx.Response(200, json=[paper_payload("p1", "2401.00001"), None])

        client, http = make_client(handler)

        async def scenario():
            await client.start(1)
            refs = await client.get_references(None, "2501.07642")
            await http.aclose()
            return refs

        refs = run(scenario())
        assert refs[0].arxiv_id == "2401.00001"
        assert refs[1].paper_id == "p2"
        assert refs[1].arxiv_id is None
        assert refs[1].title == ""

    def test_404_is_not_retried(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(404, json={"error": "not found"})

        client, http = make_client(handler)

        async def scenario():
            await client.start(1)
            with pytest.raises(S2ApiNotFound):
                await client.get_references(None, "2501.07642")
            await http.aclose()

        run(scenario())
        assert len(calls) == 1

    def test_regression_old_flat_paper_id_vs_nested_cited_paper(self):
        """Old parser looked for item['paperId'], failing when response has item['citedPaper']['paperId']."""
        def handler(request):
            if request.method == "GET":
                return httpx.Response(200, json={
                    "data": [
                        {"citedPaper": {"paperId": "correct_id"}},
                    ],
                    "next": None,
                })
            return httpx.Response(200, json=[paper_payload("correct_id", "2401.00001")])

        client, http = make_client(handler)

        async def scenario():
            await client.start(1)
            refs = await client.get_references(None, "2501.07642")
            await http.aclose()
            return refs

        refs = run(scenario())
        assert len(refs) == 1
        assert refs[0].paper_id == "correct_id"


class TestErrors:
    def test_429_retries_and_honours_retry_after(self, monkeypatch):
        monkeypatch.setattr(config, "RETRY_JITTER", 0.0)
        calls = []

        def handler(request):
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(429, headers={"Retry-After": "0"})
            return httpx.Response(200, json={"paperId": "abc"})

        client, http = make_client(handler)

        async def scenario():
            await client.start(1)
            paper_id = await client.resolve_paper_id("2501.07642")
            await http.aclose()
            return paper_id

        assert run(scenario()) == "abc"
        assert len(calls) == 2
        assert client.rate_limit_hits == 1

    def test_429_exhausts_retries_with_rate_limit_error(self, monkeypatch):
        monkeypatch.setattr(config, "RETRY_JITTER", 0.0)
        monkeypatch.setattr(config, "BACKOFF_BASE", 0.0)

        def handler(request):
            return httpx.Response(429, headers={"Retry-After": "0"})

        client, http = make_client(handler, max_retries=2)

        async def scenario():
            await client.start(1)
            with pytest.raises(S2ApiRateLimited):
                await client.resolve_paper_id("2501.07642")
            await http.aclose()

        run(scenario())
        assert client.api_calls == 2

    def test_401_403_is_fatal_and_not_retried(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(403, json={"error": "forbidden"})

        client, http = make_client(handler)

        async def scenario():
            await client.start(1)
            with pytest.raises(S2ApiAuthError):
                await client.get_references(None, "2501.07642")
            await http.aclose()

        run(scenario())
        assert len(calls) == 1
        assert client.auth_errors == 1

    def test_rate_limiter_enforces_min_interval(self):
        times = []

        def handler(request):
            times.append(time.monotonic())
            return httpx.Response(200, json={"paperId": "abc"})

        transport = httpx.MockTransport(handler)
        http = httpx.AsyncClient(base_url=config.S2_API_BASE, transport=transport)
        client = S2ApiClient(min_interval=0.05, http_client=http)

        async def scenario():
            await client.start(1)
            await client.resolve_paper_id("2501.07642")
            await client.resolve_paper_id("2501.07643")
            await http.aclose()

        run(scenario())
        assert times[1] - times[0] >= 0.04

    def test_5xx_transient_error_retries_and_succeeds(self, monkeypatch):
        monkeypatch.setattr(config, "RETRY_JITTER", 0.0)
        monkeypatch.setattr(config, "BACKOFF_BASE", 0.0)
        calls = []

        def handler(request):
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(500, text="Internal Server Error")
            if request.method == "GET":
                return httpx.Response(200, json={
                    "data": [{"citedPaper": {"paperId": "p1"}}],
                    "next": None,
                })
            return httpx.Response(200, json=[paper_payload("p1", "2401.00001")])

        client, http = make_client(handler, max_retries=3)

        async def scenario():
            await client.start(1)
            refs = await client.get_references(None, "2501.07642")
            await http.aclose()
            return refs

        refs = run(scenario())
        assert len(refs) == 1
        assert refs[0].paper_id == "p1"
        assert len(calls) == 3  # 1st GET fails (500), 2nd GET succeeds (200), 3rd POST batch (200)

    def test_permanent_failure_raises_exception_not_empty_success(self, monkeypatch):
        from src.crawl_references_module.s2_api_client import S2ApiTransientError

        monkeypatch.setattr(config, "RETRY_JITTER", 0.0)
        monkeypatch.setattr(config, "BACKOFF_BASE", 0.0)

        def handler(request):
            return httpx.Response(500, text="Service Unavailable")

        client, http = make_client(handler, max_retries=2)

        async def scenario():
            await client.start(1)
            with pytest.raises(S2ApiTransientError):
                await client.get_references(None, "2501.07642")
            await http.aclose()

        run(scenario())
