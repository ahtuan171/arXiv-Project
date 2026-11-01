"""Read-only reconciliation of unversioned legacy TeX/Bib against verified vN sources.

A legacy /src/<id> download has no revision provenance. An exact file-set match
proves identical extracted source BYTES to a previously verified vN, NOT that
the legacy download was fetched from that versioned URL.

No files are moved, copied, deleted, or updated. Never use this report as
automatic permission to skip a version-aware download. Hash checks are
intentionally restricted to an explicit bounded set of paper IDs by default.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from collections import Counter
from pathlib import Path
from typing import Iterable

from src.download_module.version_downloader import _paper_id, _status_record
from src.scripts.audit_missing_versions import _latest_jsonl, _valid_meta, _versions

_VERSION_FOLDER = re.compile(r"^v[1-9][0-9]*$")
_SOURCE_SUFFIXES = {".tex", ".bib"}
_LEGACY_STATUS_FILE = ".source-status.json"


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _legacy_fingerprint(folder: Path) -> tuple[str, tuple, str]:
    """Inspect only legacy files, excluding vN directories and the old status marker.

    Treat unexpected files and symlinks as unsafe; never silently establish a
    match from a partially inspected source tree.
    """
    if folder.is_symlink() or (folder.exists() and not folder.is_dir()):
        return "untrusted", (), "legacy path is not a regular directory"
    if not folder.is_dir():
        return "no_legacy", (), ""
    records = []
    try:
        for current_dir, names, filenames in os.walk(folder, topdown=True, followlinks=False):
            parent = Path(current_dir)
            # At the top level, drop ONLY version directories from comparison.
            if parent == folder:
                names[:] = [name for name in names if not _VERSION_FOLDER.fullmatch(name)]
            for name in names:
                child = parent / name
                if child.is_symlink():
                    return "untrusted", (), f"legacy symlink directory: {child.relative_to(folder)}"
            for name in filenames:
                path = parent / name
                rel = path.relative_to(folder).as_posix()
                if parent == folder and name == _LEGACY_STATUS_FILE:
                    if path.is_symlink() or not path.is_file():
                        return "untrusted", (), "unsafe legacy status marker"
                    continue
                if path.is_symlink() or not path.is_file():
                    return "untrusted", (), f"unsafe legacy file: {rel}"
                if path.suffix.lower() not in _SOURCE_SUFFIXES:
                    return "untrusted", (), f"unexpected legacy file: {rel}"
                records.append((rel, path.stat().st_size, _digest(path)))
    except OSError as exc:
        return "untrusted", (), f"legacy I/O error: {exc}"

    if not records:
        marker = folder / _LEGACY_STATUS_FILE
        if marker.is_file():
            try:
                payload = json.loads(marker.read_text(encoding="utf-8"))
                if isinstance(payload, dict) and payload.get("status") == "pdf_only":
                    return "legacy_pdf_only", (), "legacy PDF-only is not revision proof"
            except (OSError, ValueError, UnicodeError):
                return "untrusted", (), "invalid legacy status marker"
        return "no_legacy", (), ""
    if not any(rel.lower().endswith(".tex") and size > 0 for rel, size, _ in records):
        return "untrusted", (), "legacy source contains no nonempty TeX"
    return "has_source", tuple(sorted(records)), ""


def audit_legacy_matches(
    data_dir: str | Path,
    *,
    ids: Iterable[str] | None = None,
    max_papers: int = 20,
    sample_size: int = 10,
) -> dict:
    """Compare legacy source sets with ONLY verified vN directories.

    max_papers=0 allows all requested papers (use deliberately; hashing Drive
    may be expensive). Never assumes legacy means "latest revision".
    """
    if max_papers < 0 or sample_size < 0:
        raise ValueError("max_papers and sample_size must be >= 0")
    root = Path(data_dir)
    meta = root / "papers.jsonl"
    if not meta.is_file():
        raise FileNotFoundError(f"Missing metadata: {meta}")
    papers = _latest_jsonl(meta)
    for paper_id, row in _latest_jsonl(root / "papers.jsonl.journal").items():
        if _valid_meta(row) or not _valid_meta(papers.get(paper_id)):
            papers[paper_id] = row

    requested = sorted({_paper_id(value) for value in ids}) if ids is not None else sorted(papers)
    requested_total = len(requested)
    if max_papers:
        requested = requested[:max_papers]
    result_rows = []
    stats = Counter()
    for paper_id in requested:
        paper = papers.get(paper_id)
        if not _valid_meta(paper):
            stats["no_metadata"] += 1
            continue
        versions, issue = _versions(paper, None)
        if not versions:
            stats["invalid_version_metadata"] += 1
            continue
        status, fingerprint, error = _legacy_fingerprint(root / "raw" / paper_id)
        matches = []
        compared = 0
        if status == "has_source":
            for version in versions:
                record = _status_record(root / "raw", paper_id, version)
                if not record or record["status"] != "success":
                    continue  # PDF-only has no TeX, unverified vN cannot prove match.
                compared += 1
                marker = record["marker"]
                signature = tuple(sorted(
                    (row["path"], row["bytes"], row["sha256"]) for row in marker["files"]
                ))
                if signature == fingerprint:
                    matches.append(version)
            status = ("exact_match" if len(matches) == 1 else
                      "ambiguous_match" if len(matches) > 1 else
                      "different" if compared else "no_verified_version")
        stats[status] += 1
        result_rows.append({
            "arxiv_id": paper_id,
            "status": status,
            "matched_versions": matches,
            "verified_versions_compared": compared,
            "legacy_source_files": len(fingerprint),
            "reason": error,
        })
    summary = {
        "requested_papers": requested_total,
        "examined_papers": len(requested),
        "papers_with_valid_metadata": len(result_rows),
        "exact_matches": stats["exact_match"],
        "ambiguous_matches": stats["ambiguous_match"],
        "different": stats["different"],
        "no_verified_version": stats["no_verified_version"],
        "no_legacy": stats["no_legacy"],
        "legacy_pdf_only": stats["legacy_pdf_only"],
        "untrusted": stats["untrusted"],
        "no_metadata": stats["no_metadata"],
        "invalid_version_metadata": stats["invalid_version_metadata"],
    }
    return {
        "data_dir": str(root),
        "summary": summary,
        "sample_rows": result_rows[:sample_size],
        "rows": result_rows,
        "warning": (
            "Byte-identical source is not proof of historical fetch URL/version. "
            "This audit does NOT skip downloads, migrate legacy folders, or write data."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--max-papers", type=int, default=20)
    parser.add_argument("--sample-size", type=int, default=10)
    args = parser.parse_args()
    report = audit_legacy_matches(
        args.data_dir, max_papers=args.max_papers, sample_size=args.sample_size
    )
    print(json.dumps(
        {"summary": report["summary"], "sample_rows": report["sample_rows"],
         "warning": report["warning"]},
        indent=2, ensure_ascii=False,
    ))


if __name__ == "__main__":
    main()
