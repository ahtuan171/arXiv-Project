"""Milestone 3 — Reference Matching Pipeline (Entity Resolution & Ranking).

Links extracted BibTeX entries (Milestone 2) to crawled arXiv reference metadata
(Milestone 1) using text cleaning, manual + heuristic labelling, pairwise feature
engineering (including Milestone 2 hierarchy context), supervised ranking, and MRR@5 evaluation.
"""
from __future__ import annotations

from src.matching_module.cleaning import (
    clean_title,
    extract_arxiv_id_from_text,
    extract_surnames,
    extract_year_int,
    fold_accents,
    normalize_text,
    title_tokens,
)
from src.matching_module.exporter import (
    PREDICTION_COLUMNS,
    build_hf_dataset_card,
    export_predictions_parquet,
    prepare_hf_dataset_dir,
)
from src.matching_module.features import (
    FEATURE_COLUMNS,
    build_candidate_pairs,
    extract_hierarchy_citation_features,
)
from src.matching_module.labelling import (
    auto_label_publications,
    build_manual_seed_labels,
    load_manual_labels,
    validate_labelling_requirements,
)
from src.matching_module.model import (
    compute_mrr_at_5,
    evaluate_predictions,
    split_publications,
    train_and_rank,
)

__all__ = [
    "FEATURE_COLUMNS",
    "PREDICTION_COLUMNS",
    "auto_label_publications",
    "build_candidate_pairs",
    "build_hf_dataset_card",
    "build_manual_seed_labels",
    "clean_title",
    "compute_mrr_at_5",
    "evaluate_predictions",
    "export_predictions_parquet",
    "extract_arxiv_id_from_text",
    "extract_hierarchy_citation_features",
    "extract_surnames",
    "extract_year_int",
    "fold_accents",
    "load_manual_labels",
    "normalize_text",
    "prepare_hf_dataset_dir",
    "split_publications",
    "title_tokens",
    "train_and_rank",
    "validate_labelling_requirements",
]
