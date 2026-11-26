"""Convert Milestone 1 JSONL files (papers.jsonl, references.jsonl) to Parquet.

Reads ``papers.jsonl`` and ``references.jsonl`` from ``data/jsonl`` (or ``--input-dir``)
and writes ``papers.parquet`` and ``references.parquet`` to ``data/parquet`` (or ``--output-dir``)
with explicit flat/primitive PyArrow schemas compatible with Hugging Face Datasets.

Usage:
    python src/scripts/jsonl_to_parquet.py
    python src/scripts/jsonl_to_parquet.py --input-dir data/jsonl --output-dir data/parquet
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq


PAPERS_SCHEMA = pa.schema([
    ("arxiv_id", pa.string()),
    ("title", pa.string()),
    ("authors", pa.list_(pa.string())),
    ("abstract", pa.string()),
    ("categories", pa.list_(pa.string())),
    ("primary_category", pa.string()),
    ("submission_date", pa.string()),
    ("revised_dates", pa.list_(pa.string())),
    ("versions", pa.list_(pa.string())),
    ("doi", pa.string()),
    ("venue", pa.string()),
    ("comment", pa.string()),
    ("fetched_at", pa.string()),
])

REFERENCES_SCHEMA = pa.schema([
    ("arxiv_id", pa.string()),
    ("ref_arxiv_id", pa.string()),
    ("ref_title", pa.string()),
    ("ref_authors", pa.list_(pa.string())),
    ("ref_submission_date", pa.string()),
    ("ref_s2_id", pa.string()),
])


def _str_or_none(val: Any) -> str | None:
    if val is None:
        return None
    return str(val)


def _normalize_arxiv_id(val: Any) -> str | None:
    """Convert canonical underscore IDs (e.g. '2501_07642') to standard dot/slash form."""
    if val is None:
        return None
    text = str(val).strip()
    if re.fullmatch(r"\d{4}_\d{4,5}", text):
        return text.replace("_", ".", 1)
    if re.fullmatch(r"[A-Za-z-]+(?:\.[A-Za-z-]+)?_\d{7}", text):
        name, num = text.rsplit("_", 1)
        return f"{name}/{num}"
    return text


def _str_list(val: Any) -> list[str]:
    if not val:
        return []
    if isinstance(val, (list, tuple)):
        return [str(x) for x in val if x is not None]
    return [str(val)]


def _normalize_row(row: dict[str, Any], schema: pa.Schema) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for field in schema:
        raw = row.get(field.name)
        if pa.types.is_list(field.type):
            out[field.name] = _str_list(raw)
        elif field.name in {"arxiv_id", "ref_arxiv_id"}:
            out[field.name] = _normalize_arxiv_id(raw)
        else:
            out[field.name] = _str_or_none(raw)
    return out


def convert_jsonl_to_parquet(
    jsonl_path: Path,
    parquet_path: Path,
    schema: pa.Schema,
    chunk_size: int = 50_000,
) -> int:
    """Stream a JSONL file in chunks and write a single Snappy-compressed Parquet file."""
    if not jsonl_path.is_file():
        raise FileNotFoundError(f"Khong tim thay file: {jsonl_path}")

    parquet_path.parent.mkdir(parents=True, exist_ok=True)
    total_rows = 0
    batch: list[dict[str, Any]] = []

    with pq.ParquetWriter(parquet_path, schema, compression="snappy") as writer:
        with jsonl_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                batch.append(_normalize_row(obj, schema))
                total_rows += 1
                if len(batch) >= chunk_size:
                    writer.write_table(pa.Table.from_pylist(batch, schema=schema))
                    batch.clear()
            if batch:
                writer.write_table(pa.Table.from_pylist(batch, schema=schema))
                batch.clear()
            elif total_rows == 0:
                writer.write_table(pa.Table.from_pylist([], schema=schema))

    return total_rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Convert papers.jsonl and references.jsonl to Parquet.")
    parser.add_argument("--input-dir", type=Path, default=Path("data/jsonl"), help="Thu muc chua file .jsonl")
    parser.add_argument("--output-dir", type=Path, default=Path("data/parquet"), help="Thu muc xuat file .parquet")
    parser.add_argument("--chunk-size", type=int, default=50_000, help="So dong moi chunk khi ghi Parquet")
    args = parser.parse_args()

    jobs = [
        ("papers.jsonl", "papers.parquet", PAPERS_SCHEMA),
        ("references.jsonl", "references.parquet", REFERENCES_SCHEMA),
    ]

    for src_name, dst_name, schema in jobs:
        src_path = args.input_dir / src_name
        dst_path = args.output_dir / dst_name
        if not src_path.is_file():
            print(f"[SKIP] Khong tim thay {src_path}")
            continue
        rows = convert_jsonl_to_parquet(src_path, dst_path, schema, chunk_size=args.chunk_size)
        size_mb = dst_path.stat().st_size / (1024 * 1024)
        print(f"[OK] {src_path} -> {dst_path} | {rows:,} rows | {size_mb:.2f} MB")

    return 0


if __name__ == "__main__":
    sys.exit(main())
