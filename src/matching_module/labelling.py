"""Manual and heuristic automatic labelling for Milestone 3 Reference Matching."""
from __future__ import annotations

import math
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.matching_module.cleaning import (
    clean_title,
    extract_arxiv_id_from_text,
    extract_surnames,
    extract_year_int,
    title_tokens,
)


def _jaccard(a: list[str], b: list[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _heuristic_match_entry(
    bib_row: dict[str, Any],
    ref_records: list[dict[str, Any]],
) -> str | None:
    """Classic heuristic matcher: returns matched ref_arxiv_id or None."""
    if not ref_records:
        return None

    # 1. Exact arXiv ID in URL / DOI / journal
    bib_aid = extract_arxiv_id_from_text(
        bib_row.get("url"), bib_row.get("doi"), bib_row.get("journal")
    )
    if bib_aid:
        for ref in ref_records:
            if ref["ref_arxiv_id"] == bib_aid:
                return bib_aid

    b_title = clean_title(bib_row.get("title"))
    b_toks = title_tokens(bib_row.get("title"))
    b_surnames = extract_surnames(bib_row.get("authors"))
    b_year = extract_year_int(bib_row.get("year"))

    best_id: str | None = None
    best_score = 0.0

    for ref in ref_records:
        r_title = ref["clean_title"]
        r_toks = ref["title_tokens"]
        r_surnames = ref["surnames"]
        r_year = ref["year"]

        if not b_title or not r_title:
            continue

        # 2. Exact normalized title match
        if b_title == r_title and len(b_title) >= 8:
            return ref["ref_arxiv_id"]

        jac = _jaccard(b_toks, r_toks)
        auth_jac = _jaccard(b_surnames, r_surnames)
        if jac < 0.25 and auth_jac < 0.25:
            continue
        seq = SequenceMatcher(None, b_title, r_title).ratio()
        year_ok = (b_year is None or r_year is None or abs(b_year - r_year) <= 2)

        score = 0.65 * max(jac, seq) + 0.35 * auth_jac
        # High-precision acceptance rules
        if max(jac, seq) >= 0.86 and (auth_jac > 0 or not b_surnames or not r_surnames) and year_ok:
            if score > best_score:
                best_score = score
                best_id = ref["ref_arxiv_id"]
        elif seq >= 0.72 and auth_jac >= 0.5 and year_ok:
            if score > best_score:
                best_score = score
                best_id = ref["ref_arxiv_id"]

    return best_id


def _prepare_ref_index(references_df: pd.DataFrame) -> dict[str, list[dict[str, Any]]]:
    """Pre-index cleaned reference records by citing arxiv_id."""
    index: dict[str, list[dict[str, Any]]] = {}
    for row in references_df.to_dict(orient="records"):
        aid = str(row["arxiv_id"])
        rec = {
            "ref_arxiv_id": str(row["ref_arxiv_id"]),
            "clean_title": clean_title(row.get("ref_title")),
            "title_tokens": title_tokens(row.get("ref_title")),
            "surnames": extract_surnames(row.get("ref_authors")),
            "year": extract_year_int(row.get("ref_submission_date")),
        }
        index.setdefault(aid, []).append(rec)
    return index


def build_manual_seed_labels(
    bibtex_df: pd.DataFrame,
    references_df: pd.DataFrame,
    *,
    min_papers: int = 5,
    min_entries_per_paper: int = 20,
    seed: int = 42,
) -> pd.DataFrame:
    """Select >=5 papers (each >=20 BibTeX entries) and create initial manual ground truth."""
    bib_counts = bibtex_df.groupby("arxiv_id").size()
    ref_counts = references_df.groupby("arxiv_id").size()

    eligible = sorted(
        aid for aid in bib_counts.index
        if bib_counts[aid] >= min_entries_per_paper and ref_counts.get(aid, 0) >= 10
    )
    if len(eligible) < min_papers:
        raise ValueError(
            f"Not enough eligible papers for manual pool: found {len(eligible)}, need {min_papers}."
        )

    # Prefer papers with 25-60 entries so manual verification is tractable
    preferred = [aid for aid in eligible if 25 <= bib_counts[aid] <= 60]
    pool_candidates = preferred if len(preferred) >= min_papers else eligible

    rng = np.random.default_rng(seed)
    chosen_indices = rng.choice(len(pool_candidates), size=min_papers, replace=False)
    selected_papers = sorted(pool_candidates[i] for i in chosen_indices)

    sub_ref = references_df[references_df["arxiv_id"].astype(str).isin(selected_papers)]
    ref_index = _prepare_ref_index(sub_ref)

    rows: list[dict[str, Any]] = []
    sub_bib = bibtex_df[bibtex_df["arxiv_id"].isin(selected_papers)]
    for row in sub_bib.to_dict(orient="records"):
        aid = str(row["arxiv_id"])
        gt = _heuristic_match_entry(row, ref_index.get(aid, []))
        rows.append({
            "arxiv_id": aid,
            "citation_key": str(row["citation_key"]),
            "groundtruth_arxiv_id": gt,
            "pool": "manual",
        })
    return pd.DataFrame(rows)


def load_manual_labels(path: Path) -> pd.DataFrame:
    """Load curated manual labels from CSV (arxiv_id, citation_key, groundtruth_arxiv_id)."""
    df = pd.read_csv(path, dtype={"arxiv_id": str, "citation_key": str, "groundtruth_arxiv_id": str})
    df["groundtruth_arxiv_id"] = df["groundtruth_arxiv_id"].where(
        df["groundtruth_arxiv_id"].notna() & (df["groundtruth_arxiv_id"].str.strip() != ""),
        None,
    )
    df["pool"] = "manual"
    return df[["arxiv_id", "citation_key", "groundtruth_arxiv_id", "pool"]]


def auto_label_publications(
    bibtex_df: pd.DataFrame,
    references_df: pd.DataFrame,
    *,
    exclude_papers: set[str],
    fraction: float = 0.12,
    seed: int = 42,
) -> pd.DataFrame:
    """Automatically label >=10% of non-manual publications using classic heuristics."""
    if fraction < 0.10:
        raise ValueError("Automatic labelling fraction must be at least 0.10 (10%).")

    bib_papers = set(bibtex_df["arxiv_id"].astype(str))
    ref_papers = set(references_df["arxiv_id"].astype(str))
    eligible = sorted((bib_papers & ref_papers) - exclude_papers)

    target_count = max(2, math.ceil(len(eligible) * fraction))
    rng = np.random.default_rng(seed)
    chosen_idx = rng.choice(len(eligible), size=min(target_count, len(eligible)), replace=False)
    selected_papers = sorted(eligible[i] for i in chosen_idx)

    sub_ref = references_df[references_df["arxiv_id"].astype(str).isin(selected_papers)]
    ref_index = _prepare_ref_index(sub_ref)

    rows: list[dict[str, Any]] = []
    sub_bib = bibtex_df[bibtex_df["arxiv_id"].isin(selected_papers)]
    for row in sub_bib.to_dict(orient="records"):
        aid = str(row["arxiv_id"])
        gt = _heuristic_match_entry(row, ref_index.get(aid, []))
        rows.append({
            "arxiv_id": aid,
            "citation_key": str(row["citation_key"]),
            "groundtruth_arxiv_id": gt,
            "pool": "auto",
        })
    return pd.DataFrame(rows)


def validate_labelling_requirements(
    labels_df: pd.DataFrame,
    total_non_manual_eligible: int,
) -> dict[str, Any]:
    """Verify Milestone 3 labelling constraints (>=5 manual papers x >=20 entries; >=10% auto)."""
    manual = labels_df[labels_df["pool"] == "manual"]
    auto = labels_df[labels_df["pool"] == "auto"]

    manual_counts = manual.groupby("arxiv_id").size()
    manual_papers = int(len(manual_counts))
    min_manual_entries = int(manual_counts.min()) if manual_papers else 0
    manual_total = int(len(manual))

    auto_papers = int(auto["arxiv_id"].nunique())
    auto_pct = auto_papers / max(1, total_non_manual_eligible)

    if manual_papers < 5:
        raise ValueError(f"Manual pool requires >=5 papers, got {manual_papers}")
    if min_manual_entries < 20:
        raise ValueError(f"Each manual paper requires >=20 labeled entries, min is {min_manual_entries}")
    if manual_total < 100:
        raise ValueError(f"Manual pool requires >=100 total entries, got {manual_total}")
    if auto_papers < 2 or auto_pct < 0.10:
        raise ValueError(f"Auto pool requires >=10% of non-manual papers, got {auto_pct:.2%}")

    return {
        "manual_papers": manual_papers,
        "min_manual_entries_per_paper": min_manual_entries,
        "manual_total_entries": manual_total,
        "manual_positives": int(manual["groundtruth_arxiv_id"].notna().sum()),
        "auto_papers": auto_papers,
        "auto_coverage_pct": round(auto_pct * 100, 2),
        "auto_total_entries": int(len(auto)),
        "auto_positives": int(auto["groundtruth_arxiv_id"].notna().sum()),
    }
