"""Read-only Milestone 1 dataset audit. No network requests or mutations.

Usage:
    python -m src.scripts.audit_m1 --data-dir /path/to/data
    python -m src.scripts.audit_m1 --data-dir /path/to/data --start-id 2501.14408 --end-id 2501.19407
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

_MODERN_ID = re.compile(r"^\d{4}[._]\d{4,5}$")
_LEGACY_ID = re.compile(r"^[A-Za-z-]+(?:\.[A-Za-z-]+)?[/_]\d{7}$")


def canonical_id(value: Any) -> str:
    value = str(value or "").strip()
    if value.lower().startswith("arxiv:"):
        value = value[6:]
    value = re.sub(r"v\d+$", "", value)
    return value.replace(".", "_").replace("/", "_").replace(":", "_")


def valid_id(value: Any) -> bool:
    value = str(value or "")
    return bool(_MODERN_ID.fullmatch(value) or _LEGACY_ID.fullmatch(value))


def read_jsonl(path: Path, issues: list[str]) -> list[dict]:
    if not path.is_file():
        issues.append(f"missing file: {path}")
        return []
    rows: list[dict] = []
    with path.open(encoding="utf-8") as stream:
        for lineno, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
                if not isinstance(obj, dict):
                    raise ValueError("expected JSON object")
                rows.append(obj)
            except (json.JSONDecodeError, ValueError) as error:
                issues.append(f"{path}:{lineno}: {error}")
    return rows


def read_csv(path: Path, issues: list[str]) -> list[dict]:
    if not path.is_file():
        issues.append(f"missing file: {path}")
        return []
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def audit(data_dir: Path, start_id: str | None = None, end_id: str | None = None) -> dict:
    """Audit snapshots and published references without changing any file."""
    issues: list[str] = []
    papers = read_jsonl(data_dir / "papers.jsonl", issues)
    discovery = read_csv(data_dir / "csv" / "m1_discovery_stats.csv", issues)
    references = read_jsonl(data_dir / "references.jsonl", issues)
    downloads = read_csv(data_dir / "csv" / "m1_download_stats.csv", issues)
    reference_stats = read_csv(data_dir / "csv" / "m1_reference_stats.csv", issues)
    paper_ids = [canonical_id(row.get("arxiv_id")) for row in papers]
    duplicate_papers = sum(n - 1 for n in Counter(paper_ids).values() if n > 1)
    if duplicate_papers:
        issues.append(f"duplicate paper IDs: {duplicate_papers}")
    paper_by_id = {canonical_id(row.get("arxiv_id")): row for row in papers}
    discovery_by_id = {canonical_id(row.get("arxiv_id")): row for row in discovery}
    successful = {
        cid for cid, row in discovery_by_id.items()
        if row.get("discovery_status") == "success"
        and (start_id is None or canonical_id(start_id) <= cid)
        and (end_id is None or cid <= canonical_id(end_id))
    }
    missing_metadata = sorted(
        cid for cid in successful
        if cid not in paper_by_id or paper_by_id[cid].get("error")
        or not paper_by_id[cid].get("title")
    )
    if missing_metadata:
        issues.append(f"successful discovery IDs without metadata: {len(missing_metadata)}")
    version_count = 0
    multi_version = 0
    for cid in successful:
        paper = paper_by_id.get(cid, {})
        versions = paper.get("versions") or []
        if not isinstance(versions, list):
            issues.append(f"{cid}: versions is not a list")
            continue
        num = len(versions)
        version_count += num
        multi_version += int(num > 1)
        if num != int(discovery_by_id[cid].get("num_versions") or 0):
            issues.append(f"{cid}: version count does not match discovery CSV")
    pairs = set()
    duplicates = 0
    invalid_edges = 0
    for row in references:
        citing = canonical_id(row.get("arxiv_id"))
        cited = canonical_id(row.get("ref_arxiv_id"))
        if not valid_id(citing) or not valid_id(cited):
            invalid_edges += 1
        pair = (citing, cited)
        if pair in pairs:
            duplicates += 1
        pairs.add(pair)
    if duplicates:
        issues.append(f"duplicate published reference edges: {duplicates}")
    if invalid_edges:
        issues.append(f"published reference rows with invalid IDs: {invalid_edges}")
    return {
        "data_dir": str(data_dir),
        "discovery_rows": len(discovery),
        "discovery_success_in_scope": len(successful),
        "missing_usable_metadata": len(missing_metadata),
        "expected_versions_in_scope": version_count,
        "multi_version_publications_in_scope": multi_version,
        "download_stats_rows": len(downloads),
        "reference_stats_rows": len(reference_stats),
        "published_reference_rows": len(references),
        "published_reference_pairs_unique": len(pairs),
        "duplicate_reference_edges": duplicates,
        "issues": issues,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--start-id", default=None)
    parser.add_argument("--end-id", default=None)
    args = parser.parse_args()
    report = audit(args.data_dir, args.start_id, args.end_id)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if report["issues"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
