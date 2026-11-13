"""Small, dependency-free reader for verified M1 report inputs.

This module intentionally does not perform EDA or turn per-batch logs into an
official end-to-end benchmark. Duy's analysis notebook remains a separate
handoff. A missing input is allowed for a draft book; invalid inputs fail fast.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

SOURCE_KEYS = (
    "verified_success",
    "verified_pdf_only",
    "pending",
    "failed",
    "not_found",
    "conflicts",
    "missing_files",
)
COVERAGE_KEYS = (
    "valid_metadata_papers",
    "expected_versions",
    "verified_source_versions",
    "source_coverage_pct",
    "raw_reference_papers_verified",
)


def summary_path() -> Path:
    """Resolve an explicitly provided report, otherwise book/_data/M1 summary."""
    supplied = os.environ.get("M1_SUMMARY_JSON", "").strip()
    return Path(supplied).expanduser() if supplied else (
        Path(__file__).resolve().parent / "_data" / "m1_summary.json"
    )


def load_verified_summary(path: Path | None = None) -> dict[str, Any] | None:
    """Load the version-aware M1 summary; return None only when absent.

    The 'verified' label refers to internal audit-backed statistics, not to
    completeness of all versions or an official CPU-only benchmark.
    """
    target = Path(path) if path is not None else summary_path()
    if not target.is_file():
        return None

    with target.open("r", encoding="utf-8") as stream:
        report = json.load(stream)
    if not isinstance(report, dict) or report.get("schema_version") != 1:
        raise ValueError("m1_summary.json requires schema_version=1")

    coverage = report.get("coverage")
    statuses = report.get("source_statuses")
    if not isinstance(coverage, dict) or not isinstance(statuses, dict):
        raise ValueError("M1 summary is missing coverage/source_statuses")

    for name in COVERAGE_KEYS:
        val = coverage.get(name)
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            raise ValueError(f"Invalid coverage metric: {name}")
        if not (0 <= val <= (100 if name == "source_coverage_pct" else float("inf"))):
            raise ValueError(f"Out-of-range coverage metric: {name}")

    for name in SOURCE_KEYS:
        val = statuses.get(name)
        if isinstance(val, bool) or not isinstance(val, int) or val < 0:
            raise ValueError(f"Invalid source status count: {name}")

    expected = coverage["expected_versions"]
    verified = coverage["verified_source_versions"]
    if int(expected) != expected or int(verified) != verified:
        raise ValueError("Version counts must be integers")
    if verified > expected:
        raise ValueError("Verified versions exceed expected versions")
    if verified != statuses["verified_success"] + statuses["verified_pdf_only"]:
        raise ValueError("Verified version count disagrees with source status counts")
    if expected != sum(statuses[name] for name in SOURCE_KEYS):
        raise ValueError("Source status totals do not match expected versions")

    for field in ("benchmark_evidence", "missing_evidence", "raw_reference_sample"):
        if field not in report:
            raise ValueError(f"Missing M1 evidence field: {field}")
    if not isinstance(report["missing_evidence"], list):
        raise ValueError("missing_evidence must be a list")
    return report
