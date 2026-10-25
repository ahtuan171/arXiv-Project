"""Unit tests for src/metadata_module."""

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock
import pytest

from src.metadata_module import (
    build_record,
    error_record,
    extract_version_count,
    generate_id_list,
    get_paper,
    get_refs,
    ids_in_range,
    iter_arxiv_ref_ids,
    load_papers,
    load_references,
    normalize_ref_arxiv_id,
    papers_index,
    parse_refs,
    refs_index,
    summarize,
    yymm,
)


def test_make_id_list_range():
    # Kiem tra sinh dai 1 thang
    ids = ids_in_range("2025-02", 13235, "2025-02", 18234)
    assert len(ids) == 5000
    assert ids[0] == "2502.13235"
    assert ids[-1] == "2502.18234"

    # Kiem tra sinh dai vat qua 2 thang (phan 5)
    ids_cross = ids_in_range("2025-01", 17642, "2025-02", 3234)
    assert len(ids_cross) == 5000
    assert ids_cross[0] == "2501.17642"
    assert ids_cross[-1] == "2502.03234"


def test_generate_id_list(tmp_path: Path):
    out_file = tmp_path / "id_list.txt"
    ids = generate_id_list(part=1, out_path=out_file)
    assert len(ids) == 5000
    assert out_file.exists()
    saved_lines = out_file.read_text(encoding="utf-8").splitlines()
    assert len(saved_lines) == 5000
    assert saved_lines[0] == "2502.13235"


def test_extract_version_count():
    assert extract_version_count("http://arxiv.org/abs/2412.03848v3") == 3
    assert extract_version_count("http://arxiv.org/abs/2412.03848v1") == 1
    assert extract_version_count("http://arxiv.org/abs/2412.03848") == 1


def test_build_record_schema():
    mock_paper = MagicMock()
    mock_paper.entry_id = "http://arxiv.org/abs/2502.13235v2"
    mock_paper.title = "Sample Research Paper Title"
    author_a = MagicMock()
    author_a.name = "Nguyen Van A"
    author_b = MagicMock()
    author_b.name = "Tran Van B"
    mock_paper.authors = [author_a, author_b]
    mock_paper.summary = "Paragraph 1\nParagraph 2"
    mock_paper.categories = ["cs.AI", "cs.LG"]
    mock_paper.primary_category = "cs.AI"
    mock_paper.published = datetime(2025, 2, 18, 12, 0, tzinfo=timezone.utc)
    mock_paper.updated = datetime(2025, 2, 20, 15, 0, tzinfo=timezone.utc)
    mock_paper.doi = "10.1000/182"
    mock_paper.journal_ref = "Nature CS"
    mock_paper.comment = "10 pages, 3 figures"

    record = build_record(mock_paper, "2502.13235")

    expected_fields = [
        "arxiv_id",
        "title",
        "authors",
        "abstract",
        "categories",
        "primary_category",
        "submission_date",
        "revised_dates",
        "versions",
        "doi",
        "venue",
        "comment",
        "fetched_at",
    ]
    for field in expected_fields:
        assert field in record

    assert record["arxiv_id"] == "2502_13235"
    assert record["title"] == "Sample Research Paper Title"
    assert record["authors"] == ["Nguyen Van A", "Tran Van B"]
    assert record["abstract"] == "Paragraph 1 Paragraph 2"
    assert record["submission_date"] == "2025-02-18"
    assert record["revised_dates"] == ["2025-02-20"]
    assert record["versions"] == ["v1", "v2"]
    assert record["doi"] == "10.1000/182"


def test_normalize_ref_arxiv_id():
    assert normalize_ref_arxiv_id("2310.12345") == "2310_12345"
    assert normalize_ref_arxiv_id("2310.12345v2") == "2310_12345"
    assert normalize_ref_arxiv_id("hep-th/9901001") == "hep-th_9901001"
    assert normalize_ref_arxiv_id(None) is None
    assert normalize_ref_arxiv_id("invalid-arxiv-id") is None


def test_parse_refs():
    raw_refs = [
        {
            "paperId": "s2_id_1",
            "title": "Attention Is All You Need",
            "authors": [{"name": "Vaswani"}],
            "year": 2017,
            "externalIds": {"ArXiv": "1706.03762v5", "DOI": "10.1145/test"},
        },
        {
            "paperId": "s2_id_2",
            "title": "A Book on AI",
            "authors": [{"name": "Russell"}],
            "year": 2020,
            "externalIds": {"ISBN": "1234567890"},
        },
    ]

    refs = parse_refs(raw_refs)
    assert len(refs) == 2
    assert refs[0]["ref_arxiv_id"] == "1706_03762"
    assert refs[0]["semantic_scholar_id"] == "s2_id_1"
    assert refs[0]["authors"] == ["Vaswani"]
    assert refs[1]["ref_arxiv_id"] is None


def test_discovery_and_summarize(tmp_path: Path):
    from src import utils

    papers_file = tmp_path / "papers.jsonl"
    refs_file = tmp_path / "references.jsonl"

    p1 = {
        "arxiv_id": "2502_13235",
        "title": "Paper One",
        "authors": ["Author 1"],
        "abstract": "Abstract 1",
        "categories": ["cs.AI"],
        "primary_category": "cs.AI",
        "submission_date": "2025-02-18",
        "revised_dates": [],
        "versions": ["v1"],
        "doi": None,
        "venue": None,
        "comment": None,
        "fetched_at": "2025-02-18T10:00:00Z",
    }
    p_err = {
        "arxiv_id": "2502_99999",
        "error": "not_found",
        "fetched_at": "2025-02-18T10:00:00Z",
    }
    utils.write_jsonl(papers_file, [p1, p_err])

    r1 = {
        "arxiv_id": "2502_13235",
        "s2_paper_id": "s2_root",
        "refs": [
            {
                "ref_arxiv_id": "1706_03762",
                "title": "Transformer",
                "authors": ["Vaswani"],
                "year": 2017,
                "semantic_scholar_id": "s2_ref_1",
                "external_ids": {"ArXiv": "1706.03762"},
            }
        ],
        "fetched_at": "2025-02-18T10:00:00Z",
    }
    utils.write_jsonl(refs_file, [r1])

    # Test load_papers & skip_errors
    papers = list(load_papers(papers_file, skip_errors=True))
    assert len(papers) == 1
    assert papers[0]["arxiv_id"] == "2502_13235"

    # Test get_paper
    assert get_paper("2502.13235", papers_file)["title"] == "Paper One"
    assert get_paper("2502_13235", papers_file)["title"] == "Paper One"
    assert get_paper("non_existent", papers_file) is None

    # Test get_refs & iter_arxiv_ref_ids
    assert get_refs("2502.13235", refs_file)["s2_paper_id"] == "s2_root"
    ref_ids = list(iter_arxiv_ref_ids("2502.13235", refs_file))
    assert ref_ids == ["1706_03762"]

    # Test index functions
    p_idx = papers_index(papers_file)
    assert "2502_13235" in p_idx
    r_idx = refs_index(refs_file)
    assert "2502_13235" in r_idx

    # Test summarize
    summary = summarize(papers_file, refs_file)
    assert summary["n_papers"] == 1
    assert summary["n_papers_error"] == 1
    assert summary["n_refs_records"] == 1
    assert summary["n_refs_total"] == 1
    assert summary["n_refs_with_arxiv"] == 1
    assert summary["avg_refs_per_paper"] == 1.0
