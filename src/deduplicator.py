"""In-memory deduplication and standardisation for parsed arXiv papers."""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Mapping
from typing import Any, TypeAlias

import pandas as pd


RecordInput: TypeAlias = list[Mapping[str, Any]] | pd.DataFrame | None

ELEMENT_COLUMNS = ["element_id", "arxiv_id", "kind", "content"]
HIERARCHY_COLUMNS = ["arxiv_id", "version", "element_id", "parent_id"]
BIBTEX_COLUMNS = [
    "arxiv_id", "citation_key", "entry_type", "title", "authors", "year",
    "doi", "journal", "url",
]
STAT_COLUMNS = [
    "arxiv_id", "dedup_status", "dedup_elements_count",
    "element_dedup_ratio", "dedup_bib_count", "output_size_bytes",
    "dedup_time_sec",
]


def _records(data: RecordInput) -> list[dict[str, Any]]:
    """Convert supported parser output to detached Python dictionaries."""
    if data is None:
        return []
    if isinstance(data, pd.DataFrame):
        return data.to_dict(orient="records")
    if isinstance(data, Mapping):
        return [dict(data)]
    return [dict(row) for row in data]


def _missing(value: Any) -> bool:
    """Return whether *value* is a missing scalar or empty container."""
    if value is None or value == "":
        return True
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) == 0
    try:
        result = pd.isna(value)
        return bool(result) if not hasattr(result, "__len__") else False
    except (TypeError, ValueError):
        return False


def _string(value: Any) -> str:
    """Normalise a possibly missing text field to a string."""
    return "" if _missing(value) else str(value)


def _version(value: Any) -> str:
    """Normalise numeric version labels to the conventional ``vN`` form."""
    if _missing(value):
        return ""
    text = str(value).strip()
    return f"v{text}" if text.isdigit() else text


def _authors(value: Any) -> list[Any]:
    """Return authors in the schema's list representation."""
    if _missing(value):
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _hashable(value: Any) -> Any:
    """Create a stable, hashable representation for union operations."""
    if isinstance(value, dict):
        return tuple(sorted((str(k), _hashable(v)) for k, v in value.items()))
    if isinstance(value, (list, tuple, set)):
        return tuple(_hashable(item) for item in value)
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


def _json_size(rows: Iterable[Mapping[str, Any]]) -> int:
    """Return UTF-8 JSON size without writing a temporary file."""
    payload = json.dumps(
        list(rows), ensure_ascii=False, separators=(",", ":"), default=str
    )
    return len(payload.encode("utf-8"))


def _empty_outputs() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Build empty output tables with exact Schema v2 columns."""
    return (
        pd.DataFrame(columns=ELEMENT_COLUMNS),
        pd.DataFrame(columns=HIERARCHY_COLUMNS),
        pd.DataFrame(columns=BIBTEX_COLUMNS),
    )


def _merge_value(current: Any, incoming: Any, field: str) -> Any:
    """Merge one BibTeX field without discarding non-missing information."""
    if field == "authors":
        merged = _authors(current)
        seen = {_hashable(item) for item in merged}
        for author in _authors(incoming):
            if _hashable(author) not in seen:
                merged.append(author)
                seen.add(_hashable(author))
        return merged
    if _missing(current):
        return incoming
    if _missing(incoming) or current == incoming:
        return current
    if isinstance(current, str) and isinstance(incoming, str):
        return incoming if len(incoming) > len(current) else current
    return current


def deduplicate_paper(
    arxiv_id: str,
    raw_elements: RecordInput,
    raw_hierarchy: RecordInput,
    raw_bibtex: RecordInput,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """Deduplicate one arXiv publication entirely in RAM.

    Parameters are parser outputs for one publication and may be lists of
    mappings or pandas DataFrames. Element identity is the exact pair
    ``(kind, content)``. Hierarchy IDs are remapped to the surviving element
    IDs. BibTeX rows are grouped by ``citation_key`` and merged field by
    field, retaining all non-missing values.

    Returns ``(elements, hierarchy, bibtex, stats)``. The first three values
    are DataFrames with exactly Data Schema v2 columns; ``stats`` contains
    exactly the fields needed for ``m2_dedup_stats_<name>.csv``. Missing text,
    list, and numeric values are represented as ``""``, ``[]``, and ``None``.
    No temporary or persistent file is created.
    """
    started = time.perf_counter()
    element_rows = _records(raw_elements)
    hierarchy_rows = _records(raw_hierarchy)
    bib_rows = _records(raw_bibtex)
    raw_count = len(element_rows)
    try:
        elements: list[dict[str, Any]] = []
        element_index: dict[tuple[str, str], str] = {}
        id_map: dict[tuple[str, str], str] = {}
        fallback_id_map: dict[str, str] = {}

        for position, row in enumerate(element_rows, start=1):
            kind = _string(row.get("kind"))
            content = _string(row.get("content"))
            key = (kind, content)
            element_id = element_index.get(key)
            if element_id is None:
                element_id = f"{arxiv_id}::element_{len(elements) + 1}"
                element_index[key] = element_id
                elements.append({
                    "element_id": element_id, "arxiv_id": arxiv_id,
                    "kind": kind, "content": content,
                })
            old_id = _string(row.get("element_id"))
            if old_id:
                version = _version(row.get("version"))
                if version:
                    id_map[(version, old_id)] = element_id
                fallback_id_map[old_id] = element_id
            fallback_id_map.setdefault(str(position), element_id)

        hierarchy: list[dict[str, Any]] = []
        for row in hierarchy_rows:
            version = _version(row.get("version"))
            old_element = _string(row.get("element_id"))
            old_parent = _string(row.get("parent_id"))
            new_element = id_map.get((version, old_element)) or fallback_id_map.get(
                old_element, old_element
            )
            new_parent = (
                (id_map.get((version, old_parent)) or
                 fallback_id_map.get(old_parent, old_parent))
                if old_parent else ""
            )
            hierarchy.append({
                "arxiv_id": arxiv_id, "version": version,
                "element_id": new_element, "parent_id": new_parent,
            })

        bib_index: dict[str, dict[str, Any]] = {}
        bib_order: list[str] = []
        for row in bib_rows:
            key = _string(row.get("citation_key"))
            if key not in bib_index:
                bib_index[key] = {
                    "arxiv_id": arxiv_id, "citation_key": key,
                    "entry_type": _string(row.get("entry_type")),
                    "title": _string(row.get("title")),
                    "authors": _authors(row.get("authors")),
                    "year": None if _missing(row.get("year")) else row.get("year"),
                    "doi": _string(row.get("doi")),
                    "journal": _string(row.get("journal")),
                    "url": _string(row.get("url")),
                }
                bib_order.append(key)
            else:
                target = bib_index[key]
                for field in BIBTEX_COLUMNS[2:]:
                    target[field] = _merge_value(target[field], row.get(field), field)

        bib_output = [bib_index[key] for key in bib_order]
        elements_df = pd.DataFrame(elements, columns=ELEMENT_COLUMNS)
        hierarchy_df = pd.DataFrame(hierarchy, columns=HIERARCHY_COLUMNS)
        bibtex_df = pd.DataFrame(bib_output, columns=BIBTEX_COLUMNS)
        output_size = (
            _json_size(elements) + _json_size(hierarchy) + _json_size(bib_output)
        )
        status = "skipped" if not (element_rows or hierarchy_rows or bib_rows) else "success"
        ratio = 0.0 if raw_count == 0 else 1.0 - len(elements) / raw_count
        stats = {
            "arxiv_id": str(arxiv_id), "dedup_status": status,
            "dedup_elements_count": int(len(elements)),
            "element_dedup_ratio": float(ratio),
            "dedup_bib_count": int(len(bib_output)),
            "output_size_bytes": int(output_size),
            "dedup_time_sec": float(time.perf_counter() - started),
        }
        return elements_df, hierarchy_df, bibtex_df, stats
    except Exception:
        elements_df, hierarchy_df, bibtex_df = _empty_outputs()
        stats = {
            "arxiv_id": str(arxiv_id), "dedup_status": "failed",
            "dedup_elements_count": 0, "element_dedup_ratio": 0.0,
            "dedup_bib_count": 0, "output_size_bytes": 0,
            "dedup_time_sec": float(time.perf_counter() - started),
        }
        return elements_df, hierarchy_df, bibtex_df, stats


__all__ = [
    "BIBTEX_COLUMNS", "ELEMENT_COLUMNS", "HIERARCHY_COLUMNS",
    "STAT_COLUMNS", "deduplicate_paper",
]
