"""Crawl Semantic Scholar references for papers in data/papers.jsonl -> data/references.jsonl.

Chay tu goc repo:
    python -m src.crawl_references
    python -m src.crawl_references --papers data/papers.jsonl --out data/references.jsonl
    python -m src.crawl_references --limit 20          # thu 20 paper dau

Dieu kien truoc: data/papers.jsonl phai ton tai (chay crawl_papers.py truoc).

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
from typing import Optional

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
# S2: 1 request/giay voi API key (da duoc dat trong config.S2_MIN_INTERVAL = 1.0)
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
# Neu chua dat, S2 van chap nhan nhung co the bi throttle hon.
_S2_HEADERS: dict[str, str] = {}
if config.S2_API_KEY:
    _S2_HEADERS["x-api-key"] = config.S2_API_KEY


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalize_ref_arxiv_id(raw_id: Optional[str]) -> Optional[str]:
    """Chuan hoa arXiv ID cua mot reference thanh dang "YYMM_NNNNN".

    - Xu ly ID kieu moi: "2310.12345" hoac "2310.12345v2" -> "2310_12345"
    - Xu ly ID kieu cu (truoc 2007): "hep-th/9901001" -> "hep-th_9901001"
    - Neu raw_id la None hoac khong khop -> tra ve None
    """
    if not raw_id:
        return None
    # ID kieu cu: "archive/YYMMNNN", vi du "hep-th/9901001"
    if re.match(r"^[a-zA-Z.\-]+/\d+$", raw_id):
        return utils.clean_arxiv_id(raw_id)
    # ID kieu moi: "2310.12345" hoac "2310.12345v2"
    new_style = re.match(r"^(\d{4}\.\d{4,5})(v\d+)?$", raw_id)
    if new_style:
        return utils.clean_arxiv_id(new_style.group(1))  # bo version suffix
    return None


def _parse_refs(raw_refs: list[dict]) -> list[dict]:
    """Chuyen danh sach reference tho tu S2 API thanh list theo schema."""
    refs = []
    for r in raw_refs:
        ext = r.get("externalIds") or {}
        refs.append({
            "ref_arxiv_id": _normalize_ref_arxiv_id(ext.get("ArXiv")),
            "title": r.get("title"),
            "authors": [a["name"] for a in (r.get("authors") or [])],
            "year": r.get("year"),
            "semantic_scholar_id": r.get("paperId"),
            "external_ids": ext,
        })
    return refs


def _error_record(arxiv_id: str, error: str) -> dict:
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
    # requests.HTTPError (la subclass cua RequestException) duoc raise boi
    # resp.raise_for_status() khi gap 429, 500, 503, ...
    # -> retry decorator tu dong cho BACKOFF_BASE ** attempt giay roi thu lai.
)
def _try_fetch_s2(arxiv_id_clean: str) -> Optional[dict]:
    """Goi S2 API, tra ve data dict hoac None neu 404.

    arxiv_id_clean: dang "2502_13235" (da clean).
    Ham nay doi sang dang "2502.13235" cho S2 API.
    Tra ve None neu S2 khong tim thay paper (404).
    Raise requests.HTTPError doi voi 429/5xx -> duoc retry boi decorator.
    """
    _s2_limiter.wait()  # dam bao >= 1 giay giua cac request S2

    canonical = arxiv_id_clean.replace("_", ".")  # S2 can dang co dau cham
    url = f"{_S2_BASE}/arXiv:{canonical}"

    resp = requests.get(
        url,
        params={"fields": _S2_FIELDS},
        headers=_S2_HEADERS,
        timeout=config.REQUEST_TIMEOUT,
    )

    if resp.status_code == 404:
        return None  # paper chua duoc S2 index

    resp.raise_for_status()  # raise HTTPError neu 429/5xx
    return resp.json()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

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

    papers_path = Path(args.papers)
    out_path = Path(args.out)

    if not config.S2_API_KEY:
        log.warning(
            "Bien moi truong S2_API_KEY chua duoc dat. "
            "Request se dung pool unauthenticated (de bi throttle hon)."
        )

    # --- Doc danh sach paper ID tu papers.jsonl (chi lay record khong co loi) ---
    paper_ids = [
        r["arxiv_id"]
        for r in utils.read_jsonl(papers_path)
        if "error" not in r and "arxiv_id" in r
    ]
    log.info("Tong paper trong papers.jsonl (thanh cong): %d", len(paper_ids))

    # --- Resume: bo qua ID da crawl thanh cong hoac s2_not_found ---
    # Cac record bi loi fetch_failed (do 429/mang truoc do) se duoc tu dong crawl lai
    done: set[str] = set()
    if out_path.exists():
        for r in utils.read_jsonl(out_path):
            pid = r.get("arxiv_id")
            err = str(r.get("error") or "")
            if pid and not err.startswith("fetch_failed"):
                done.add(pid)
    todo = [pid for pid in paper_ids if pid not in done]

    if args.limit > 0:
        todo = todo[: args.limit]

    log.info("Da co: %d | Can crawl: %d", len(done), len(todo))

    if not todo:
        log.info("Khong con ID nao can crawl. Thoat.")
        return

    # --- Crawl vong lap chinh ---
    success = not_found = error = 0
    with utils.measure("crawl_references", disk_path=out_path.parent) as perf:
        for i, arxiv_id in enumerate(todo, start=1):
            try:
                data = _try_fetch_s2(arxiv_id)
            except Exception as exc:
                log.error("[%d/%d] %s -> loi lien tuc: %s", i, len(todo), arxiv_id, exc)
                record = _error_record(arxiv_id, f"fetch_failed: {exc}")
                error += 1
            else:
                if data is None:
                    log.warning("[%d/%d] %s -> s2_not_found", i, len(todo), arxiv_id)
                    record = _error_record(arxiv_id, "s2_not_found")
                    not_found += 1
                else:
                    refs = _parse_refs(data.get("references") or [])
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

            utils.append_jsonl(out_path, record)

    log.info(
        "Hoan thanh. OK: %d | S2 not found: %d | Loi khac: %d | Thoi gian: %.1fs",
        success, not_found, error, perf.seconds,
    )


if __name__ == "__main__":
    main()
