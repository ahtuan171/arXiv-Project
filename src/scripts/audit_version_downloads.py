"""Reconcile version-aware Milestone 1 source outputs without changing legacy data.

The event log is append-only: the latest event alone is NOT proof that files
exist. A valid, hashed .version-status.json and its source files are the truth.
This audit checks only expected raw/<paper>/vN destinations, not legacy files.

CLI examples:
    python -m src.scripts.audit_version_downloads --data-dir /path/to/data
    python -m src.scripts.audit_version_downloads --data-dir /path/to/data --export

Export always covers the WHOLE metadata snapshot to prevent a second month's
run from replacing the CSV with only its own range. It replaces only the
new data/csv/m1_download_versions.csv after a complete successful audit.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Callable

from src.download_module.version_downloader import (
    SOURCE_BASE_URL,
    _latest_version_events,
    _network_id,
    _status_record,
)
from src.scripts.audit_missing_versions import _latest_jsonl, _valid_meta, _versions

COLUMNS = [
    "arxiv_id", "version", "status", "verification", "manifest_status",
    "source_url", "raw_path", "raw_size_bytes", "cleaned_size_bytes",
    "num_tex_files", "download_time_sec", "attempts", "fetched_at", "error",
]


ProgressFn = Callable[[int, int, dict], None]


def audit_version_downloads(
    data_dir: str | Path, *, sample_size: int = 10,
    progress_callback: ProgressFn | None = None,
) -> dict[str, Any]:
    """Verify every expected version and return one in-memory row per key.

    This is read-only and intentionally checks hashes; on a large Drive folder,
    it can take time. No API requests, no legacy source enumeration or writes.
    """
    if sample_size < 0:
        raise ValueError("sample_size must be >= 0")
    root = Path(data_dir)
    metadata = root / "papers.jsonl"
    if not metadata.is_file():
        raise FileNotFoundError(f"Missing metadata snapshot: {metadata}")
    papers = _latest_jsonl(metadata)
    # Match version downloader precedence for pending journal metadata.
    for paper_id, row in _latest_jsonl(root / "papers.jsonl.journal").items():
        if _valid_meta(row) or not _valid_meta(papers.get(paper_id)):
            papers[paper_id] = row
    history = _latest_version_events(root / "versions_manifest.jsonl")

    rows: list[dict] = []
    expected: set[tuple[str, str]] = set()
    counters = Counter()
    issues: list[str] = []
    # Calculate total without opening/hash-checking any raw source files.
    progress_total = 0
    for paper in papers.values():
        if _valid_meta(paper):
            labels, _ = _versions(paper, None)
            progress_total += len(labels)
    progress_done = 0
    warned_progress = False

    for paper_id in sorted(papers):
        paper = papers[paper_id]
        if not _valid_meta(paper):
            continue
        counters["valid_papers"] += 1
        versions, note = _versions(paper, None)
        if not versions:
            counters["invalid_version_metadata"] += 1
            if len(issues) < sample_size:
                issues.append(f"{paper_id}: {note or 'missing versions'}")
            continue

        for version in versions:
            key = (paper_id, version)
            expected.add(key)
            event = history.get(key, {})
            manifest_status = str(event.get("status") or "")
            state = _status_record(root / "raw", paper_id, version)
            url = f"{SOURCE_BASE_URL}/{_network_id(paper_id)}{version}"
            record = {
                "arxiv_id": paper_id, "version": version,
                "status": "pending", "verification": "not_applicable",
                "manifest_status": manifest_status,
                "source_url": str(event.get("source_url") or url),
                "raw_path": "", "raw_size_bytes": "", "cleaned_size_bytes": "",
                "num_tex_files": "", "download_time_sec": event.get("download_time_sec", ""),
                "attempts": event.get("attempts", ""),
                "fetched_at": event.get("fetched_at", ""),
                "error": "",
            }
            if state and state["status"] in {"success", "pdf_only"}:
                marker = state["marker"]
                record.update(
                    status=state["status"], verification="verified",
                    source_url=marker.get("source_url") or url,
                    raw_path=f"raw/{paper_id}/{version}",
                    raw_size_bytes=marker.get("raw_size_bytes", ""),
                    cleaned_size_bytes=marker.get("cleaned_size_bytes", ""),
                    num_tex_files=marker.get("num_tex_files", ""),
                    fetched_at=marker.get("fetched_at") or record["fetched_at"],
                )
                if manifest_status and manifest_status != state["status"]:
                    # Trusted files win over stale or interrupted journal events.
                    counters["marker_manifest_disagreement"] += 1
                    if len(issues) < sample_size:
                        issues.append(f"{paper_id}/{version}: verified marker overrides manifest {manifest_status}")
            elif state:
                record.update(status="conflict", verification="conflict", error=state["error"])
                if len(issues) < sample_size:
                    issues.append(f"{paper_id}/{version}: {state['error']}")
            elif manifest_status in {"success", "pdf_only"}:
                record.update(status="missing_files", verification="missing",
                              error="manifest says complete, but version directory is absent")
                if len(issues) < sample_size:
                    issues.append(f"{paper_id}/{version}: missing files after {manifest_status}")
            elif manifest_status in {"not_found", "failed"}:
                record.update(status=manifest_status, error=str(event.get("error") or ""))
            # Leave never-attempted versions as pending, including legacy-only source.
            counters[record["status"]] += 1
            rows.append(record)
            progress_done += 1
            if progress_callback is not None and not warned_progress:
                try:
                    progress_callback(progress_done, progress_total, record)
                except Exception as exc:
                    # A failing progress UI must never invalidate the audit.
                    print(f"[Version audit] Progress display warning: {exc}", flush=True)
                    warned_progress = True

    orphan_keys = set(history).difference(expected)
    counters["orphan_manifest_keys"] = len(orphan_keys)
    if orphan_keys and len(issues) < sample_size:
        issues.extend(f"orphan manifest: {paper_id}/{version}" for paper_id, version in sorted(orphan_keys)[:sample_size-len(issues)])

    summary = {
        "valid_papers": counters["valid_papers"],
        "expected_versions": len(expected),
        "verified_success": counters["success"],
        "verified_pdf_only": counters["pdf_only"],
        "not_found": counters["not_found"],
        "failed": counters["failed"],
        "pending": counters["pending"],
        "conflicts": counters["conflict"],
        "missing_files": counters["missing_files"],
        "invalid_version_metadata": counters["invalid_version_metadata"],
        "orphan_manifest_keys": counters["orphan_manifest_keys"],
        "marker_manifest_disagreement": counters["marker_manifest_disagreement"],
    }
    assert len(rows) == len(expected)
    assert sum(summary[k] for k in (
        "verified_success", "verified_pdf_only", "not_found", "failed",
        "pending", "conflicts", "missing_files",
    )) == summary["expected_versions"]
    return {"data_dir": str(root), "summary": summary, "rows": rows,
            "sample_issues": issues[:sample_size]}


def export_version_csv(report: dict[str, Any]) -> Path:
    """Atomically write ONLY the new CSV from a full-dataset audit report."""
    root = Path(report["data_dir"])
    dest = root / "csv" / "m1_download_versions.csv"
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{dest.name}.", dir=dest.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=COLUMNS, extrasaction="raise")
            writer.writeheader()
            writer.writerows(report["rows"])
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, dest)
    finally:
        Path(name).unlink(missing_ok=True)
    return dest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--export", action="store_true", help="Atomically refresh new version CSV")
    parser.add_argument("--sample-size", type=int, default=10)
    args = parser.parse_args()
    report = audit_version_downloads(args.data_dir, sample_size=args.sample_size)
    print(json.dumps({"summary": report["summary"], "issues": report["sample_issues"]},
                     ensure_ascii=False, indent=2))
    if args.export:
        target = export_version_csv(report)
        print(f"Wrote {len(report['rows'])} version rows to {target}")
    else:
        print("READ ONLY: no files changed. Add --export to write new version CSV.")


if __name__ == "__main__":
    main()
