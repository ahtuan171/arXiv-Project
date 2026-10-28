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
