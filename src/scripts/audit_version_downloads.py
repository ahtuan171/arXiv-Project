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

from src import utils
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


def _audit_single_version(
    args: tuple[str, str, dict, str, Path],
) -> tuple[dict, str | None, str | None, str | None]:
    paper_id, version, event, url, raw_dir = args
    manifest_status = str(event.get("status") or "")
    state = _status_record(raw_dir, paper_id, version)
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
    disagreement = None
    conflict_err = None
    missing_err = None

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
            disagreement = f"{paper_id}/{version}: verified marker overrides manifest {manifest_status}"
    elif state:
        record.update(status="conflict", verification="conflict", error=state["error"])
        conflict_err = f"{paper_id}/{version}: {state['error']}"
    elif manifest_status in {"success", "pdf_only"}:
        record.update(status="missing_files", verification="missing",
                      error="manifest says complete, but version directory is absent")
        missing_err = f"{paper_id}/{version}: missing files after {manifest_status}"
    elif manifest_status in {"not_found", "failed"}:
        record.update(status=manifest_status, error=str(event.get("error") or ""))

    return (record, disagreement, conflict_err, missing_err)


def audit_version_downloads(
    data_dir: str | Path, *, sample_size: int = 10,
    progress_callback: ProgressFn | None = None,
    max_workers: int | None = None,
    ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Verify every expected version and return one in-memory row per key.

    This is read-only and intentionally checks hashes; on a large Drive folder,
    it can take time. No API requests, no legacy source enumeration or writes.
    If ids is provided, scopes the audit to the specified arXiv IDs.
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

    target_ids = {utils.canonical_arxiv_id(str(x)) for x in ids} if ids is not None else None
    paper_keys = sorted(papers) if target_ids is None else sorted(pid for pid in papers if pid in target_ids)

    rows: list[dict] = []
    expected: set[tuple[str, str]] = set()
    counters = Counter()
    issues: list[str] = []
    # Calculate total without opening/hash-checking any raw source files.
    progress_total = 0
    for paper_id in paper_keys:
        paper = papers[paper_id]
        if _valid_meta(paper):
            labels, _ = _versions(paper, None)
            progress_total += len(labels)
    progress_done = 0
    warned_progress = False

    items_to_audit: list[tuple[str, str, dict, str, Path]] = []
    for paper_id in paper_keys:
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
            url = f"{SOURCE_BASE_URL}/{_network_id(paper_id)}{version}"
            items_to_audit.append((paper_id, version, event, url, root / "raw"))

    workers = max_workers if max_workers is not None else min(16, max(4, (os.cpu_count() or 4) * 2))
    if len(items_to_audit) <= 1 or workers <= 1:
        iterator = map(_audit_single_version, items_to_audit)
        executor = None
    else:
        from concurrent.futures import ThreadPoolExecutor
        executor = ThreadPoolExecutor(max_workers=workers)
        iterator = executor.map(_audit_single_version, items_to_audit)

    try:
        for record, disagreement, conflict_err, missing_err in iterator:
            if disagreement:
                counters["marker_manifest_disagreement"] += 1
                if len(issues) < sample_size:
                    issues.append(disagreement)
            if conflict_err and len(issues) < sample_size:
                issues.append(conflict_err)
            if missing_err and len(issues) < sample_size:
                issues.append(missing_err)

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
    finally:
        if executor is not None:
            executor.shutdown(wait=False)

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




DOWNLOAD_STATS_COLUMNS = [
    "arxiv_id", "download_status", "has_tex",
    "raw_size_bytes", "cleaned_size_bytes", "num_tex_files", "download_time_sec",
]


def export_download_stats_csv(report: dict[str, Any], *, csv_path: str | Path | None = None) -> Path:
    """Aggregate per-version audit rows into paper-level Table 2 (m1_download_stats.csv)."""
    root = Path(report["data_dir"])
    dest = Path(csv_path) if csv_path is not None else root / "csv" / "m1_download_stats.csv"
    dest.parent.mkdir(parents=True, exist_ok=True)

    grouped: dict[str, list[dict]] = {}
    for r in report["rows"]:
        grouped.setdefault(r["arxiv_id"], []).append(r)

    rows = []
    for aid, versions in sorted(grouped.items()):
        statuses = [v.get("status") for v in versions]
        if any(s == "success" for s in statuses):
            status = "success"
        elif any(s == "pdf_only" for s in statuses):
            status = "pdf_only"
        elif any(s == "not_found" for s in statuses):
            status = "failed"
        elif any(s == "failed" for s in statuses):
            status = "failed"
        else:
            status = "failed"

        raw_sum = sum(int(v.get("raw_size_bytes") or 0) for v in versions if v.get("raw_size_bytes"))
        clean_sum = sum(int(v.get("cleaned_size_bytes") or 0) for v in versions if v.get("cleaned_size_bytes"))
        tex_files = sum(int(v.get("num_tex_files") or 0) for v in versions if v.get("num_tex_files"))
        time_sum = sum(float(v.get("download_time_sec") or 0.0) for v in versions if v.get("download_time_sec"))
        has_tex = tex_files > 0 and status == "success"

        rows.append({
            "arxiv_id": _network_id(aid),
            "download_status": status,
            "has_tex": has_tex,
            "raw_size_bytes": raw_sum,
            "cleaned_size_bytes": clean_sum,
            "num_tex_files": tex_files,
            "download_time_sec": round(time_sum, 4),
        })

    fd, name = tempfile.mkstemp(prefix=f".{dest.name}.", dir=dest.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=DOWNLOAD_STATS_COLUMNS, extrasaction="raise")
            writer.writeheader()
            writer.writerows(rows)
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
