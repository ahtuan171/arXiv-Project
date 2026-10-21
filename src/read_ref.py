"""Data loader for references.jsonl -> pandas DataFrame."""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pandas as pd

from src.utils import read_jsonl


REQUIRED_COLUMNS = [
    "arxiv_id",
    "ref_arxiv_id",
    "ref_title",
    "ref_authors",
    "ref_submission_date",
    "ref_s2_id",
]


def load_references(path: Path | str) -> pd.DataFrame:
    """Load references.jsonl into a DataFrame with proper dtypes."""
    path = Path(path)
    if not path.exists():
        return pd.DataFrame(columns=REQUIRED_COLUMNS)

    records = list(read_jsonl(path))
    if not records:
        return pd.DataFrame(columns=REQUIRED_COLUMNS)

    df = pd.DataFrame(records)

    # Ensure all columns exist
    for col in REQUIRED_COLUMNS:
        if col not in df.columns:
            df[col] = "" if col != "ref_authors" else [[] for _ in range(len(df))]

    # Type coercion
    df["arxiv_id"] = df["arxiv_id"].astype(str)
    df["ref_arxiv_id"] = df["ref_arxiv_id"].astype(str)
    df["ref_title"] = df["ref_title"].astype(str)
    df["ref_submission_date"] = df["ref_submission_date"].astype(str)
    df["ref_s2_id"] = df["ref_s2_id"].astype(str)

    # ref_authors: ensure list of strings
    def _ensure_list(v):
        if isinstance(v, list):
            return [str(x) for x in v]
        if isinstance(v, str):
            try:
                import json
                parsed = json.loads(v)
                if isinstance(parsed, list):
                    return [str(x) for x in parsed]
            except Exception:
                pass
            return [v] if v else []
        return []

    df["ref_authors"] = df["ref_authors"].apply(_ensure_list)

    # Replace NaN/None with empty string for string columns
    str_cols = ["ref_title", "ref_submission_date", "ref_s2_id"]
    for col in str_cols:
        df[col] = df[col].fillna("")

    return df[REQUIRED_COLUMNS]


def iter_references(path: Path | str) -> Iterator[dict]:
    """Iterate over references as dicts (memory efficient)."""
    for record in read_jsonl(Path(path)):
        yield record