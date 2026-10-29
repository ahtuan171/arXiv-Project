from pathlib import Path
import pytest
from src.crawl_references_module.crawler import crawl_references_range
from src.crawl_references_module.s2_api_client import S2ApiAuthError

class AuthFailClient:
    async def get_references(self, page, arxiv_id):
        raise S2ApiAuthError("HTTP 401 Unauthorized")

def test_range_auth_error_fails_fast(tmp_path: Path):
    client = AuthFailClient()
    with pytest.raises(S2ApiAuthError):
        crawl_references_range(
            id_list=["2502.00001", "2502.00002"],
            client=client,
            out_path=tmp_path / "references.jsonl",
            csv_path=tmp_path / "m1_reference_stats.csv",
            resume=False,
        )


class MockBatchClient:
    def __init__(self):
        self.batch_calls = 0

    async def get_references_batch(self, batch_ids):
        from src.crawl_references_module.s2_client import S2Paper
        self.batch_calls += 1
        results = []
        for i, cid in enumerate(batch_ids):
            if cid == "2502.00002":
                results.append({
                    "arxiv_id": cid,
                    "status": "not_found",
                    "refs": [],
                    "refs_unresolved": 0,
                })
            else:
                ref = S2Paper(
                    paper_id=f"ref_{i}",
                    arxiv_id=f"2301.{i:05d}",
                    title=f"Ref {i}",
                    authors=["Author A"],
                    year=2023,
                    publication_date="2023-01-01",
                )
                results.append({
                    "arxiv_id": cid,
                    "status": "ok",
                    "refs": [ref],
                    "refs_unresolved": 0,
                })
        return results


def test_crawl_references_range_uses_batch_client(tmp_path: Path):
    client = MockBatchClient()
    out_p = tmp_path / "references.jsonl"
    csv_p = tmp_path / "m1_reference_stats.csv"

    df = crawl_references_range(
        id_list=["2502.00001", "2502.00002"],
        client=client,
        out_path=out_p,
        csv_path=csv_p,
        resume=False,
    )
    assert len(df) == 2
    assert client.batch_calls == 1
    # Check CSV contents
    row1 = df[df["arxiv_id"] == "2502.00001"].iloc[0]
    row2 = df[df["arxiv_id"] == "2502.00002"].iloc[0]
    assert row1["ref_status"] == "success"
    assert row1["num_crawled_refs"] == 1
    assert row1["num_arxiv_refs"] == 1
    assert row2["ref_status"] == "not_found"
    assert row2["num_crawled_refs"] == 0
    # Check references.jsonl
    assert out_p.exists()
    lines = out_p.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1

