"""Feature engineering for Milestone 3 Reference Matching (including M2 hierarchy features)."""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

import numpy as np
import pandas as pd
from sklearn.feature_extraction.text import TfidfVectorizer

from src.matching_module.cleaning import (
    clean_title,
    extract_arxiv_id_from_text,
    extract_surnames,
    extract_year_int,
    title_tokens,
)

FEATURE_COLUMNS = [
    "arxiv_id_exact_match",
    "title_exact_norm",
    "title_jaccard",
    "title_char_ngram_cosine",
    "title_word_tfidf_cosine",
    "title_seq_ratio",
    "title_len_ratio",
    "title_in_ref_title",
    "author_surname_jaccard",
    "first_author_match",
    "author_count_diff",
    "year_exact_match",
    "year_abs_diff",
    "year_missing",
    "citation_count_in_paper",
    "cited_in_intro_or_related",
]

_CITE_KEY_RE = re.compile(r"\\cite[a-zA-Z*]*\s*(?:\[[^\]]*\]\s*)*\{([^}]+)\}")
_INTRO_RELATED_KEYWORDS = ("intro", "related", "background", "preliminar", "literature")


def extract_hierarchy_citation_features(
    elements_df: pd.DataFrame | None,
    hierarchy_df: pd.DataFrame | None,
    paper_ids: set[str],
) -> dict[tuple[str, str], tuple[int, int]]:
    """Extract per-(arxiv_id, citation_key) context features from Milestone 2 hierarchy.

    Returns a mapping ``(arxiv_id, citation_key) -> (citation_count, in_intro_or_related)``.
    """
    if elements_df is None or elements_df.empty:
        return {}

    sub_elem = elements_df[elements_df["arxiv_id"].astype(str).isin(paper_ids)]
    if sub_elem.empty:
        return {}

    # Identify heading elements whose title relates to Introduction / Related Work
    intro_heading_ids: set[str] = set()
    heading_rows = sub_elem[sub_elem["kind"].astype(str) == "heading"]
    for row in heading_rows.to_dict(orient="records"):
        title_l = str(row.get("content", "")).lower()
        if any(kw in title_l for kw in _INTRO_RELATED_KEYWORDS):
            intro_heading_ids.add(str(row["element_id"]))

    # Map element_id -> parent_id (one hop + two hops for paragraph -> heading)
    parent_map: dict[str, str] = {}
    if hierarchy_df is not None and not hierarchy_df.empty:
        sub_hier = hierarchy_df[hierarchy_df["arxiv_id"].astype(str).isin(paper_ids)]
        for row in sub_hier.to_dict(orient="records"):
            eid = str(row["element_id"])
            pid = str(row["parent_id"])
            if eid not in parent_map:
                parent_map[eid] = pid

    counts: dict[tuple[str, str], int] = {}
    in_intro: dict[tuple[str, str], int] = {}

    sent_rows = sub_elem[sub_elem["kind"].astype(str) == "sentence"]
    for row in sent_rows.to_dict(orient="records"):
        content = str(row.get("content", ""))
        if "\\cite" not in content:
            continue
        aid = str(row["arxiv_id"])
        eid = str(row["element_id"])
        p1 = parent_map.get(eid, "")
        p2 = parent_map.get(p1, "")
        is_intro = int(p1 in intro_heading_ids or p2 in intro_heading_ids)

        for m in _CITE_KEY_RE.finditer(content):
            for raw_key in m.group(1).split(","):
                ckey = raw_key.strip()
                if not ckey:
                    continue
                k = (aid, ckey)
                counts[k] = counts.get(k, 0) + 1
                if is_intro:
                    in_intro[k] = 1

    return {k: (cnt, in_intro.get(k, 0)) for k, cnt in counts.items()}


def _jaccard(a: list[str], b: list[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def build_candidate_pairs(
    bibtex_df: pd.DataFrame,
    references_df: pd.DataFrame,
    labels_df: pd.DataFrame | None = None,
    hierarchy_feats: dict[tuple[str, str], tuple[int, int]] | None = None,
) -> pd.DataFrame:
    """Build the m x n candidate pair DataFrame with all engineered features for selected papers."""
    h_feats = hierarchy_feats or {}

    if labels_df is not None:
        target_papers = set(labels_df["arxiv_id"].astype(str))
        gt_map = {
            (str(r["arxiv_id"]), str(r["citation_key"])): r["groundtruth_arxiv_id"]
            for r in labels_df.to_dict(orient="records")
        }
        pool_map = {
            str(r["arxiv_id"]): str(r.get("pool", "unknown"))
            for r in labels_df.to_dict(orient="records")
        }
    else:
        target_papers = set(bibtex_df["arxiv_id"].astype(str)) & set(references_df["arxiv_id"].astype(str))
        gt_map = {}
        pool_map = {}

    sub_bib = bibtex_df[bibtex_df["arxiv_id"].astype(str).isin(target_papers)].copy()
    sub_ref = references_df[references_df["arxiv_id"].astype(str).isin(target_papers)].copy()

    ref_groups: dict[str, list[dict[str, Any]]] = {}
    for row in sub_ref.to_dict(orient="records"):
        aid = str(row["arxiv_id"])
        ref_groups.setdefault(aid, []).append({
            "ref_arxiv_id": str(row["ref_arxiv_id"]),
            "clean_title": clean_title(row.get("ref_title")),
            "tokens": title_tokens(row.get("ref_title")),
            "surnames": extract_surnames(row.get("ref_authors")),
            "year": extract_year_int(row.get("ref_submission_date")),
        })

    bib_groups: dict[str, list[dict[str, Any]]] = {}
    for row in sub_bib.to_dict(orient="records"):
        aid = str(row["arxiv_id"])
        bib_groups.setdefault(aid, []).append({
            "citation_key": str(row["citation_key"]),
            "bib_arxiv_id": extract_arxiv_id_from_text(row.get("url"), row.get("doi"), row.get("journal")),
            "clean_title": clean_title(row.get("title")),
            "tokens": title_tokens(row.get("title")),
            "surnames": extract_surnames(row.get("authors")),
            "year": extract_year_int(row.get("year")),
        })

    pair_rows: list[dict[str, Any]] = []
    char_vec = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 4), min_df=1)
    word_vec = TfidfVectorizer(analyzer="word", ngram_range=(1, 2), min_df=1)

    for aid, bibs in bib_groups.items():
        refs = ref_groups.get(aid, [])
        if not refs:
            continue

        bib_titles = [b["clean_title"] or " " for b in bibs]
        ref_titles = [r["clean_title"] or " " for r in refs]
        all_titles = bib_titles + ref_titles

        try:
            c_mat = char_vec.fit_transform(all_titles)
            c_sim = (c_mat[: len(bibs)] @ c_mat[len(bibs) :].T).toarray()
        except ValueError:
            c_sim = np.zeros((len(bibs), len(refs)), dtype=float)

        try:
            w_mat = word_vec.fit_transform(all_titles)
            w_sim = (w_mat[: len(bibs)] @ w_mat[len(bibs) :].T).toarray()
        except ValueError:
            w_sim = np.zeros((len(bibs), len(refs)), dtype=float)

        for i, b in enumerate(bibs):
            ckey = b["citation_key"]
            gt_id = gt_map.get((aid, ckey))
            cite_cnt, in_intro = h_feats.get((aid, ckey), (0, 0))
            b_title = b["clean_title"]
            b_toks = b["tokens"]
            b_surnames = b["surnames"]
            b_year = b["year"]
            b_aid = b["bib_arxiv_id"]

            for j, r in enumerate(refs):
                r_id = r["ref_arxiv_id"]
                r_title = r["clean_title"]
                r_toks = r["tokens"]
                r_surnames = r["surnames"]
                r_year = r["year"]

                len_b, len_r = len(b_title), len(r_title)
                len_ratio = (min(len_b, len_r) / max(len_b, len_r)) if max(len_b, len_r) > 0 else 0.0
                c_val = float(c_sim[i, j])
                if b_title and r_title and (c_val >= 0.12 or len_ratio >= 0.5):
                    sm = SequenceMatcher(None, b_title, r_title)
                    seq_ratio = sm.ratio() if sm.quick_ratio() >= 0.25 else sm.quick_ratio()
                else:
                    seq_ratio = 0.0
                in_ref = int(bool(b_title and r_title and len_b >= 10 and (b_title in r_title or r_title in b_title)))

                first_auth = int(bool(b_surnames and r_surnames and b_surnames[0] == r_surnames[0]))
                auth_jac = _jaccard(b_surnames, r_surnames)
                auth_diff = abs(len(b_surnames) - len(r_surnames))

                if b_year is not None and r_year is not None:
                    year_exact = int(b_year == r_year)
                    year_diff = float(abs(b_year - r_year))
                    year_miss = 0
                else:
                    year_exact = 0
                    year_diff = 10.0
                    year_miss = 1

                pair_rows.append({
                    "arxiv_id": aid,
                    "citation_key": ckey,
                    "ref_arxiv_id": r_id,
                    "groundtruth_arxiv_id": gt_id,
                    "pool": pool_map.get(aid, "unknown"),
                    "label": int(gt_id is not None and r_id == gt_id),
                    "arxiv_id_exact_match": int(b_aid is not None and b_aid == r_id),
                    "title_exact_norm": int(bool(b_title and r_title and b_title == r_title)),
                    "title_jaccard": _jaccard(b_toks, r_toks),
                    "title_char_ngram_cosine": float(c_sim[i, j]),
                    "title_word_tfidf_cosine": float(w_sim[i, j]),
                    "title_seq_ratio": float(seq_ratio),
                    "title_len_ratio": float(len_ratio),
                    "title_in_ref_title": in_ref,
                    "author_surname_jaccard": float(auth_jac),
                    "first_author_match": first_auth,
                    "author_count_diff": int(auth_diff),
                    "year_exact_match": year_exact,
                    "year_abs_diff": year_diff,
                    "year_missing": year_miss,
                    "citation_count_in_paper": int(cite_cnt),
                    "cited_in_intro_or_related": int(in_intro),
                })

    return pd.DataFrame(pair_rows)
