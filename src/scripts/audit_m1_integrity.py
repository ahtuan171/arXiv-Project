"""Read-only Milestone 1 full-dataset integrity and coverage audit.

No API calls or mutations. Verifies source vN markers and hashes, metadata
index keys, published citation edge uniqueness and raw reference ledgers.
Incomplete collection is not corruption. Structural consistency alone is NOT
proof of full historical revision dates/citations or assignment acceptance.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from src import utils
from src.crawl_references_module.raw_reference_collector import _done, _load_state, _read_records
from src.scripts.audit_version_downloads import audit_version_downloads
from src.scripts.build_version_metadata import build_version_metadata

ProgressFn = Callable[[int, int, dict], None]


def _published(path: Path) -> tuple[dict, set[tuple[str, str]], list[str]]:
    counts = Counter()
    pairs = set()
    problems = []
    if not path.is_file():
        return {"rows": 0, "unique_pairs": 0, "duplicate_pairs": 0, "invalid_ids": 0}, pairs, ["Missing published references.jsonl"]
    for row in _read_records(path):
        counts["rows"] += 1
        src, dst = row.get("arxiv_id"), row.get("ref_arxiv_id")
        if not utils.is_valid_arxiv_id(src) or not utils.is_valid_arxiv_id(dst):
            counts["invalid_ids"] += 1
            continue
        pair = (utils.canonical_arxiv_id(src), utils.canonical_arxiv_id(dst))
        if pair in pairs:
            counts["duplicate_pairs"] += 1
        pairs.add(pair)
    if counts["duplicate_pairs"]:
        problems.append(f"Published citations have {counts['duplicate_pairs']} duplicate pairs")
    if counts["invalid_ids"]:
        problems.append(f"Published citations have {counts['invalid_ids']} invalid ID pairs")
    return {
        "rows": counts["rows"], "unique_pairs": len(pairs),
        "duplicate_pairs": counts["duplicate_pairs"], "invalid_ids": counts["invalid_ids"],
    }, pairs, problems


def _metadata_snapshot_keys(path: Path) -> set[tuple[str, str]]:
    found = set()
    for item in _read_records(path):
        key = (item.get("arxiv_id"), item.get("version"))
        if not all(isinstance(x, str) and x for x in key) or key in found:
            raise ValueError(f"Invalid or duplicate metadata_versions key: {key}")
        found.add(key)
    return found


def _csv_statuses(path: Path) -> dict[tuple[str, str], str]:
    found = {}
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if not {"arxiv_id", "version", "status"}.issubset(reader.fieldnames or []):
            raise ValueError("Version CSV lacks required columns")
        for item in reader:
            key = (item["arxiv_id"], item["version"])
            if not all(key) or key in found:
                raise ValueError(f"Invalid or duplicate version CSV key: {key}")
            found[key] = item["status"]
    return found


def audit_m1_integrity(
    data_dir: str | Path, *, sample_size: int = 10,
    progress_callback: ProgressFn | None = None,
) -> dict:
    """Audit all months. Do not scope to the current notebook ID list."""
    if sample_size < 0:
        raise ValueError("sample_size must be nonnegative")
    root = Path(data_dir)
    if not root.is_dir():
        raise FileNotFoundError(f"Missing data directory: {root}")
    blockers, warnings, incomplete = [], [], []

    paper_ids = set()
    duplicates = 0
    for row in _read_records(root / "papers.jsonl"):
        pid = row.get("arxiv_id")
        if not utils.is_valid_arxiv_id(pid):
            blockers.append(f"Invalid paper ID: {str(pid)[:80]}")
            continue
        key = utils.canonical_arxiv_id(pid)
        duplicates += int(key in paper_ids)
        paper_ids.add(key)
    if duplicates:
        blockers.append(f"Duplicate paper snapshot IDs: {duplicates}")

    sources = audit_version_downloads(root, sample_size=sample_size,
                                      progress_callback=progress_callback)
    metadata = build_version_metadata(root, sample_size=sample_size)
    vs, ms = sources["summary"], metadata["summary"]
    source_rows = sources["rows"]
    source_keys = {(r["arxiv_id"], r["version"]) for r in source_rows}
    metadata_keys = {(r["arxiv_id"], r["version"]) for r in metadata["rows"]}
    if source_keys != metadata_keys or len(source_keys) != len(source_rows):
        blockers.append("Expected source versions and version metadata keys disagree")
    for field in ("conflicts", "missing_files", "orphan_manifest_keys", "invalid_version_metadata"):
        if vs[field]:
            blockers.append(f"Source audit {field}: {vs[field]}")
    if vs["marker_manifest_disagreement"]:
        warnings.append(f"{vs['marker_manifest_disagreement']} marker/manifest disagreements")
    if any(vs[k] for k in ("pending", "failed", "not_found")):
        incomplete.append(
            f"Unfinished source versions: pending={vs['pending']}, "
            f"failed={vs['failed']}, not_found={vs['not_found']}"
        )
    if ms["unknown_version_dates"]:
        incomplete.append(f"Unknown revision dates: {ms['unknown_version_dates']}")
    if ms["ambiguous_revised_dates"]:
        warnings.append(f"Ambiguous revised_dates on {ms['ambiguous_revised_dates']} papers")

    try:
        published, published_pairs, issues = _published(root / "references.jsonl")
        blockers.extend(issues)
    except (OSError, ValueError, UnicodeError) as exc:
        blockers.append(f"Published references unreadable: {exc}")
        published = {"rows": 0, "unique_pairs": 0, "duplicate_pairs": 0, "invalid_ids": 0}
        published_pairs = set()

    rawstats = {
        "raw_rows": 0, "with_arxiv_id": 0, "without_arxiv_id": 0,
        "verified_papers": 0, "not_found_papers": 0, "unresolved_papers": 0,
        "orphan_raw_papers": 0, "inferred_pagination_papers": 0,
        "overlap_with_published_pairs": 0, "raw_only_pairs": 0,
        "published_only_pairs": 0,
    }
    rawpairs = set()
    valid_paper_ids = {r["arxiv_id"] for r in metadata["rows"] if r["version"] == "v1"}
    try:
        raw, ledger = _load_state(root)
        rawstats["orphan_raw_papers"] = len(set(raw) - paper_ids)
        for paper_id, entries in raw.items():
            rawstats["raw_rows"] += len(entries)
            for position, item in entries.items():
                if item.get("version") is not None:
                    blockers.append(f"Raw citation assigned to unproven version {paper_id}/{position}")
                if item.get("version_attribution") != "unknown_s2_publication_level":
                    blockers.append(f"Raw citation lacks version provenance {paper_id}/{position}")
                ref = item.get("ref_arxiv_id")
                if ref:
                    if not utils.is_valid_arxiv_id(ref):
                        blockers.append(f"Invalid raw reference arXiv ID {paper_id}/{position}")
                    else:
                        rawpairs.add((paper_id, utils.canonical_arxiv_id(ref)))
                    rawstats["with_arxiv_id"] += 1
                else:
                    rawstats["without_arxiv_id"] += 1

        for paper_id in valid_paper_ids:
            event = ledger.get(paper_id, {})
            records = raw.get(paper_id, {})
            if _done(event, records):
                rawstats["verified_papers"] += 1
                if event.get("completeness") == "inferred_short_page":
                    rawstats["inferred_pagination_papers"] += 1
            elif event.get("status") == "ok":
                blockers.append(f"Raw reference ledger count/hash mismatch: {paper_id}")
                rawstats["unresolved_papers"] += 1
            elif event.get("status") == "not_found" and not records:
                rawstats["not_found_papers"] += 1
            else:
                rawstats["unresolved_papers"] += 1

        if rawstats["orphan_raw_papers"]:
            warnings.append(f"Raw references outside metadata scope: {rawstats['orphan_raw_papers']} papers")
        if rawstats["inferred_pagination_papers"]:
            warnings.append(f"Reference pagination inferred for {rawstats['inferred_pagination_papers']} papers")
    except (ValueError, OSError, TypeError, KeyError) as exc:
        blockers.append(f"Raw references or ledger unreadable: {exc}")
        rawstats["unresolved_papers"] = len(valid_paper_ids)

    rawstats["overlap_with_published_pairs"] = len(rawpairs & published_pairs)
    rawstats["raw_only_pairs"] = len(rawpairs - published_pairs)
    rawstats["published_only_pairs"] = len(published_pairs - rawpairs)
    if rawstats["unresolved_papers"]:
        incomplete.append(f"Raw references pending or unverified: {rawstats['unresolved_papers']} papers")

    snapshots = {}
    meta_path = root / "metadata_versions.jsonl"
    snapshots["metadata_versions_exists"] = meta_path.is_file()
    if meta_path.is_file():
        try:
            snapshots["metadata_versions_matches_keys"] = (
                _metadata_snapshot_keys(meta_path) == source_keys
            )
            if not snapshots["metadata_versions_matches_keys"]:
                warnings.append("metadata_versions.jsonl is stale; rerun cell 2.5 export")
        except (ValueError, OSError, TypeError) as exc:
            blockers.append(f"Invalid metadata_versions.jsonl: {exc}")
    else:
        incomplete.append("metadata_versions.jsonl has not been exported")

    csvfile = root / "csv" / "m1_download_versions.csv"
    snapshots["download_versions_csv_exists"] = csvfile.is_file()
    if csvfile.is_file():
        try:
            snapshot = _csv_statuses(csvfile)
            current = {(r["arxiv_id"], r["version"]): r["status"] for r in source_rows}
            snapshots["download_versions_csv_current"] = snapshot == current
            if snapshot != current:
                warnings.append("m1_download_versions.csv is stale; rerun cell 2.3 export")
        except (ValueError, OSError, TypeError, csv.Error) as exc:
            blockers.append(f"Invalid m1_download_versions.csv: {exc}")
    else:
        incomplete.append("m1_download_versions.csv has not been exported")

    verified = vs["verified_success"] + vs["verified_pdf_only"]
    total = vs["expected_versions"]
    status = "needs_fix" if blockers else "in_progress" if incomplete else "structurally_consistent"
    return {
        "data_dir": str(root),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": status,
        "coverage": {
            "valid_metadata_papers": ms["valid_papers"],
            "expected_versions": total,
            "verified_source_versions": verified,
            "source_coverage_pct": round(100 * verified / total, 2) if total else 0.0,
            "version_metadata_rows": len(metadata["rows"]),
            "known_version_dates": ms["known_version_dates"],
            "unknown_version_dates": ms["unknown_version_dates"],
            "raw_reference_papers_verified": rawstats["verified_papers"],
        },
        "source_versions": vs,
        "metadata_versions": ms,
        "published_references": published,
        "raw_references": rawstats,
        "snapshots": snapshots,
        "blocker_count": len(blockers),
        "blockers": blockers[:sample_size],
        "warning_count": len(warnings),
        "warnings": warnings[:sample_size],
        "incomplete": incomplete,
        "source_issue_samples": sources["sample_issues"],
        "metadata_issue_samples": metadata["sample_issues"],
        "interpretation": (
            "Read-only structural audit; no network or dataset changes. "
            "Published versus raw citation overlap is informational, not required equality. "
            "Unknown revision dates and version-specific citations are not fabricated. "
            "Even structurally_consistent is NOT proof of Milestone 1 acceptance."
        ),
    }


def export_integrity_report(report: dict) -> Path:
    """Explicit, atomic report only; block symlink/path conflicts."""
    root = Path(report["data_dir"])
    folder = root / "reports"
    if folder.is_symlink() or (folder.exists() and not folder.is_dir()):
        raise ValueError(f"Unsafe report folder: {folder}")
    folder.mkdir(parents=True, exist_ok=True)
    output = folder / "m1_integrity_report.json"
    if output.is_symlink() or (output.exists() and not output.is_file()):
        raise ValueError(f"Unsafe report path: {output}")
    fd, temp = tempfile.mkstemp(prefix=".m1-audit-", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp, output)
    finally:
        Path(temp).unlink(missing_ok=True)
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--sample-size", type=int, default=10)
    parser.add_argument("--export", action="store_true")
    args = parser.parse_args()
    report = audit_m1_integrity(args.data_dir, sample_size=args.sample_size)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.export:
        print(f"Saved integrity report: {export_integrity_report(report)}")
    else:
        print("READ ONLY: no files changed. Use --export for a standalone JSON report.")


if __name__ == "__main__":
    main()
