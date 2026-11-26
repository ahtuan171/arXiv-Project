"""Parquet export and Hugging Face Dataset packaging for Milestone 3."""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

PREDICTION_COLUMNS = [
    "arxiv_id",
    "partition",
    "citation_key",
    "groundtruth_arxiv_id",
    "cand_1",
    "cand_2",
    "cand_3",
    "cand_4",
    "cand_5",
]

PREDICTIONS_ARROW_SCHEMA = pa.schema([
    pa.field("arxiv_id", pa.string(), nullable=False),
    pa.field("partition", pa.dictionary(pa.int32(), pa.string()), nullable=False),
    pa.field("citation_key", pa.string(), nullable=False),
    pa.field("groundtruth_arxiv_id", pa.string(), nullable=True),
    pa.field("cand_1", pa.string(), nullable=True),
    pa.field("cand_2", pa.string(), nullable=True),
    pa.field("cand_3", pa.string(), nullable=True),
    pa.field("cand_4", pa.string(), nullable=True),
    pa.field("cand_5", pa.string(), nullable=True),
])


def export_predictions_parquet(predictions_df: pd.DataFrame, out_path: Path) -> Path:
    """Export predictions DataFrame to Parquet conforming strictly to Milestone 3 schema."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df = predictions_df[PREDICTION_COLUMNS].copy()
    df["arxiv_id"] = df["arxiv_id"].astype(str)
    df["partition"] = df["partition"].astype(str)
    df["citation_key"] = df["citation_key"].astype(str)

    records: list[dict[str, Any]] = []
    for row in df.to_dict(orient="records"):
        rec: dict[str, Any] = {
            "arxiv_id": row["arxiv_id"],
            "partition": row["partition"],
            "citation_key": row["citation_key"],
        }
        for col in ("groundtruth_arxiv_id", "cand_1", "cand_2", "cand_3", "cand_4", "cand_5"):
            val = row.get(col)
            rec[col] = None if (val is None or pd.isna(val) or str(val).strip() == "") else str(val)
        records.append(rec)

    table = pa.Table.from_pylist(records, schema=PREDICTIONS_ARROW_SCHEMA)
    pq.write_table(table, out_path, compression="snappy")
    return out_path


def build_hf_dataset_card(table_names: list[str] | None = None) -> str:
    """Generate the Hugging Face README.md dataset card with YAML table configs."""
    tables = table_names or ["papers", "references", "elements", "hierarchy", "bibtex", "predictions"]
    lines = ["---", "configs:"]
    for name in tables:
        lines.extend([
            f"- config_name: {name}",
            "  data_files:",
            "  - split: train",
            f"    path: data/{name}/*.parquet",
        ])
    lines.extend([
        "---",
        "",
        "# arXiv Corpus & Reference Matching Dataset",
        "",
        "Six-table Parquet dataset produced across Milestones 1, 2, and 3.",
        "",
    ])
    return "\n".join(lines)


def prepare_hf_dataset_dir(parquet_dir: Path, hf_dir: Path) -> Path:
    """Assemble the 6-table Hugging Face dataset directory with YAML README.md."""
    tables = ["papers", "references", "elements", "hierarchy", "bibtex", "predictions"]
    hf_dir.mkdir(parents=True, exist_ok=True)

    present: list[str] = []
    for name in tables:
        src = parquet_dir / f"{name}.parquet"
        if not src.is_file():
            continue
        dst_dir = hf_dir / "data" / name
        dst_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst_dir / "part-00000.parquet")
        present.append(name)

    card = build_hf_dataset_card(present)
    (hf_dir / "README.md").write_text(card, encoding="utf-8")
    return hf_dir
