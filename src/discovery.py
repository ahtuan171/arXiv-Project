"""Data Loader API cho TV4 -- doc papers.jsonl va references.jsonl.

Module nay cung cap cac ham tien ich de TV4 (phan matching/analysis) co the:
  - Lap qua tat ca paper hoac reference da crawl
  - Tra cuu mot paper/reference cu the theo arXiv ID
  - Load toan bo vao dict de truy xuat O(1)
  - Lay danh sach arXiv ID cua cac reference cua mot paper

Khong co dependency nao ngoai cac module co san trong repo (utils, config).

Cach dung:
    from src.discovery import load_papers, load_references, get_paper, get_refs

    # Lap qua tat ca paper
    for paper in load_papers():
        print(paper["arxiv_id"], paper["title"])

    # Tra cuu mot paper
    p = get_paper("2502.13235")
    p = get_paper("2502_13235")   # ca hai dang deu duoc

    # Lay arXiv ID cua cac reference cua mot paper
    for ref_id in iter_arxiv_ref_ids("2502.13235"):
        print(ref_id)  # e.g. "1706_03762"

    # Load toan bo vao bo nho de truy xuat nhanh
    index = papers_index()
    paper = index["2502_13235"]
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator, Optional

from src import config, utils


# ---------------------------------------------------------------------------
# Paths mac dinh (co the override bang argument)
# ---------------------------------------------------------------------------

def _papers_path() -> Path:
    return config.DATA_DIR / "papers.jsonl"


def _refs_path() -> Path:
    return config.DATA_DIR / "references.jsonl"


# ---------------------------------------------------------------------------
# Iterators
# ---------------------------------------------------------------------------

def load_papers(
    path: Optional[Path] = None,
    *,
    skip_errors: bool = True,
) -> Iterator[dict]:
    """Yield tung record paper tu papers.jsonl.

    Args:
        path: Duong dan toi papers.jsonl. Mac dinh: data/papers.jsonl.
        skip_errors: Neu True (mac dinh), bo qua record co truong "error"
                     (la cac paper khong tim thay hoac loi mang).

    Yields:
        dict theo Data Schema v1. Cac truong chinh:
            arxiv_id, title, authors, abstract, categories,
            submission_date, revised_dates, versions, doi, venue, comment, fetched_at
    """
    for record in utils.read_jsonl(path or _papers_path()):
        if skip_errors and "error" in record:
            continue
        yield record


def load_references(
    path: Optional[Path] = None,
    *,
    skip_errors: bool = True,
) -> Iterator[dict]:
    """Yield tung record reference tu references.jsonl.

    Args:
        path: Duong dan toi references.jsonl. Mac dinh: data/references.jsonl.
        skip_errors: Neu True (mac dinh), bo qua record co truong "error".

    Yields:
        dict voi cac truong: arxiv_id, s2_paper_id, refs (list), fetched_at.
        Moi phan tu trong refs co: ref_arxiv_id, title, authors, year,
                                    semantic_scholar_id, external_ids.
    """
    for record in utils.read_jsonl(path or _refs_path()):
        if skip_errors and "error" in record:
            continue
        yield record


# ---------------------------------------------------------------------------
# Single-item lookup (doc tuan tu, phu hop khi chi can 1 paper)
# ---------------------------------------------------------------------------

def get_paper(
    arxiv_id: str,
    path: Optional[Path] = None,
) -> Optional[dict]:
    """Tra ve dict cua mot paper theo arXiv ID, hoac None neu khong tim thay.

    Args:
        arxiv_id: Chap nhan ca dang "2502.13235" va dang clean "2502_13235".
        path: Duong dan toi papers.jsonl.

    Returns:
        Dict paper (Data Schema v1) hoac None.

    Note:
        Ham nay doc file tuan tu. Neu can tra cuu nhieu lan, dung papers_index().
    """
    target = utils.clean_arxiv_id(arxiv_id)
    for record in utils.read_jsonl(path or _papers_path()):
        if record.get("arxiv_id") == target:
            return record
    return None


def get_refs(
    arxiv_id: str,
    path: Optional[Path] = None,
) -> Optional[dict]:
    """Tra ve dict reference cua mot paper theo arXiv ID, hoac None.

    Args:
        arxiv_id: Chap nhan ca dang "2502.13235" va dang clean "2502_13235".
        path: Duong dan toi references.jsonl.

    Returns:
        Dict voi truong "refs" (list cac reference) hoac None neu khong tim thay.

    Note:
        Ham nay doc file tuan tu. Neu can tra cuu nhieu lan, dung refs_index().
    """
    target = utils.clean_arxiv_id(arxiv_id)
    for record in utils.read_jsonl(path or _refs_path()):
        if record.get("arxiv_id") == target:
            return record
    return None


# ---------------------------------------------------------------------------
# Convenience iterator
# ---------------------------------------------------------------------------

def iter_arxiv_ref_ids(
    arxiv_id: str,
    path: Optional[Path] = None,
) -> Iterator[str]:
    """Yield cac arXiv ID (clean form) cua cac reference cua mot paper.

    Chi yield cac reference CO arXiv ID (bo qua reference sach/tap chi khong co tren arXiv).

    Args:
        arxiv_id: Paper goc (dang "2502.13235" hoac "2502_13235").
        path: Duong dan toi references.jsonl.

    Yields:
        str: Clean arXiv ID, vi du "1706_03762".

    Vi du:
        for ref_id in iter_arxiv_ref_ids("2502.13235"):
            paper = get_paper(ref_id)
            if paper:
                print(paper["title"])
    """
    record = get_refs(arxiv_id, path)
    if record is None:
        return
    for ref in record.get("refs", []):
        ref_id = ref.get("ref_arxiv_id")
        if ref_id:
            yield ref_id


# ---------------------------------------------------------------------------
# In-memory index (dung khi can truy xuat O(1) nhieu lan)
# ---------------------------------------------------------------------------

def papers_index(path: Optional[Path] = None) -> dict[str, dict]:
    """Load toan bo papers.jsonl vao dict { arxiv_id -> paper_dict }.

    Canh bao: load toan bo file vao bo nho RAM.
    Chi dung khi can tra cuu ngau nhien nhieu lan (O(1) moi lan tra cuu).

    Args:
        path: Duong dan toi papers.jsonl.

    Returns:
        dict key la arxiv_id (clean form), value la paper dict.
    """
    return {r["arxiv_id"]: r for r in load_papers(path)}


def refs_index(path: Optional[Path] = None) -> dict[str, dict]:
    """Load toan bo references.jsonl vao dict { arxiv_id -> refs_dict }.

    Canh bao: load toan bo file vao bo nho RAM.

    Args:
        path: Duong dan toi references.jsonl.

    Returns:
        dict key la arxiv_id (clean form), value la refs dict (co truong "refs").
    """
    return {r["arxiv_id"]: r for r in load_references(path)}


# ---------------------------------------------------------------------------
# Summary statistics (tien ich cho bao cao)
# ---------------------------------------------------------------------------

def summarize(
    papers_path: Optional[Path] = None,
    refs_path: Optional[Path] = None,
) -> dict:
    """Tinh cac thong ke tong hop de ghi vao bao cao.

    Returns:
        dict voi cac truong:
            n_papers          : so paper crawl thanh cong
            n_papers_error    : so paper loi (not_found hoac fetch_failed)
            n_refs_records    : so record reference crawl thanh cong
            n_refs_total      : tong so reference (ke ca khong co arXiv ID)
            n_refs_with_arxiv : so reference co arXiv ID
            avg_refs_per_paper: trung binh so reference moi paper
    """
    # Papers
    all_paper_records = list(utils.read_jsonl(papers_path or _papers_path()))
    n_ok = sum(1 for r in all_paper_records if "error" not in r)
    n_err = sum(1 for r in all_paper_records if "error" in r)

    # References (neu file chua ton tai -> tra ve 0)
    refs_file = refs_path or _refs_path()
    if not refs_file.exists():
        return {
            "n_papers": n_ok,
            "n_papers_error": n_err,
            "n_refs_records": 0,
            "n_refs_total": 0,
            "n_refs_with_arxiv": 0,
            "avg_refs_per_paper": 0.0,
        }
    all_ref_records = list(utils.read_jsonl(refs_file))
    ok_ref_records = [r for r in all_ref_records if "error" not in r]
    n_refs_total = sum(len(r.get("refs", [])) for r in ok_ref_records)
    n_refs_with_arxiv = sum(
        sum(1 for ref in r.get("refs", []) if ref.get("ref_arxiv_id"))
        for r in ok_ref_records
    )
    avg_refs = n_refs_total / len(ok_ref_records) if ok_ref_records else 0.0

    return {
        "n_papers": n_ok,
        "n_papers_error": n_err,
        "n_refs_records": len(ok_ref_records),
        "n_refs_total": n_refs_total,
        "n_refs_with_arxiv": n_refs_with_arxiv,
        "avg_refs_per_paper": round(avg_refs, 2),
    }
