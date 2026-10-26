"""Data Loader API cho metadata va references (papers.jsonl va references.jsonl).

Cung cap cac ham tien ich:
  - Lap qua tat ca paper hoac reference da crawl
  - Tra cuu mot paper/reference cu the theo arXiv ID
  - Load toan bo vao dict de truy xuat O(1)
  - Lay danh sach arXiv ID cua cac reference cua mot paper
  - Tinh toan cac thong ke tong hop
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator, Optional

from src import config, utils


# ---------------------------------------------------------------------------
# Paths mac dinh
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
    """Yield tung record paper tu papers.jsonl."""
    for record in utils.read_jsonl(path or _papers_path()):
        if skip_errors and "error" in record:
            continue
        yield record


def load_references(
    path: Optional[Path] = None,
    *,
    skip_errors: bool = True,
) -> Iterator[dict]:
    """Yield tung record reference tu references.jsonl."""
    for record in utils.read_jsonl(path or _refs_path()):
        if skip_errors and "error" in record:
            continue
        yield record


# ---------------------------------------------------------------------------
# Single-item lookup
# ---------------------------------------------------------------------------

def get_paper(
    arxiv_id: str,
    path: Optional[Path] = None,
) -> Optional[dict]:
    """Tra ve dict cua mot paper theo arXiv ID, hoac None neu khong tim thay."""
    target = utils.clean_arxiv_id(arxiv_id)
    for record in utils.read_jsonl(path or _papers_path()):
        if record.get("arxiv_id") == target:
            return record
    return None


def get_refs(
    arxiv_id: str,
    path: Optional[Path] = None,
) -> Optional[dict]:
    """Tra ve dict reference cua mot paper theo arXiv ID, hoac None."""
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
    """Yield cac arXiv ID (clean form) cua cac reference cua mot paper."""
    record = get_refs(arxiv_id, path)
    if record is None:
        return
    for ref in record.get("refs", []):
        ref_id = ref.get("ref_arxiv_id")
        if ref_id:
            yield ref_id


# ---------------------------------------------------------------------------
# In-memory index
# ---------------------------------------------------------------------------

def papers_index(path: Optional[Path] = None) -> dict[str, dict]:
    """Load toan bo papers.jsonl vao dict { arxiv_id -> paper_dict }."""
    return {r["arxiv_id"]: r for r in load_papers(path)}


def refs_index(path: Optional[Path] = None) -> dict[str, dict]:
    """Load toan bo references.jsonl vao dict { arxiv_id -> refs_dict }."""
    return {r["arxiv_id"]: r for r in load_references(path)}


# ---------------------------------------------------------------------------
# Summary statistics
# ---------------------------------------------------------------------------

def summarize(
    papers_path: Optional[Path] = None,
    refs_path: Optional[Path] = None,
) -> dict:
    """Tinh cac thong ke tong hop."""
    all_paper_records = list(utils.read_jsonl(papers_path or _papers_path()))
    n_ok = sum(1 for r in all_paper_records if "error" not in r)
    n_err = sum(1 for r in all_paper_records if "error" in r)

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
