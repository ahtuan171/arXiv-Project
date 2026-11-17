"""Strict input contract for the integrated M1 Jupyter Book.

A recorded successful fetch does not prove that source/reference bytes were
SHA-256 verified. Fail closed on missing or contradictory evidence.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any

SOURCE_KEYS = (
    "tex_source_success", "pdf_only", "pending", "failed",
    "not_found", "conflicts", "missing_files",
)


def summary_path() -> Path:
    specified = os.environ.get("M1_SUMMARY_JSON", "").strip()
    if specified:
        return Path(specified).expanduser()
    return Path(__file__).resolve().parent / "_data" / "m1_summary.json"


def _count(value: Any, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{label}: expected nonnegative integer")
    return value


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label}: expected finite numeric value")
    return float(value)


def load_verified_summary(path: Path | None = None) -> dict[str, Any] | None:
    """Validate an audit-aware summary, not a claim of SHA-256 verification."""
    chosen = Path(path) if path is not None else summary_path()
    if not chosen.is_file():
        return None

    def reject_constant(constant: str) -> None:
        raise ValueError(f"Non-standard JSON constant: {constant}")

    with chosen.open("r", encoding="utf-8") as stream:
        info = json.load(stream, parse_constant=reject_constant)
    if not isinstance(info, dict) or info.get("schema_version") != 1:
        raise ValueError("M1 summary must have schema_version=1")
    scope, cov, status = (info.get(x) for x in ("scope", "coverage", "source_statuses"))
    if not all(isinstance(x, dict) for x in (scope, cov, status)):
        raise ValueError("Missing scope/coverage/source_statuses")

    assigned = _count(scope.get("assigned_target_papers"), "assigned_target_papers")
    actual = _count(scope.get("actual_collected_papers"), "actual_collected_papers")
    missing = _count(scope.get("missing_papers_count"), "missing_papers_count")
    if assigned == 0 or actual + missing != assigned:
        raise ValueError("Paper scope/counts disagree")
    if abs(_number(scope.get("coverage_pct"), "coverage_pct") - 100 * actual / assigned) > .01:
        raise ValueError("Paper scope percentage disagrees")
    metadata = _count(cov.get("valid_metadata_papers"), "valid_metadata_papers")
    refs = _count(cov.get("raw_reference_papers_crawled_or_resolved"), "raw_reference_papers_crawled_or_resolved")
    if metadata > actual or refs > actual:
        raise ValueError("Source counts exceed collected papers")

    expected = _count(cov.get("expected_versions"), "expected_versions")
    recorded = _count(cov.get("source_status_recorded_versions"), "source_status_recorded_versions")
    recorded2 = _count(status.get("status_recorded"), "status_recorded")
    states = {name: _count(status.get(name), "status." + name) for name in SOURCE_KEYS}
    if recorded > expected or recorded != recorded2 or recorded != sum(states.values()):
        raise ValueError("Version status totals disagree")
    if cov.get("tex_source_versions") != states["tex_source_success"] or cov.get("pdf_only_versions") != states["pdf_only"]:
        raise ValueError("TeX/PDF-only totals disagree with source status")
    pct = _number(cov.get("source_coverage_pct"), "source_coverage_pct")
    if expected == 0 or not 0 <= pct <= 100 or abs(pct - 100 * states["tex_source_success"] / expected) > .06:
        raise ValueError("Source coverage percentage disagrees")

    hash_status = cov.get("hash_verified_status")
    raw_verified = cov.get("raw_reference_papers_verified")
    if not isinstance(hash_status, str) or not hash_status:
        raise ValueError("Hash-verification provenance missing")
    if hash_status.startswith("not_verified") and raw_verified is not None:
        raise ValueError("Cannot claim verified references without SHA-256 evidence")
    if raw_verified is not None and _count(raw_verified, "raw_reference_papers_verified") > refs:
        raise ValueError("Verified reference count exceeds resolved count")

    bench = info.get("benchmark_evidence")
    batches = info.get("benchmark_batches")
    if not isinstance(bench, dict) or not isinstance(batches, list):
        raise ValueError("Benchmark evidence/batches missing")
    aggregated = bench.get("batch_aggregated_metrics")
    if aggregated is not None:
        if not isinstance(aggregated, dict) or _count(aggregated.get("batch_count"), "batch_count") != len(batches):
            raise ValueError("Benchmark batch count disagrees")
        members = aggregated.get("per_member")
        if not isinstance(members, dict) or sum(_count(v.get("batches"), "member.batches") for v in members.values()) != len(batches):
            raise ValueError("Per-member batch counts disagree")
        if _count(aggregated.get("total_papers"), "batch.total_papers") != actual:
            raise ValueError("Benchmark paper scope disagrees")
        if _number(aggregated.get("total_wall_time_sec"), "batch.total_wall_time_sec") < 0:
            raise ValueError("Negative runtime")
        if not str(aggregated.get("benchmark_type", "")).startswith("batch_aggregated"):
            raise ValueError("Unsubstantiated end-to-end benchmark claim")

    if not isinstance(info.get("missing_evidence"), list):
        raise ValueError("Missing-evidence list required")
    audit = info.get("data_integrity_audit")
    if not isinstance(audit, dict):
        raise ValueError("Missing data-integrity audit")
    for kind in ("papers_jsonl", "versions_manifest_jsonl"):
        section = audit.get(kind)
        if not isinstance(section, dict) or _count(section.get("malformed_json_lines"), kind + ".malformed") != 0:
            raise ValueError("Malformed/unaudited JSONL input: " + kind)
    return info
