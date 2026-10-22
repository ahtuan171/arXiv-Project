"""Data loader for references.jsonl -> pandas DataFrame."""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pandas as pd

from src.utils import read_jsonl


REQUIRED_COLUMNS = [
    "schema_version",
    "arxiv_id",
    "ref_arxiv_id",
    "ref_title",
    "ref_authors",
    "ref_submission_date",
    "ref_year",
    "ref_s2_id",
    "member",
    "crawled_at",
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

    # Read data produced with the previous crawl-metadata field name.
    if "ref_submission_date" not in df.columns and "ref_publication_date" in df.columns:
        df = df.rename(columns={"ref_publication_date": "ref_submission_date"})

    # Ensure all columns exist
    for col in REQUIRED_COLUMNS:
        if col not in df.columns:
            if col == "ref_authors":
                df[col] = [[] for _ in range(len(df))]
            elif col == "ref_year":
                df[col] = pd.NA
            else:
                df[col] = ""

    # Type coercion
    for col in ("arxiv_id", "ref_arxiv_id", "ref_title", "ref_submission_date", "ref_s2_id", "member", "crawled_at"):
        df[col] = df[col].astype(str)
    df["ref_year"] = pd.to_numeric(df["ref_year"], errors="coerce").astype("Int64")

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
    str_cols = ["ref_title", "ref_submission_date", "ref_s2_id", "member", "crawled_at"]
    for col in str_cols:
        df[col] = df[col].fillna("")

    return df[REQUIRED_COLUMNS]


def iter_references(path: Path | str) -> Iterator[dict]:
    """Iterate over references as dicts (memory efficient)."""
    for record in read_jsonl(Path(path)):
        yield record