"""Crawl arXiv metadata for all IDs in data/id_list.txt -> data/papers.jsonl.

Chay tu goc repo:
    python -m src.crawl_papers                          # dung data/id_list.txt mac dinh
    python -m src.crawl_papers --limit 10               # thu 10 paper dau
    python -m src.crawl_papers --id-list data/id_list.txt --out data/papers.jsonl

Output (Data Schema v1) -- moi dong JSONL la mot dict:
    arxiv_id        : str   "2502_13235"   (dau . -> _ theo clean_arxiv_id)
    title           : str
    authors         : list[str]
    abstract        : str   (newlines ben trong duoc gop lai thanh khoang trang)
    categories      : list[str]             e.g. ["cs.CL", "cs.AI"]
    primary_category: str | null
    submission_date : str   "2025-02-18"   (ngay nop v1, ISO-8601)
    revised_dates   : list[str]            (ngay sua cuoi cung; rong neu chi co v1)
    versions        : list[str]            ["v1"] hoac ["v1","v2",...] -- CV2/CV3 dung de biet can tai gi
    doi             : str | null
    venue           : str | null           (journal_ref tu arXiv)
    comment         : str | null           (comment field tu arXiv)
    fetched_at      : str   ISO-8601 UTC timestamp luc crawl

Neu paper khong ton tai tren arXiv, record co them truong "error": "not_found"
(khong bi bo qua -- van duoc ghi de dam bao resume khong hoi lai).

Resume: script kiem tra data/papers.jsonl da co -> bo qua ID da crawl -> an toan khi chay lai.
"""

from __future__ import annotations

import argparse
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import arxiv

from src import config, utils

# ---------------------------------------------------------------------------
# Logger (ghi ra stderr + file logs/crawl_papers.log)
# ---------------------------------------------------------------------------
log = utils.setup_logging(
    "crawl_papers",
    log_file=config.LOG_DIR / "crawl_papers.log",
)

# ---------------------------------------------------------------------------
# Rate limiter va arXiv client
# ---------------------------------------------------------------------------
# arXiv yeu cau >= 3 giay giua cac request (xem crawling.md).
_arxiv_limiter = utils.RateLimiter(config.ARXIV_MIN_INTERVAL)

# delay_seconds=0: ta tu quan ly rate limiting qua _arxiv_limiter.
# num_retries=0:   ta dung decorator @retry ben duoi thay cho retry noi bo.
_client = arxiv.Client(delay_seconds=0.0, num_retries=0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_version_count(entry_id: str) -> int:
    """Lay so version tu entry_id cua arXiv.

    entry_id co dang "http://arxiv.org/abs/2412.03848v3" -> tra ve 3.
    Neu khong khop (hiem) -> tra ve 1.
    """
    match = re.search(r"v(\d+)$", entry_id.strip())
    return int(match.group(1)) if match else 1


def _build_record(paper: arxiv.Result, original_id: str) -> dict:
    """Chuyen arxiv.Result thanh dict theo Data Schema v1."""
    n_versions = _extract_version_count(paper.entry_id)

    # submission_date = ngay nop lan dau (v1)
    submission_date = paper.published.date().isoformat() if paper.published else None

    # revised_dates: arXiv API chi tra ve ngay UPDATE cuoi cung (latest version),
    # khong tra ve ngay cua tung version trung gian.
    # -> Neu co nhieu hon 1 version, ghi ngay updated (= ngay version moi nhat).
    if n_versions > 1 and paper.updated:
        revised_dates: list[str] = [paper.updated.date().isoformat()]
    else:
        revised_dates = []

    return {
        "arxiv_id": utils.clean_arxiv_id(original_id),
        "title": paper.title,
        "authors": [a.name for a in paper.authors],
        "abstract": paper.summary.replace("\n", " ") if paper.summary else "",
        "categories": list(paper.categories),
        # primary_category: str truc tiep trong arxiv>=2.x,
        # hoac object co .id trong mot so phien ban cu hon.
        "primary_category": (
            paper.primary_category.id
            if hasattr(paper.primary_category, "id")
            else (paper.primary_category or None)
        ),
        "submission_date": submission_date,
        "revised_dates": revised_dates,
        # versions: CV2/CV3 dung list nay de biet can tai bao nhieu version source
        "versions": [f"v{i}" for i in range(1, n_versions + 1)],
        "doi": paper.doi,
        "venue": paper.journal_ref,
        "comment": paper.comment,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def _error_record(arxiv_id_clean: str, error: str) -> dict:
    """Tao record loi de ghi vao JSONL (tranh hoi lai khi resume)."""
    return {
        "arxiv_id": arxiv_id_clean,
        "error": error,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


# ---------------------------------------------------------------------------
# Fetch function (voi retry cho loi mang)
# ---------------------------------------------------------------------------

@utils.retry(
    max_attempts=config.MAX_RETRIES,
    backoff=config.BACKOFF_BASE,
    exceptions=(Exception,),   # retry moi loi network/timeout
)
def _try_fetch_arxiv(canonical_id: str) -> Optional[arxiv.Result]:
    """Goi arXiv API, tra ve Result hoac None neu khong tim thay.

    canonical_id: dang "2502.13235" (co dau cham, khong co version suffix).
    Ham nay duoc boc boi @retry -> tu dong thu lai khi gap loi mang.
    Neu paper khong ton tai -> tra ve None (khong raise, khong retry).
    """
    _arxiv_limiter.wait()   # dam bao >= 3 giay giua cac request
    search = arxiv.Search(id_list=[canonical_id])
    # next(..., None): tra ve None thay vi raise StopIteration neu khong tim thay
    return next(_client.results(search), None)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Crawl arXiv paper metadata -> data/papers.jsonl"
    )
    ap.add_argument(
        "--id-list",
        default=str(config.DATA_DIR / "id_list.txt"),
        help="File chua danh sach arXiv ID (mac dinh: data/id_list.txt)",
    )
    ap.add_argument(
        "--out",
        default=str(config.DATA_DIR / "papers.jsonl"),
        help="File JSONL dau ra (mac dinh: data/papers.jsonl)",
    )
    ap.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Gioi han so paper can crawl trong lan chay nay (0 = tat ca)",
    )
    args = ap.parse_args()

    id_list_path = Path(args.id_list)
    out_path = Path(args.out)

    # --- Doc danh sach ID tu file ---
    raw_ids = [
        line.strip()
        for line in id_list_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    # --- Resume: bo qua ID da co trong output ---
    # load_done_ids doc truong "arxiv_id" tu JSONL (ca record thanh cong lan loi)
    done: set[str] = utils.load_done_ids(out_path, key="arxiv_id")

    # Chuan hoa de so sanh: "2502.13235" -> "2502_13235"
    todo = [rid for rid in raw_ids if utils.clean_arxiv_id(rid) not in done]

    if args.limit > 0:
        todo = todo[: args.limit]

    log.info(
        "Tong ID: %d | Da co: %d | Can crawl: %d",
        len(raw_ids), len(done), len(todo),
    )

    if not todo:
        log.info("Khong con ID nao can crawl. Thoat.")
        return

    # --- Crawl vong lap chinh ---
    success = error = 0
    with utils.measure("crawl_papers", disk_path=out_path.parent) as perf:
        for i, raw_id in enumerate(todo, start=1):
            # canonical_id: dang arXiv dung cho API, vi du "2502.13235"
            canonical_id = raw_id  # id_list.txt da o dang YYMM.NNNNN

            try:
                paper = _try_fetch_arxiv(canonical_id)
            except Exception as exc:
                # Sau MAX_RETRIES lan van loi -> ghi error record
                log.error("[%d/%d] %s -> loi lien tuc: %s", i, len(todo), raw_id, exc)
                record = _error_record(
                    utils.clean_arxiv_id(canonical_id), f"fetch_failed: {exc}"
                )
                error += 1
            else:
                if paper is None:
                    log.warning("[%d/%d] %s -> not_found", i, len(todo), raw_id)
                    record = _error_record(utils.clean_arxiv_id(canonical_id), "not_found")
                    error += 1
                else:
                    record = _build_record(paper, canonical_id)
                    n_ver = len(record["versions"])
                    log.info(
                        "[%d/%d] %s -> OK (%d version(s)): %s",
                        i, len(todo), raw_id, n_ver, record["title"][:60],
                    )
                    success += 1

            utils.append_jsonl(out_path, record)

    log.info(
        "Hoan thanh. Thanh cong: %d | Loi: %d | Thoi gian: %.1fs",
        success, error, perf.seconds,
    )


if __name__ == "__main__":
    main()
