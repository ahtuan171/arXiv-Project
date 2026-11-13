"""Direct unit tests for :func:`src.deduplicator.deduplicate_paper`.

Run directly with::

    python test_dedup.py
"""

import pandas as pd

from src.deduplicator import deduplicate_paper


def build_mock_data() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Create two-version in-memory input data for one arXiv paper."""

    raw_elements = pd.DataFrame(
        [
            {
                "element_id": "v1_e1",
                "arxiv_id": "2501.07642",
                "version": "v1",
                "kind": "section",
                "content": "Introduction",
            },
            {
                "element_id": "v1_e2",
                "arxiv_id": "2501.07642",
                "version": "v1",
                "kind": "paragraph",
                "content": "This is the introduction.",
            },
            {
                "element_id": "v2_e1",
                "arxiv_id": "2501.07642",
                "version": "v2",
                "kind": "section",
                "content": "Introduction",
            },
            {
                "element_id": "v2_e2",
                "arxiv_id": "2501.07642",
                "version": "v2",
                "kind": "paragraph",
                "content": "This is the introduction.",
            },
            {
                "element_id": "v2_e3",
                "arxiv_id": "2501.07642",
                "version": "v2",
                "kind": "paragraph",
                "content": "This paragraph was added in version two.",
            },
        ]
    )

    raw_hierarchy = pd.DataFrame(
        [
            {
                "arxiv_id": "2501.07642",
                "version": "v1",
                "element_id": "v1_e1",
                "parent_id": "",
            },
            {
                "arxiv_id": "2501.07642",
                "version": "v1",
                "element_id": "v1_e2",
                "parent_id": "v1_e1",
            },
            {
                "arxiv_id": "2501.07642",
                "version": "v2",
                "element_id": "v2_e1",
                "parent_id": "",
            },
            {
                "arxiv_id": "2501.07642",
                "version": "v2",
                "element_id": "v2_e2",
                "parent_id": "v2_e1",
            },
            {
                "arxiv_id": "2501.07642",
                "version": "v2",
                "element_id": "v2_e3",
                "parent_id": "v2_e1",
            },
        ]
    )

    raw_bibtex = pd.DataFrame(
        [
            {
                "arxiv_id": "2501.07642",
                "version": "v1",
                "citation_key": "attention2017",
                "entry_type": "article",
                "title": "Attention Is All You Need",
                "authors": ["Ashish Vaswani", "Noam Shazeer"],
                "year": None,
                "doi": "10.48550/arXiv.1706.03762",
                "journal": "",
                "url": "",
            },
            {
                "arxiv_id": "2501.07642",
                "version": "v2",
                "citation_key": "attention2017",
                "entry_type": "article",
                "title": "Attention Is All You Need",
                "authors": ["Ashish Vaswani", "Noam Shazeer"],
                "year": 2017,
                "doi": "",
                "journal": "NeurIPS",
                "url": "https://arxiv.org/abs/1706.03762",
            },
        ]
    )

    return raw_elements, raw_hierarchy, raw_bibtex


def run_tests() -> None:
    """Run all direct assertions and print a PASS message per requirement."""

    raw_elements, raw_hierarchy, raw_bibtex = build_mock_data()
    elements_clean, hierarchy_clean, bibtex_clean, stats = deduplicate_paper(
        "2501.07642",
        raw_elements,
        raw_hierarchy,
        raw_bibtex,
    )

    assert len(elements_clean) < len(raw_elements)
    assert len(elements_clean) == 3
    print("PASS - Element text deduplication")

    v2_root = hierarchy_clean[
        (hierarchy_clean["version"] == "v2")
        & (hierarchy_clean["parent_id"] == "")
    ].iloc[0]
    v1_root = hierarchy_clean[
        (hierarchy_clean["version"] == "v1")
        & (hierarchy_clean["parent_id"] == "")
    ].iloc[0]
    assert v2_root["element_id"] == v1_root["element_id"]

    v2_children = hierarchy_clean[
        (hierarchy_clean["version"] == "v2")
        & (hierarchy_clean["parent_id"] == v1_root["element_id"])
    ]
    assert len(v2_children) == 2
    print("PASS - Hierarchy parent-child preservation")

    assert len(bibtex_clean) == 1
    citation = bibtex_clean.iloc[0]
    assert citation["citation_key"] == "attention2017"
    assert citation["year"] == 2017
    assert citation["doi"] == "10.48550/arXiv.1706.03762"
    print("PASS - BibTeX citation merge")

    assert stats["dedup_status"] == "success"
    assert stats["dedup_elements_count"] == len(elements_clean)
    print("PASS - Statistics calculation")

    print("PASS - All deduplicate_paper tests")


if __name__ == "__main__":
    run_tests()
