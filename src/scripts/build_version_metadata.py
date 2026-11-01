"""Produce a version-keyed metadata index from existing arXiv paper records.

This module never requests arXiv, rewrites papers.jsonl, or invents revision
history. In the current crawler, "revised_dates" usually contains only the
latest arXiv updated date, not all dates. Unknown per-version dates remain "".
Export is opt-in and atomically replaces ONLY metadata_versions.jsonl.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import date
from pathlib import Path

from src.recovery import atomic_write_jsonl
from src.scripts.audit_missing_versions import _latest_jsonl, _valid_meta, _versions


def _date(value: object) -> str:
    """Return an ISO date if supplied and valid; otherwise an empty string."""
    if not isinstance(value, str) or not value.strip():
        return ""
    raw = value.strip()
    try:
        parsed = date.fromisoformat(raw)
    except ValueError:
        return ""
    return parsed.isoformat()


def build_version_metadata(data_dir: str | Path, *, sample_size: int = 10) -> dict:
    """Read-only, all-month version records with explicit date provenance."""
    if sample_size < 0:
        raise ValueError("sample_size must be >= 0")
    root = Path(data_dir)
    path = root / "papers.jsonl"
    if not path.is_file():
        raise FileNotFoundError(f"Missing metadata snapshot: {path}")
    papers = _latest_jsonl(path)
    for paper_id, paper in _latest_jsonl(root / "papers.jsonl.journal").items():
        if _valid_meta(paper) or not _valid_meta(papers.get(paper_id)):
            papers[paper_id] = paper

    rows = []
    issues = []
    counts = Counter()
    for paper_id in sorted(papers):
        paper = papers[paper_id]
        if not _valid_meta(paper):
            continue
        counts["valid_papers"] += 1
        versions, issue = _versions(paper, None)
        if not versions:
            counts["invalid_version_metadata"] += 1
            if len(issues) < sample_size:
                issues.append(f"{paper_id}: {issue or 'no trustworthy versions'}")
            continue

        submitted = _date(paper.get("submission_date"))
        provided = paper.get("revised_dates")
        revisions = provided if isinstance(provided, list) else []
        revision_dates = [_date(value) for value in revisions]
        full_history = len(revision_dates) == len(versions) - 1
        latest_only = len(revision_dates) == 1 and len(versions) > 1
        if revisions and not (full_history or latest_only):
            counts["ambiguous_revised_dates"] += 1
            if len(issues) < sample_size:
                issues.append(f"{paper_id}: {len(revisions)} revised dates for {len(versions)} versions; unmapped")
        for index, version in enumerate(versions):
            version_date, date_source = "", "unknown"
            if index == 0 and submitted:
                version_date, date_source = submitted, "paper_submission_date"
            elif index > 0 and full_history and revision_dates[index - 1]:
                version_date, date_source = revision_dates[index - 1], "paper_revised_dates_ordered"
            elif index == len(versions) - 1 and latest_only and revision_dates[0]:
                version_date, date_source = revision_dates[0], "paper_latest_updated_date"

            # Title/authors/abstract belong to the publication-level snapshot,
            # NOT necessarily the historical contents of each prior revision.
            rows.append({
                "arxiv_id": paper_id,
                "version": version,
                "publication_metadata_id": paper_id,
                "submission_date": submitted,
                "version_date": version_date,
                "date_source": date_source,
                "date_known": bool(version_date),
                "paper_metadata_fetched_at": paper.get("fetched_at") or "",
            })
            counts["known_version_dates" if version_date else "unknown_version_dates"] += 1

    summary = {
        "valid_papers": counts["valid_papers"],
        "expected_versions": len(rows),
        "known_version_dates": counts["known_version_dates"],
        "unknown_version_dates": counts["unknown_version_dates"],
        "invalid_version_metadata": counts["invalid_version_metadata"],
        "ambiguous_revised_dates": counts["ambiguous_revised_dates"],
    }
    assert summary["known_version_dates"] + summary["unknown_version_dates"] == summary["expected_versions"]
    return {
        "data_dir": str(root),
        "summary": summary,
        "rows": rows,
        "sample_issues": issues[:sample_size],
        "limitations": (
            "Derived only from paper-level snapshots. Version labels are known; "
            "historical titles/abstracts/authors and intermediate revision dates "
            "are NOT reconstructed. Unknown dates remain empty."
        ),
    }


def export_version_metadata(report: dict) -> Path:
    """Opt-in atomic snapshot for BOTH months, without touching legacy data."""
    root = Path(report["data_dir"])
    path = root / "metadata_versions.jsonl"
    # Fail closed if a symlink is used in place of the new snapshot.
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError(f"Unsafe metadata output: {path}")
    atomic_write_jsonl(path, report["rows"])
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--sample-size", type=int, default=10)
    parser.add_argument("--export", action="store_true")
    args = parser.parse_args()
    report = build_version_metadata(args.data_dir, sample_size=args.sample_size)
    print(json.dumps(
        {"summary": report["summary"], "issues": report["sample_issues"],
         "limitations": report["limitations"]}, ensure_ascii=False, indent=2,
    ))
    if args.export:
        print(f"Wrote {len(report['rows'])} rows to {export_version_metadata(report)}")
    else:
        print("READ ONLY: pass --export to create metadata_versions.jsonl.")


if __name__ == "__main__":
    main()
