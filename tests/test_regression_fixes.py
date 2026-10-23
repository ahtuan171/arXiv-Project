"""Regression tests for the crawler fixes: Problems B1, B2, C1, D1, E.

These tests use mocked HTTP responses via ``httpx.MockTransport`` and never
make real API calls.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from email.utils import format_datetime

import httpx
import pytest

from src import config
from src.crawler import (
    CrawlPaths,
    _is_done,
    _ledger_entry,
    build_reference_record,
    crawl_references,
)
from src.s2_api_client import (
    ReferenceResult,
    S2ApiClient,
    S2ApiRateLimited,
    S2ApiTransientError,
    _parse_retry_after,
    is_sufficiently_complete,
    merge_paper,
)
from src.s2_client import S2Paper
from src.stats import CrawlStats
from src.utils import canonical_arxiv_id, load_latest_records, read_jsonl

MEMBER = "member1"
FIRST_ID = "2501.07642"


def run(coro):
    return asyncio.run(coro)


def make_client(handler, **kwargs):
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(base_url=config.S2_API_BASE, transport=transport)
    return S2ApiClient(min_interval=0, http_client=http, **kwargs), http


def make_paths(tmp_path):
    return CrawlPaths(
        references=tmp_path / "references.jsonl",
        ledger=tmp_path / "ledger.jsonl",
        log=tmp_path / "crawl.log",
        stats_json=tmp_path / "stats.json",
        stats_markdown=tmp_path / "stats.md",
    )


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


# ============================================================================
# Problem B1 — References without paperId
# ============================================================================


class TestProblemB1:
    """Missing paperId should not discard references that have a valid arXiv ID."""

    def test_missing_paper_id_with_valid_arxiv_in_external_ids(self):
        """B1: paperId=None + externalIds.ArXiv='2401.00001' → kept."""
        def handler(request):
            if request.method == "GET":
                return httpx.Response(200, json={
                    "data": [
                        {
                            "citedPaper": {
                                "paperId": None,
                                "externalIds": {"ArXiv": "2401.00001"},
                                "title": "ArXiv-Only Paper",
                                "authors": [{"name": "A"}],
                                "year": 2024,
                                "publicationDate": "2024-01-01",
                            }
                        },
                    ],
                    "next": None,
                })
            return httpx.Response(500, text="should not batch")

        client, http = make_client(handler)

        async def scenario():
            await client.start(1)
            refs = await client.get_references(None, FIRST_ID)
            await http.aclose()
            return refs

        refs = run(scenario())
        assert len(refs) == 1
        assert refs[0].arxiv_id == "2401.00001"
        assert refs[0].paper_id == ""  # no S2 paperId
        assert isinstance(refs, ReferenceResult)
        assert refs.refs_with_paper_id == 0
        assert refs.refs_with_arxiv_id == 1
        assert refs.refs_unresolved == 0

    def test_missing_paper_id_no_arxiv_id_is_unresolved(self):
        """B1: paperId=None + externalIds=None → classified as unresolved."""
        def handler(request):
            if request.method == "GET":
                return httpx.Response(200, json={
                    "data": [
                        {"citedPaper": {"paperId": None}},
                        {"citedPaper": {"paperId": None, "externalIds": {"DOI": "10.1/x"}}},
                        {"citedPaper": {"paperId": None, "externalIds": None}},
                    ],
                    "next": None,
                })
            return httpx.Response(200, json=[])

        client, http = make_client(handler)

        async def scenario():
            await client.start(1)
            refs = await client.get_references(None, FIRST_ID)
            await http.aclose()
            return refs

        refs = run(scenario())
        assert len(refs) == 0  # all unresolvable
        assert refs.raw_refs_received == 3
        assert refs.refs_unresolved == 3
        assert refs.refs_with_paper_id == 0
        assert len(refs.unresolved_details) == 3

    def test_missing_paper_id_with_arxiv_creates_edge(self):
        """B1: a reference retained by arXiv ID should produce a valid edge record."""
        ref = S2Paper(
            paper_id="",
            arxiv_id="2401.00001",
            title="Test",
            authors=["A"],
            year=2024,
            publication_date="2024-01-01",
        )
        record = build_reference_record(FIRST_ID, ref, MEMBER, "2026-01-01T00:00:00+00:00")
        assert record is not None
        assert record["ref_arxiv_id"] == "2401_00001"
        assert record["ref_s2_id"] == ""
        assert record["ref_submission_date"] == "2024-01-01"

    def test_unresolvable_ref_does_not_create_edge(self):
        """B1: a reference with no arXiv ID produces no edge."""
        ref = S2Paper(
            paper_id="s2id",
            arxiv_id=None,
            title="No arXiv",
            authors=[],
            year=None,
            publication_date=None,
        )
        assert build_reference_record(FIRST_ID, ref, MEMBER, "now") is None


# ============================================================================
# Problem B2 — Completeness policy and metadata fallback
# ============================================================================


class TestProblemB2:
    """Partial direct metadata should trigger fallback; complete should not."""

    def test_complete_direct_metadata_skips_batch(self):
        """B2: complete record with externalIds → no POST batch request."""
        requests = []

        def handler(request):
            requests.append(request)
            if request.method == "GET":
                return httpx.Response(200, json={
                    "data": [{
                        "citedPaper": {
                            "paperId": "p1",
                            "externalIds": {"ArXiv": "2401.00001"},
                            "title": "Complete",
                            "authors": [{"name": "A"}],
                            "year": 2024,
                            "publicationDate": "2024-01-01",
                        }
                    }],
                    "next": None,
                })
            return httpx.Response(500, text="batch should not be called")

        client, http = make_client(handler)

        async def scenario():
            await client.start(1)
            refs = await client.get_references(None, FIRST_ID)
            await http.aclose()
            return refs

        refs = run(scenario())
        assert len(refs) == 1
        assert refs[0].title == "Complete"
        post_calls = [r for r in requests if r.method == "POST"]
        assert len(post_calls) == 0

    def test_title_only_triggers_fallback(self):
        """B2: title only (no externalIds) → needs_fallback → batch lookup."""
        requests = []

        def handler(request):
            requests.append(request)
            if request.method == "GET":
                return httpx.Response(200, json={
                    "data": [{
                        "citedPaper": {
                            "paperId": "p1",
                            "title": "Title Only",
                        }
                    }],
                    "next": None,
                })
            return httpx.Response(200, json=[
                paper_payload("p1", "2401.00001", title="Full Title"),
            ])

        client, http = make_client(handler)

        async def scenario():
            await client.start(1)
            refs = await client.get_references(None, FIRST_ID)
            await http.aclose()
            return refs

        refs = run(scenario())
        assert len(refs) == 1
        assert refs[0].arxiv_id == "2401.00001"
        assert refs[0].title == "Title Only"  # direct value preserved
        assert refs.fallback_requests == 1
        post_calls = [r for r in requests if r.method == "POST"]
        assert len(post_calls) == 1

    def test_merge_paper_does_not_overwrite_valid_with_none(self):
        """B2: merge_paper preserves valid base fields when fallback has None."""
        base = S2Paper("p1", "2401.00001", "Good Title", ["A", "B"], 2024, "2024-01-01")
        fallback = S2Paper("p1", None, "", [], None, None)
        merged = merge_paper(base, fallback)
        assert merged.arxiv_id == "2401.00001"
        assert merged.title == "Good Title"
        assert merged.authors == ["A", "B"]
        assert merged.year == 2024
        assert merged.publication_date == "2024-01-01"

    def test_merge_paper_fills_missing_fields(self):
        """B2: merge_paper fills in fields missing from base."""
        base = S2Paper("p1", None, "", [], None, None)
        fallback = S2Paper("p1", "2401.00001", "Filled", ["X"], 2024, "2024-06-01")
        merged = merge_paper(base, fallback)
        assert merged.arxiv_id == "2401.00001"
        assert merged.title == "Filled"
        assert merged.authors == ["X"]
        assert merged.year == 2024

    def test_is_sufficiently_complete(self):
        """B2: completeness policy classification."""
        complete = S2Paper("p1", "2401.00001", "T", ["A"], 2024, "2024-01-01")
        raw = {"externalIds": {"ArXiv": "2401.00001"}}
        assert is_sufficiently_complete(complete, raw) is True

        no_title = S2Paper("p1", "2401.00001", "", ["A"], 2024, "2024-01-01")
        assert is_sufficiently_complete(no_title, raw) is False

        no_external = S2Paper("p1", "2401.00001", "T", ["A"], 2024, "2024-01-01")
        raw_no_ext = {"title": "T"}  # no externalIds key
        assert is_sufficiently_complete(no_external, raw_no_ext) is False

        # arXiv paper without authors
        no_authors = S2Paper("p1", "2401.00001", "T", [], 2024, "2024-01-01")
        assert is_sufficiently_complete(no_authors, raw) is False


# ============================================================================
# Problem C1 — Metrics counting
# ============================================================================


class TestProblemC1:
    """Metrics must distinguish raw refs, identified, unresolved, and edges."""

    def test_reference_result_metrics(self):
        """C1: ReferenceResult carries per-paper granular metrics."""
        def handler(request):
            if request.method == "GET":
                return httpx.Response(200, json={
                    "data": [
                        {"citedPaper": {"paperId": "p1",
                                        "externalIds": {"ArXiv": "2401.00001"},
                                        "title": "Good", "authors": [{"name": "A"}],
                                        "year": 2024, "publicationDate": "2024-01-01"}},
                        {"citedPaper": {"paperId": None}},  # unresolved
                        {"citedPaper": {"paperId": "p2",
                                        "externalIds": {"DOI": "10.1/x"},
                                        "title": "No arXiv", "authors": [{"name": "B"}],
                                        "year": 2023, "publicationDate": "2023-01-01"}},
                    ],
                    "next": None,
                })
            return httpx.Response(500, text="no batch needed")

        client, http = make_client(handler)

        async def scenario():
            await client.start(1)
            refs = await client.get_references(None, FIRST_ID)
            await http.aclose()
            return refs

        refs = run(scenario())
        assert refs.raw_refs_received == 3
        assert refs.refs_with_paper_id == 2
        assert refs.refs_unresolved == 1
        assert refs.refs_with_arxiv_id == 1
        assert len(refs) == 2  # only resolved refs

    def test_stats_add_paper_accumulates_granular_metrics(self):
        """C1: CrawlStats.add_paper accumulates new per-paper metrics."""
        stats = CrawlStats()
        refs = [
            S2Paper("p1", "2401.00001", "T", [], 2024, None),
            S2Paper("p2", None, "T2", [], 2023, None),
        ]
        stats.add_paper(
            refs, written=1,
            raw_refs_received=5,
            refs_with_paper_id=3,
            refs_unresolved=2,
            fallback_requests=1,
            is_partial=True,
        )
        assert stats.raw_refs_received == 5
        assert stats.refs_with_paper_id == 3
        assert stats.refs_unresolved == 2
        assert stats.fallback_requests == 1
        assert stats.partial == 1
        assert stats.edges_written == 1  # alias
        assert stats.refs_with_arxiv_id == 1  # alias


# ============================================================================
# Problem D1 — Ledger status: ok vs partial vs error
# ============================================================================


class TestProblemD1:
    """Ledger status must distinguish complete, partial, and error."""

    def test_is_done_ok_is_always_done(self):
        assert _is_done({"status": "ok"}, retry_errors=False, retry_not_found=False) is True
        assert _is_done({"status": "ok"}, retry_errors=True, retry_not_found=True) is True

    def test_is_done_partial_respects_retry_partial(self):
        rec = {"status": "partial", "attempts": 1}
        # Default: partial is treated as done (no infinite recrawl)
        assert _is_done(rec, retry_errors=False, retry_not_found=False) is True
        assert _is_done(rec, retry_errors=False, retry_not_found=False, retry_partial=False) is True
        # With --retry-partial: partial is retried
        assert _is_done(rec, retry_errors=False, retry_not_found=False, retry_partial=True) is False

    def test_is_done_error_respects_max_attempts(self):
        rec = {"status": "error", "attempts": config.MAX_PAPER_ATTEMPTS}
        assert _is_done(rec, retry_errors=False, retry_not_found=False) is True
        assert _is_done(rec, retry_errors=True, retry_not_found=False) is False

    def test_ledger_entry_has_granular_fields(self):
        entry = _ledger_entry(
            "2501.07642", MEMBER,
            status="partial", attempts=1,
            refs_total=10, refs_with_arxiv=5, refs_written=5,
            raw_refs_received=12, refs_with_paper_id=8,
            refs_unresolved=2, fallback_requests=1,
            completeness="partial",
        )
        assert entry["status"] == "partial"
        assert entry["raw_refs_received"] == 12
        assert entry["refs_unresolved"] == 2
        assert entry["completeness"] == "partial"
        assert entry["arxiv_id"] == "2501_07642"

    def test_crawler_sets_partial_status_for_unresolved_refs(self, tmp_path, quiet_log):
        """D1: End-to-end: paper with unresolved refs gets 'partial' ledger status."""
        paths = make_paths(tmp_path)

        def handler(request):
            if request.method == "GET":
                return httpx.Response(200, json={
                    "data": [
                        {"citedPaper": {
                            "paperId": "p1",
                            "externalIds": {"ArXiv": "2401.00001"},
                            "title": "Ok", "authors": [{"name": "A"}],
                            "year": 2024, "publicationDate": "2024-01-01",
                        }},
                        {"citedPaper": {"paperId": None}},  # unresolved
                    ],
                    "next": None,
                })
            return httpx.Response(500, text="no batch needed")

        transport = httpx.MockTransport(handler)
        http = httpx.AsyncClient(base_url=config.S2_API_BASE, transport=transport)
        api = S2ApiClient(min_interval=0, http_client=http)

        result = crawl_references(
            MEMBER, paths=paths, api_client=api,
            client_type="api", limit=1, tabs=1, log=quiet_log,
        )
        assert not result.fatal
        assert result.stats.processed == 1
        assert result.stats.partial == 1

        ledger = load_latest_records(paths.ledger)
        key = canonical_arxiv_id(FIRST_ID)
        assert ledger[key]["status"] == "partial"
        assert ledger[key]["completeness"] == "partial"
        assert ledger[key]["refs_unresolved"] == 1

        # Resume without retry_partial → paper is skipped
        result2 = crawl_references(
            MEMBER, paths=paths, api_client=api,
            client_type="api", limit=1, tabs=1, log=quiet_log,
        )
        assert result2.stats.skipped >= 1

    def test_crawler_sets_ok_status_for_complete_refs(self, tmp_path, quiet_log):
        """D1: paper where all refs resolve gets 'ok' status."""
        paths = make_paths(tmp_path)

        def handler(request):
            if request.method == "GET":
                return httpx.Response(200, json={
                    "data": [{
                        "citedPaper": {
                            "paperId": "p1",
                            "externalIds": {"ArXiv": "2401.00001"},
                            "title": "Complete",
                            "authors": [{"name": "A"}],
                            "year": 2024,
                            "publicationDate": "2024-01-01",
                        }
                    }],
                    "next": None,
                })
            return httpx.Response(500, text="no batch")

        transport = httpx.MockTransport(handler)
        http = httpx.AsyncClient(base_url=config.S2_API_BASE, transport=transport)
        api = S2ApiClient(min_interval=0, http_client=http)

        result = crawl_references(
            MEMBER, paths=paths, api_client=api,
            client_type="api", limit=1, tabs=1, log=quiet_log,
        )
        assert not result.fatal
        assert result.stats.partial == 0

        ledger = load_latest_records(paths.ledger)
        key = canonical_arxiv_id(FIRST_ID)
        assert ledger[key]["status"] == "ok"
        assert ledger[key]["completeness"] == "complete"


# ============================================================================
# Problem E — Retry and rate-limit behavior
# ============================================================================


class TestProblemE:
    """Retry, backoff, and Retry-After parsing."""

    def test_parse_retry_after_delay_seconds(self):
        assert _parse_retry_after("5") == 5.0
        assert _parse_retry_after("0") == 0.0
        assert _parse_retry_after("0.5") == 0.5
        # Capped at MAX_RETRY_AFTER
        assert _parse_retry_after("999999") == config.MAX_RETRY_AFTER

    def test_parse_retry_after_http_date(self):
        """E: HTTP-date format like 'Thu, 01 Dec 2022 16:00:00 GMT'."""
        future = datetime(2099, 1, 1, tzinfo=timezone.utc)
        header = format_datetime(future, usegmt=True)
        result = _parse_retry_after(header)
        assert result is not None
        assert result > 0
        assert result <= config.MAX_RETRY_AFTER

    def test_parse_retry_after_none(self):
        assert _parse_retry_after(None) is None

    def test_parse_retry_after_garbage(self):
        assert _parse_retry_after("not-a-number-or-date") is None

    def test_429_followed_by_success(self, monkeypatch):
        """E: 429 → retry → success."""
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
            result = await client.resolve_paper_id(FIRST_ID)
            await http.aclose()
            return result

        assert run(scenario()) == "abc"
        assert len(calls) == 2
        assert client.rate_limit_hits == 1

    def test_429_exhausts_retries(self, monkeypatch):
        """E: continuous 429 → retry exhaustion → S2ApiRateLimited raised."""
        monkeypatch.setattr(config, "RETRY_JITTER", 0.0)
        monkeypatch.setattr(config, "BACKOFF_BASE", 0.0)

        def handler(request):
            return httpx.Response(429, headers={"Retry-After": "0"})

        client, http = make_client(handler, max_retries=2)

        async def scenario():
            await client.start(1)
            with pytest.raises(S2ApiRateLimited):
                await client.resolve_paper_id(FIRST_ID)
            await http.aclose()

        run(scenario())
        assert client.api_calls == 2

    def test_5xx_is_retried_and_succeeds(self, monkeypatch):
        """E: transient 500 → retry → success."""
        monkeypatch.setattr(config, "RETRY_JITTER", 0.0)
        monkeypatch.setattr(config, "BACKOFF_BASE", 0.0)
        calls = []

        def handler(request):
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(500, text="error")
            return httpx.Response(200, json={"paperId": "abc"})

        client, http = make_client(handler, max_retries=3)

        async def scenario():
            await client.start(1)
            result = await client.resolve_paper_id(FIRST_ID)
            await http.aclose()
            return result

        assert run(scenario()) == "abc"
        assert len(calls) == 2

    def test_5xx_exhaustion_raises_transient_error(self, monkeypatch):
        """E: persistent 500 → raises S2ApiTransientError."""
        monkeypatch.setattr(config, "RETRY_JITTER", 0.0)
        monkeypatch.setattr(config, "BACKOFF_BASE", 0.0)

        def handler(request):
            return httpx.Response(500, text="down")

        client, http = make_client(handler, max_retries=2)

        async def scenario():
            await client.start(1)
            with pytest.raises(S2ApiTransientError):
                await client.resolve_paper_id(FIRST_ID)
            await http.aclose()

        run(scenario())

    def test_429_with_http_date_retry_after(self, monkeypatch):
        """E: 429 with HTTP-date Retry-After header is parsed and respected."""
        monkeypatch.setattr(config, "RETRY_JITTER", 0.0)
        calls = []

        def handler(request):
            calls.append(request)
            if len(calls) == 1:
                future = datetime(2099, 1, 1, tzinfo=timezone.utc)
                # The delay will be capped at MAX_RETRY_AFTER
                return httpx.Response(429, headers={
                    "Retry-After": format_datetime(future, usegmt=True),
                })
            return httpx.Response(200, json={"paperId": "abc"})

        client, http = make_client(handler, max_retries=3)

        async def scenario():
            await client.start(1)
            # This would take forever with the real delay;
            # override MAX_RETRY_AFTER to 0 so it completes quickly
            monkeypatch.setattr(config, "MAX_RETRY_AFTER", 0.0)
            monkeypatch.setattr(config, "MAX_BACKOFF", 0.0)
            result = await client.resolve_paper_id(FIRST_ID)
            await http.aclose()
            return result

        assert run(scenario()) == "abc"
        assert client.rate_limit_hits == 1


# ============================================================================
# Schema regression
# ============================================================================


class TestSchemaRegression:
    """Ensure ref_submission_date mapping and output schema are preserved."""

    def test_ref_submission_date_uses_publication_date(self):
        ref = S2Paper("p1", "2401.00001", "T", ["A"], 2024, "2024-06-15")
        record = build_reference_record(FIRST_ID, ref, MEMBER, "now")
        assert record["ref_submission_date"] == "2024-06-15"

    def test_ref_submission_date_empty_when_no_date(self):
        ref = S2Paper("p1", "2401.00001", "T", ["A"], 2024, None)
        record = build_reference_record(FIRST_ID, ref, MEMBER, "now")
        assert record["ref_submission_date"] == ""

    def test_edge_record_has_all_required_columns(self):
        ref = S2Paper("p1", "2401.00001", "T", ["A"], 2024, "2024-01-01")
        record = build_reference_record(FIRST_ID, ref, MEMBER, "2026-01-01T00:00:00+00:00")
        expected = {
            "arxiv_id", "ref_arxiv_id", "ref_title",
            "ref_authors", "ref_submission_date", "ref_s2_id",
        }
        assert set(record.keys()) == expected

    def test_no_edge_for_invalid_arxiv_id(self):
        ref = S2Paper("p1", "not-an-arxiv-id!!!", "T", [], None, None)
        assert build_reference_record(FIRST_ID, ref, MEMBER, "now") is None


@pytest.fixture
def quiet_log():
    return logging.getLogger("tests.regression")
