"""Auditable Milestone 1 report summary (no data changes by default).

All source-coverage numbers come from the integrity audit, not manifest counts.
File-size and complete-publication figures require an up-to-date validated
version CSV. Actual CPU-only end-to-end resource metrics are *not* generated
from legacy benchmark files or invented from API download timings.

CLI:
    python -m src.scripts.build_m1_report --data-dir /path/to/data
    python -m src.scripts.build_m1_report --data-dir /path/to/data --export
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from src import utils
from src.crawl_references_module.raw_reference_collector import _done, _load_state
from src.scripts.audit_m1_integrity import audit_m1_integrity
from src.scripts.audit_missing_versions import _latest_jsonl, _valid_meta


def _read_stats(path: Path, status_field: str) -> dict:
    """Return latest row by arXiv ID with explicit source and scope."""
    result = {"present": path.is_file(), "rows": None, "unique_papers": None,
              "statuses": {}, "source": str(path.name)}
    if not path.is_file():
        return result
    latest = {}
    total = 0
    with path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        if not {"arxiv_id", status_field}.issubset(set(reader.fieldnames or [])):
            raise ValueError(f"Missing columns in {path.name}")
        for row in reader:
            pid = row.get("arxiv_id")
            if not utils.is_valid_arxiv_id(pid):
                raise ValueError(f"Invalid arXiv ID in {path.name}: {pid!r}")
            latest[utils.canonical_arxiv_id(pid)] = row
            total += 1
    result.update(rows=total, unique_papers=len(latest),
                  statuses=dict(Counter(str(row.get(status_field) or "unknown")
                                        for row in latest.values())))
    return result


def _verified_source_metrics(csv_path: Path) -> dict:
    """Compute raw-archive-vs-retained TeX sizes from verified version rows."""
    grouped = defaultdict(list)
    count, sum_raw, sum_clean, tex_count = 0, 0, 0, 0
    with csv_path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        needed = {"arxiv_id", "version", "status", "verification",
                  "raw_size_bytes", "cleaned_size_bytes", "num_tex_files"}
        if not needed.issubset(reader.fieldnames or []):
            raise ValueError("Version CSV missing source size/verification columns")
        for row in reader:
            grouped[row["arxiv_id"]].append(row["status"])
            if row["status"] == "success" and row["verification"] == "verified":
                try:
                    original = int(row["raw_size_bytes"])
                    retained = int(row["cleaned_size_bytes"])
                    texfiles = int(row["num_tex_files"])
                except (ValueError, TypeError) as exc:
                    raise ValueError("Verified version has nonnumeric size/count") from exc
                if min(original, retained) < 0 or texfiles < 1:
                    raise ValueError("Verified TeX has impossible size/file count")
                count += 1
                sum_raw += original
                sum_clean += retained
                tex_count += texfiles

    completed = sum(
        bool(statuses) and all(s in ("success", "pdf_only") for s in statuses)
        for statuses in grouped.values()
    )
    return {
        "verified_tex_versions_in_size_sample": count,
        "complete_source_publications": completed,
        "publications_in_version_csv": len(grouped),
        "mean_downloaded_archive_bytes_per_tex_version": round(sum_raw / count, 2) if count else None,
        "mean_retained_tex_bib_bytes_per_tex_version": round(sum_clean / count, 2) if count else None,
        "total_downloaded_archive_bytes_for_verified_tex": sum_raw,
        "total_retained_tex_bib_bytes_for_verified_tex": sum_clean,
        "mean_tex_files_per_verified_tex_version": round(tex_count / count, 3) if count else None,
        "size_comparison_note": (
            "Downloaded archive size is typically COMPRESSED; retained TeX/Bib size "
            "is extracted. These are not comparable uncompressed before/after figure sizes."
        ),
    }


def _raw_reference_sample(data_dir: Path) -> dict:
    """Only completed, hash-verified raw reference papers enter sample means."""
    if not (data_dir / "references_raw_ledger.jsonl").exists():
        return {"verified_reference_papers": 0, "mean_raw_references_per_verified_paper": None,
                "mean_arxiv_references_per_verified_paper": None,
                "sampling_note": "No verified raw-reference paper. Missing is not zero."}
    raw, events = _load_state(data_dir)
    n, refs, arxiv = 0, 0, 0
    for pid, event in events.items():
        entries = raw.get(pid, {})
        if not _done(event, entries):
            continue
        n += 1
        refs += len(entries)
        arxiv += sum(bool(row.get("ref_arxiv_id")) for row in entries.values())
    return {
        "verified_reference_papers": n,
        "mean_raw_references_per_verified_paper": round(refs / n, 3) if n else None,
        "mean_arxiv_references_per_verified_paper": round(arxiv / n, 3) if n else None,
        "sampling_note": (
            "Means use only ledger-verified S2 publication-level reference results. "
            "They are NOT averages over all assigned papers or per-revision citations."
        ),
    }


def _legacy_benchmark(path: Path) -> dict:
    """Historical Table 4 cannot certify a new version-aware pipeline run."""
    info = {"legacy_csv_exists": path.is_file(), "legacy_run_count": 0,
            "official_version_pipeline_metrics": None,
            "provenance": "legacy notebook pipeline only; not validated against vN downloader"}
    if path.is_file():
        with path.open(encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            if not {"total_papers", "total_wall_time_sec", "peak_ram_mb",
                    "average_ram_mb", "peak_disk_mb", "final_storage_mb"}.issubset(
                    set(reader.fieldnames or [])
            ):
                info["legacy_csv_warning"] = "Legacy benchmark schema does not match Table 4"
            else:
                info["legacy_run_count"] = sum(1 for _ in reader)
    return info


def build_m1_report(data_dir: str | Path, *, integrity_report: dict | None = None) -> dict:
    """Build repeatable read-only report from verified audit and existing CSVs."""
    root = Path(data_dir)
    if integrity_report is None:
        integrity = audit_m1_integrity(root)
    else:
        if Path(integrity_report.get("data_dir", "")).resolve() != root.resolve():
            raise ValueError("Integrity report is for a different data directory")
        integrity = integrity_report
    if "source_versions" not in integrity or "coverage" not in integrity:
        raise ValueError("Missing required integrity audit results")
    discovery = _read_stats(root / "csv" / "m1_discovery_stats.csv", "discovery_status")
    refs_legacy = _read_stats(root / "csv" / "m1_reference_stats.csv", "ref_status")

    snapshots = integrity.get("snapshots", {})
    source_sizes = None
    source_stats_reason = "m1_download_versions.csv missing or stale; export validated cell 2.3 first"
    if snapshots.get("download_versions_csv_current") is True:
        source_sizes = _verified_source_metrics(root / "csv" / "m1_download_versions.csv")
        source_stats_reason = None

    raw_sample = _raw_reference_sample(root)
    old_benchmark = _legacy_benchmark(root / "csv" / "m1_system_benchmark.csv")
    version = integrity["source_versions"]
    expected = integrity["coverage"]["expected_versions"]
    verified = integrity["coverage"]["verified_source_versions"]

    missing_evidence = []
    if not discovery["present"]:
        missing_evidence.append("Discovery statistics CSV")
    if source_sizes is None:
        missing_evidence.append("Current verified version CSV for size/publication statistics")
    if raw_sample["verified_reference_papers"] == 0:
        missing_evidence.append("Completed raw-reference sample for means")
    missing_evidence.extend([
        "CPU-only full-pipeline wall-clock timing with scope and session metadata",
        "CPU-only full-pipeline peak/mean process-tree RSS measurements",
        "CPU-only end-to-end peak scratch disk and final output storage",
        "Version-specific historical citation/metadata completeness assessment",
        "Course-required Jupyter Book publication in a SEPARATE report repository",
    ])
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "data_dir": str(root),
        "integrity_generated_at": integrity.get("generated_at"),
        "integrity_status": integrity["status"],
        "coverage": integrity["coverage"],
        "source_statuses": {
            "verified_success": version["verified_success"],
            "verified_pdf_only": version["verified_pdf_only"],
            "pending": version["pending"], "failed": version["failed"],
            "not_found": version["not_found"], "conflicts": version["conflicts"],
            "missing_files": version["missing_files"],
        },
        "discovery_legacy": discovery,
        "reference_legacy": refs_legacy,
        "raw_reference_sample": raw_sample,
        "source_file_statistics": source_sizes,
        "source_statistics_unavailable_reason": source_stats_reason,
        "published_reference_edges": integrity["published_references"],
        "benchmark_evidence": old_benchmark,
        "missing_evidence": missing_evidence,
        "submission_ready": False,
        "interpretation": (
            f"{verified} / {expected} version sources were verified at audit time. "
            "No official end-to-end benchmark is derived from legacy logs. "
            "Milestone 1 report readiness requires live CPU-only measurements, "
            "provenance and a separately published Jupyter Book."
        ),
    }


def export_m1_report(report: dict) -> Path:
    """Opt-in atomic JSON export; do not overwrite original data or statistics."""
    root = Path(report["data_dir"])
    folder = root / "reports"
    if folder.is_symlink() or (folder.exists() and not folder.is_dir()):
        raise ValueError(f"Unsafe reports directory: {folder}")
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "m1_summary.json"
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise ValueError(f"Unsafe output path: {target}")
    fd, tmp = tempfile.mkstemp(prefix=".m1-summary-", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, target)
    finally:
        Path(tmp).unlink(missing_ok=True)
    return target


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-dir", type=Path, required=True)
    p.add_argument("--export", action="store_true")
    args = p.parse_args()
    result = build_m1_report(args.data_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    if args.export:
        print(f"Report saved to {export_m1_report(result)}")
    else:
        print("READ ONLY: no files changed; pass --export to save m1_summary.json.")


if __name__ == "__main__":
    main()
