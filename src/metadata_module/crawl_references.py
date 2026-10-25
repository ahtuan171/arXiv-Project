"""Crawl Semantic Scholar references for papers in data/papers.jsonl -> data/references.jsonl.

Chay tu goc repo:
    python -m src.metadata_module.crawl_references
    python -m src.metadata_module.crawl_references --papers data/papers.jsonl --out data/references.jsonl
    python -m src.metadata_module.crawl_references --limit 20          # thu 20 paper dau

Dieu kien truoc: data/papers.jsonl phai ton tai (chay crawl_papers truoc).

Output (Data Schema v1) -- moi dong JSONL la mot dict:
    arxiv_id    : str           paper goc (dang "2502_13235")
    s2_paper_id : str | null    S2 paper ID cua paper goc
    refs        : list[dict]    danh sach cac reference, moi ref co:
        ref_arxiv_id       : str | null   clean arXiv ID cua reference ("1706_03762")
                                          null neu reference khong co tren arXiv
        title              : str | null
        authors            : list[str]
        year               : int | null
        semantic_scholar_id: str | null   S2 paper ID cua reference
        external_ids       : dict         toan bo externalIds tu S2 (DOI, MAG, ...)
    fetched_at  : str           ISO-8601 UTC timestamp luc crawl

Neu S2 khong tim thay paper, record co them "error": "s2_not_found" va refs = [].
Neu goi API loi sau nhieu lan retry, record co "error": "fetch_failed: ...".

Resume: script kiem tra data/references.jsonl da co -> bo qua ID da crawl -> an toan khi chay lai.
"""

from __future__ import annotations

import argparse
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Union

import requests

from src import config, utils

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------
log = utils.setup_logging(
    "crawl_references",
    log_file=config.LOG_DIR / "crawl_references.log",
)

# ---------------------------------------------------------------------------
# Rate limiter cho Semantic Scholar
# ---------------------------------------------------------------------------
# S2: 1 request/giay voi API key (da duoc dat trong config.S2_MIN_INTERVAL)
_s2_limiter = utils.RateLimiter(config.S2_MIN_INTERVAL)

# S2 API endpoint va fields can lay
_S2_BASE = "https://api.semanticscholar.org/graph/v1/paper"
_S2_FIELDS = ",".join([
    "paperId",
    "references.paperId",
    "references.externalIds",
    "references.title",
    "references.authors",
    "references.year",
])

# Header API key (lay tu bien moi truong S2_API_KEY)
_S2_HEADERS: dict[str, str] = {}
if config.S2_API_KEY:
    _S2_HEADERS["x-api-key"] = config.S2_API_KEY


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def normalize_ref_arxiv_id(raw_id: Optional[str]) -> Optional[str]:
    """Chuan hoa arXiv ID cua mot reference thanh dang "YYMM_NNNNN".

    - Xu ly ID kieu moi: "2310.12345" hoac "2310.12345v2" -> "2310_12345"
    - Xu ly ID kieu cu (truoc 2007): "hep-th/9901001" -> "hep-th_9901001"
    - Neu raw_id la None hoac khong khop -> tra ve None
    """
    if not raw_id:
        return None
    if re.match(r"^[a-zA-Z.\-]+/\d+$", raw_id):
        return utils.clean_arxiv_id(raw_id)
    new_style = re.match(r"^(\d{4}\.\d{4,5})(v\d+)?$", raw_id)
    if new_style:
        return utils.clean_arxiv_id(new_style.group(1))
    return None


def parse_refs(raw_refs: list[dict]) -> list[dict]:
    """Chuyen danh sach reference tho tu S2 API thanh list theo schema."""
    refs = []
    for r in raw_refs:
        ext = r.get("externalIds") or {}
        refs.append({
            "ref_arxiv_id": normalize_ref_arxiv_id(ext.get("ArXiv")),
            "title": r.get("title"),
            "authors": [a["name"] for a in (r.get("authors") or [])],
            "year": r.get("year"),
            "semantic_scholar_id": r.get("paperId"),
            "external_ids": ext,
        })
    return refs


def error_record(arxiv_id: str, error: str) -> dict[str, Any]:
    return {
        "arxiv_id": arxiv_id,
        "s2_paper_id": None,
        "refs": [],
        "error": error,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


# ---------------------------------------------------------------------------
# Fetch function (voi retry cho loi mang va 429)
# ---------------------------------------------------------------------------

@utils.retry(
    max_attempts=config.MAX_RETRIES,
    backoff=config.BACKOFF_BASE,
    exceptions=(requests.RequestException,),
)
def try_fetch_s2(arxiv_id_clean: str, api_key: Optional[str] = None) -> Optional[dict]:
    """Goi S2 API, tra ve data dict hoac None neu 404.

    arxiv_id_clean: dang "2502_13235" (da clean).
    Tra ve None neu S2 khong tim thay paper (404).
    """
    _s2_limiter.wait()

    canonical = arxiv_id_clean.replace("_", ".")
    url = f"{_S2_BASE}/arXiv:{canonical}"

    headers = dict(_S2_HEADERS)
    if api_key:
        headers["x-api-key"] = api_key

    resp = requests.get(
        url,
        params={"fields": _S2_FIELDS},
        headers=headers,
        timeout=config.REQUEST_TIMEOUT,
    )

    if resp.status_code == 404:
        return None

    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Core Crawl Function
# ---------------------------------------------------------------------------

def crawl_references(
    papers_path: Union[Path, str] = config.DATA_DIR / "papers.jsonl",
    out_path: Union[Path, str] = config.DATA_DIR / "references.jsonl",
    limit: int = 0,
    api_key: Optional[str] = None,
) -> dict[str, Any]:
    """Crawl Semantic Scholar references va luu vao file JSONL.

    Args:
        papers_path: Duong dan toi file papers.jsonl da crawl truoc do.
        out_path: Duong dan luu references.jsonl.
        limit: Gioi han so bai can crawl (0 = tat ca).
        api_key: Tuy chon API key cua Semantic Scholar.

    Returns:
        dict thong ke so luong va thoi gian thuc hien.
    """
    p_path = Path(papers_path)
    o_path = Path(out_path)

    if not p_path.exists():
        raise FileNotFoundError(f"Khong tim thay {p_path}. Hay chay crawl_papers truoc!")

    key = api_key or config.S2_API_KEY
    if not key:
        log.warning(
            "Bien moi truong S2_API_KEY chua duoc dat. "
            "Request se dung pool unauthenticated (de bi throttle hon)."
        )

    paper_ids = [
        r["arxiv_id"]
        for r in utils.read_jsonl(p_path)
        if "error" not in r and "arxiv_id" in r
    ]
    log.info("Tong paper trong papers.jsonl (thanh cong): %d", len(paper_ids))

    done: set[str] = set()
    if o_path.exists():
        for r in utils.read_jsonl(o_path):
            pid = r.get("arxiv_id")
            err = str(r.get("error") or "")
            if pid and not err.startswith("fetch_failed"):
                done.add(pid)
    todo = [pid for pid in paper_ids if pid not in done]

    if limit > 0:
        todo = todo[:limit]

    log.info("Da co: %d | Can crawl: %d", len(done), len(todo))

    if not todo:
        log.info("Khong con ID nao can crawl.")
        return {
            "total_papers": len(paper_ids),
            "already_done": len(done),
            "todo": 0,
            "success": 0,
            "not_found": 0,
            "error": 0,
            "elapsed_sec": 0.0,
        }

    success = not_found = error = 0
    with utils.measure("crawl_references", disk_path=o_path.parent) as perf:
        for i, arxiv_id in enumerate(todo, start=1):
            try:
                data = try_fetch_s2(arxiv_id, api_key=key)
            except Exception as exc:
                log.error("[%d/%d] %s -> loi lien tuc: %s", i, len(todo), arxiv_id, exc)
                record = error_record(arxiv_id, f"fetch_failed: {exc}")
                error += 1
            else:
                if data is None:
                    log.warning("[%d/%d] %s -> s2_not_found", i, len(todo), arxiv_id)
                    record = error_record(arxiv_id, "s2_not_found")
                    not_found += 1
                else:
                    refs = parse_refs(data.get("references") or [])
                    n_with_arxiv = sum(1 for r in refs if r["ref_arxiv_id"])
                    record = {
                        "arxiv_id": arxiv_id,
                        "s2_paper_id": data.get("paperId"),
                        "refs": refs,
                        "fetched_at": datetime.now(timezone.utc).isoformat(
                            timespec="seconds"
                        ),
                    }
                    log.info(
                        "[%d/%d] %s -> OK (%d refs, %d co arXiv ID)",
                        i, len(todo), arxiv_id, len(refs), n_with_arxiv,
                    )
                    success += 1

            utils.append_jsonl(o_path, record)

    log.info(
        "Hoan thanh. OK: %d | S2 not found: %d | Loi khac: %d | Thoi gian: %.1fs",
        success, not_found, error, perf.seconds,
    )

    return {
        "total_papers": len(paper_ids),
        "already_done": len(done),
        "todo": len(todo),
        "success": success,
        "not_found": not_found,
        "error": error,
        "elapsed_sec": round(perf.seconds, 2),
    }


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Crawl Semantic Scholar references -> data/references.jsonl"
    )
    ap.add_argument(
        "--papers",
        default=str(config.DATA_DIR / "papers.jsonl"),
        help="File papers.jsonl dau vao (mac dinh: data/papers.jsonl)",
    )
    ap.add_argument(
        "--out",
        default=str(config.DATA_DIR / "references.jsonl"),
        help="File JSONL dau ra (mac dinh: data/references.jsonl)",
    )
    ap.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Gioi han so paper can crawl trong lan chay nay (0 = tat ca)",
    )
    args = ap.parse_args()

    crawl_references(
        papers_path=args.papers,
        out_path=args.out,
        limit=args.limit,
    )


if __name__ == "__main__":
    main()
