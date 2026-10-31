"""Unit tests for S2ApiClient batch processing, fault tolerance, and identifier resolution."""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src import config
from src.crawl_references_module.s2_api_client import (
    S2ApiClient,
    S2ApiAuthError,
    S2ApiNotFound,
    S2ApiPayloadTooLarge,
    S2ApiRateLimited,
    S2ApiTransientError,
)
from src.crawl_references_module.s2_client import S2Paper


class DummyResponse:
    """Mock HTTP response for httpx."""

    def __init__(
        self,
        status_code: int = 200,
        json_data: any = None,
        headers: dict = None,
        content: bytes = None,
    ):
        self.status_code = status_code
        self._json_data = json_data
        self.headers = headers or {}
        if content is not None:
            self.content = content
        elif json_data is not None:
            self.content = json.dumps(json_data).encode("utf-8")
        else:
            self.content = b""
        self.text = self.content.decode("utf-8", errors="replace")

    def json(self):
        if self._json_data is not None:
            return self._json_data
        raise ValueError("No JSON")


class TestS2ApiClientBatch(unittest.IsolatedAsyncioTestCase):

    async def test_auth_headers_and_anonymous_rate_limiting(self):
        """Test x-api-key header and anonymous fallback rate interval."""
        # Case 1: With API Key
        client_keyed = S2ApiClient(api_key="secret-key-123")
        self.assertEqual(client_keyed.api_key, "secret-key-123")
        self.assertEqual(client_keyed.rate_limiter.min_interval, config.S2_API_MIN_INTERVAL)
        await client_keyed.start(tabs=1)
        self.assertEqual(client_keyed._http.headers.get("x-api-key"), "secret-key-123")
        await client_keyed.close()

        # Case 2: Without API Key (Anonymous pool)
        client_anon = S2ApiClient(api_key="")
        self.assertEqual(client_anon.api_key, "")
        self.assertEqual(client_anon.rate_limiter.min_interval, config.S2_API_ANONYMOUS_MIN_INTERVAL)
        await client_anon.start(tabs=1)
        self.assertNotIn("x-api-key", client_anon._http.headers)
        await client_anon.close()

    async def test_auth_error_fails_fast(self):
        """Test HTTP 401/403 raises S2ApiAuthError immediately without retrying."""
        mock_http = AsyncMock()
        mock_http.request.return_value = DummyResponse(status_code=403)
        client = S2ApiClient(api_key="bad-key", http_client=mock_http)
        client.rate_limiter.min_interval = 0.0

        with self.assertRaises(S2ApiAuthError):
            await client._request("GET", "/paper/arXiv:2502.00001")
        self.assertEqual(mock_http.request.call_count, 1)

    @patch("asyncio.sleep", new_callable=AsyncMock)
    async def test_rate_limit_and_exponential_backoff(self, mock_sleep):
        """Test HTTP 429 handles exponential backoff, jitter, and ceiling without Retry-After."""
        mock_http = AsyncMock()
        resp_429 = DummyResponse(status_code=429)
        resp_200 = DummyResponse(status_code=200, json_data={"paperId": "abc12345"})
        mock_http.request.side_effect = [resp_429, resp_200]

        client = S2ApiClient(api_key="test-key", http_client=mock_http)
        client.rate_limiter.min_interval = 0.0

        res = await client._request("GET", "/paper/arXiv:2502.00001")
        self.assertEqual(res, {"paperId": "abc12345"})
        self.assertEqual(mock_http.request.call_count, 2)
        self.assertEqual(client.rate_limit_hits, 1)

        # Verify that sleep was called with exponential backoff delay (cooldown + jitter <= max_backoff)
        self.assertGreaterEqual(mock_sleep.call_count, 1)
        slept_delay = mock_sleep.call_args_list[0][0][0]
        self.assertGreaterEqual(slept_delay, config.S2_RATE_LIMIT_COOLDOWN)
        self.assertLessEqual(slept_delay, config.S2_RATE_LIMIT_COOLDOWN + config.RETRY_JITTER)
        self.assertLessEqual(slept_delay, config.MAX_BACKOFF)

    async def test_batch_500_papers(self):
        """Test batching up to 500 papers in a single request."""
        paper_ids = [f"ARXIV:2502.{i:05d}" for i in range(1, 501)]
        expected_items = [
            {
                "paperId": f"s2_{i:05d}",
                "title": f"Paper Title {i}",
                "externalIds": {"ArXiv": f"2502.{i:05d}"},
                "year": 2025,
            }
            for i in range(1, 501)
        ]

        mock_http = AsyncMock()
        mock_http.request.return_value = DummyResponse(status_code=200, json_data=expected_items)

        client = S2ApiClient(http_client=mock_http)
        client.rate_limiter.min_interval = 0.0

        papers = await client.fetch_papers_batch(paper_ids)
        self.assertEqual(len(papers), 500)
        self.assertEqual(mock_http.request.call_count, 1)

        # Verify call payload
        call_kwargs = mock_http.request.call_args.kwargs
        self.assertEqual(len(call_kwargs["json"]["ids"]), 500)
        self.assertEqual(call_kwargs["params"]["fields"], config.S2_API_PAPER_FIELDS)

    async def test_flexible_identifiers_and_cache_mapping(self):
        """Test diverse ID formats (ARXIV, CorpusId, SHA-40) and null handling."""
        query_ids = [
            "ARXIV:2106.15928",
            "CorpusId:235688001",
            "649def34f8be52c8b66281af98ae884c09aef38b",
            "nonexistent_id",
        ]
        api_response = [
            {
                "paperId": "649def34f8be52c8b66281af98ae884c09aef38b",
                "title": "ArXiv Paper",
                "externalIds": {"ArXiv": "2106.15928"},
                "year": 2021,
            },
            {
                "paperId": "corpus_paper_sha",
                "title": "Corpus Paper",
                "externalIds": {"CorpusId": 235688001},
                "year": 2021,
            },
            {
                "paperId": "649def34f8be52c8b66281af98ae884c09aef38b",
                "title": "Direct SHA Paper",
                "year": 2020,
            },
            None,  # Not found
        ]

        mock_http = AsyncMock()
        mock_http.request.return_value = DummyResponse(status_code=200, json_data=api_response)

        client = S2ApiClient(http_client=mock_http)
        client.rate_limiter.min_interval = 0.0

        papers = await client.fetch_papers_batch(query_ids)
        # Should resolve 2 distinct papers without crashing on None
        self.assertEqual(len(papers), 2)

        # Cache should be populated by both query_id and paperId
        self.assertIn("ARXIV:2106.15928", client._ref_cache)
        self.assertIn("2106.15928", client._ref_cache)
        self.assertIn("CorpusId:235688001", client._ref_cache)
        self.assertIn("649def34f8be52c8b66281af98ae884c09aef38b", client._ref_cache)

    async def test_adaptive_batch_subdivision_on_413_payload_too_large(self):
        """Test recursive subdivision when a batch hits HTTP 413."""
        chunk_4 = ["id_1", "id_2", "id_3", "id_4"]

        def mock_request(method, path, **kwargs):
            payload_ids = kwargs.get("json", {}).get("ids", [])
            if len(payload_ids) > 2:
                return DummyResponse(status_code=413)  # Too large!
            # Sub-batch succeeds
            items = [
                {"paperId": pid, "title": f"Title {pid}", "year": 2024}
                for pid in payload_ids
            ]
            return DummyResponse(status_code=200, json_data=items)

        mock_http = AsyncMock()
        mock_http.request.side_effect = mock_request

        client = S2ApiClient(http_client=mock_http)
        client.rate_limiter.min_interval = 0.0

        papers = await client.fetch_papers_batch(chunk_4)
        self.assertEqual(len(papers), 4)
        # 1 call of 4 (failed with 413) + 2 sub-calls of 2 (both succeeded) = 3 calls
        self.assertEqual(mock_http.request.call_count, 3)

    async def test_adaptive_subdivision_on_payload_over_10mb(self):
        """Test client detects response body > 10MB and subdivides."""
        chunk_2 = ["paper_heavy_1", "paper_heavy_2"]

        def mock_request(method, path, **kwargs):
            payload_ids = kwargs.get("json", {}).get("ids", [])
            if len(payload_ids) == 2:
                # Return payload simulating > 10MB
                fake_11mb = b"X" * (11 * 1024 * 1024)
                return DummyResponse(
                    status_code=200,
                    headers={"content-length": str(len(fake_11mb))},
                    content=fake_11mb,
                )
            # Individual paper succeeds
            pid = payload_ids[0]
            item = [{"paperId": pid, "title": f"Title {pid}", "year": 2024}]
            return DummyResponse(status_code=200, json_data=item)

        mock_http = AsyncMock()
        mock_http.request.side_effect = mock_request

        client = S2ApiClient(http_client=mock_http)
        client.rate_limiter.min_interval = 0.0

        papers = await client.fetch_papers_batch(chunk_2)
        self.assertEqual(len(papers), 2)
        # 1 call for 2 (aborted due to >10MB) + 2 calls for 1 each = 3 calls
        self.assertEqual(mock_http.request.call_count, 3)

    async def test_custom_fields_and_dot_notation(self):
        """Test custom fields parameter including dot notation for nested fields."""
        custom_fields = "title,authors.name,authors.affiliations,citations.title,embedding.specter_v2"
        mock_http = AsyncMock()
        mock_http.request.return_value = DummyResponse(
            status_code=200,
            json_data=[{"paperId": "p1", "title": "Paper 1"}],
        )

        client = S2ApiClient(http_client=mock_http)
        client.rate_limiter.min_interval = 0.0

        await client.fetch_papers_batch(["p1"], fields=custom_fields)
        call_kwargs = mock_http.request.call_args.kwargs
        self.assertEqual(call_kwargs["params"]["fields"], custom_fields)

    async def test_fetch_papers_batch_raw(self):
        """Test fetch_papers_batch_raw returns 1-to-1 list preserving order and None."""
        raw_ids = ["p1", "missing", "p2"]
        mock_items = [
            {"paperId": "p1", "title": "One"},
            None,
            {"paperId": "p2", "title": "Two"},
        ]
        mock_http = AsyncMock()
        mock_http.request.return_value = DummyResponse(status_code=200, json_data=mock_items)

        client = S2ApiClient(http_client=mock_http)
        client.rate_limiter.min_interval = 0.0

        raw_results = await client.fetch_papers_batch_raw(raw_ids)
        self.assertEqual(len(raw_results), 3)
        self.assertEqual(raw_results[0]["paperId"], "p1")
        self.assertIsNone(raw_results[1])
        self.assertEqual(raw_results[2]["paperId"], "p2")

    async def test_get_references_batch(self):
        """Test get_references_batch sends POST /paper/batch with json ids and extracts references."""
        query_ids = ["2502.00001", "2502.00002"]
        mock_response = [
            {
                "paperId": "p1",
                "title": "Paper One",
                "references": [
                    {
                        "paperId": "ref_p1",
                        "title": "Cited Paper",
                        "authors": [{"name": "Alice"}],
                        "year": 2023,
                        "externalIds": {"ArXiv": "2301.00001"},
                    }
                ],
            },
            None,  # Not found
        ]

        mock_http = AsyncMock()
        mock_http.request.return_value = DummyResponse(status_code=200, json_data=mock_response)

        client = S2ApiClient(http_client=mock_http)
        client.rate_limiter.min_interval = 0.0

        results = await client.get_references_batch(query_ids)
        self.assertEqual(len(results), 2)
        # Check call payload
        call_kwargs = mock_http.request.call_args.kwargs
        self.assertEqual(call_kwargs["json"]["ids"], ["ARXIV:2502.00001", "ARXIV:2502.00002"])
        self.assertIn("references.paperId", call_kwargs["params"]["fields"])

        # Check item 0 (found with 1 ref)
        self.assertEqual(results[0]["arxiv_id"], "2502.00001")
        self.assertEqual(results[0]["status"], "ok")
        self.assertEqual(len(results[0]["refs"]), 1)
        self.assertEqual(results[0]["refs"][0].arxiv_id, "2301.00001")

        # Check item 1 (not found)
        self.assertEqual(results[1]["arxiv_id"], "2502.00002")
        self.assertEqual(results[1]["status"], "not_found")
        self.assertEqual(len(results[1]["refs"]), 0)


if __name__ == "__main__":
    unittest.main()
