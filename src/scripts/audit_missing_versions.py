"""Light, read-only Milestone 1 version coverage audit.

Reads metadata, small CSV/JSONL status ledgers only. NEVER lists or stats
data/raw, downloads sources, changes metadata, or writes output by default.

The old downloader fetched the unversioned /src/{id} (latest version only).
A successful old publication-level row is a HINT, not verified version coverage.

Run in Colab BEFORE Fast Scratch switches config.DATA_DIR:
  from src.scripts.audit_missing_versions import audit_missing_versions
  report = audit_missing_versions(config.DEFAULT_DATA_DIR, ids=id_list)
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from pathlib import Path
from typing import Iterable

_VERSION_RE = re.compile(r"^v([1-9][0-9]*)$")
_SUFFIX_RE = re.compile(r"v[1-9][0-9]*$")


def _id(value: object) -> str:
    raw = str(value or "").strip()
    if raw.lower().startswith("arxiv:"):
        raw = raw[6:]
    raw = _SUFFIX_RE.sub("", raw)
    return raw.replace(".", "_").replace("/", "_").replace(":", "_")


def _jsonl_rows(path: Path):
    if not path.is_file():
        return
    with path.open(encoding="utf-8") as handle:
        for line_num, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"Malformed JSONL at {path}:{line_num}: {error}") from error
            if not isinstance(row, dict):
                raise ValueError(f"Non-object JSONL at {path}:{line_num}")
            yield row


_LATEST_JSONL_CACHE: dict[Path, tuple[int, int, dict[str, dict]]] = {}


def _latest_jsonl(path: Path) -> dict[str, dict]:
    if not path.is_file():
        return {}
    try:
        st = path.stat()
        cache_key = (st.st_mtime_ns, st.st_size)
    except OSError:
        cache_key = None
    if cache_key and path in _LATEST_JSONL_CACHE and _LATEST_JSONL_CACHE[path][:2] == cache_key:
        return dict(_LATEST_JSONL_CACHE[path][2])

    result = {}
    for row in _jsonl_rows(path):
        if row.get("arxiv_id"):
            result[_id(row["arxiv_id"])] = row

    if cache_key:
        _LATEST_JSONL_CACHE[path] = (cache_key[0], cache_key[1], result)
    return dict(result)


def _csv_records(path: Path, *, required: bool = False) -> dict[str, dict]:
    if not path.is_file():
        if required:
            raise FileNotFoundError(f"Required statistics file not found: {path}")
        return {}
    rows: dict[str, dict] = {}
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if row.get("arxiv_id"):
                rows[_id(row["arxiv_id"])] = row
    return rows


def _latest_stats(csv_dir: Path, name: str, *, required: bool = False) -> dict[str, dict]:
    """Use report CSV as baseline, overlay later journal events for same paper."""
    out = _csv_records(csv_dir / (name + ".csv"), required=required)
    out.update(_latest_jsonl(csv_dir / (name + ".jsonl")))
    return out


def _valid_meta(row: dict | None) -> bool:
    return bool(row and row.get("title") and not row.get("error"))


def _versions(paper: dict, discovered: dict | None) -> tuple[list[str], str | None]:
    reported = paper.get("versions")
    if isinstance(reported, list) and reported:
        numbers = []
        for value in reported:
            m = _VERSION_RE.fullmatch(str(value))
            if m is None:
                return [], "invalid version label in metadata"
            numbers.append(int(m.group(1)))
        if len(numbers) != len(set(numbers)):
            return [], "duplicate versions in metadata"
        expected = list(range(1, max(numbers) + 1))
        if sorted(numbers) != expected:
            return [], "metadata versions are not consecutive from v1"
        return [f"v{n}" for n in expected], None
    # Old records without versions can use discovery count as an *inference*.
    count = (discovered or {}).get("num_versions")
    try:
        count_n = int(count)
    except (TypeError, ValueError):
        count_n = 0
    if count_n > 0:
        return [f"v{n}" for n in range(1, count_n + 1)], "version list inferred from discovery CSV"
    return [], "no trustworthy version count"


def _legacy_status(stats: dict | None, ledger: dict | None) -> str:
    """Ledger's latest recovery status takes precedence over snapshot CSV."""
    if ledger and ledger.get("status"):
        status = str(ledger["status"])
        return {
            "ok": "success", "pdf_only": "pdf_only",
            "error": "failed", "blocked": "failed",
        }.get(status, status)
    return str((stats or {}).get("download_status") or "unrecorded")


def audit_missing_versions(
    data_dir: str | Path,
    *,
    ids: Iterable[str] | None = None,
    sample_size: int = 12,
) -> dict:
    """Return summary and candidate worklist in memory without touching raw/."""
    root = Path(data_dir)
    paper_path = root / "papers.jsonl"
    if not paper_path.is_file():
        raise FileNotFoundError(f"Missing metadata snapshot: {paper_path}")
    if sample_size < 0:
        raise ValueError("sample_size must be >= 0")

    # Load existing metadata + pending successful journal entries.
    papers = _latest_jsonl(paper_path)
    for paper_id, row in _latest_jsonl(root / "papers.jsonl.journal").items():
        if _valid_meta(row) or not _valid_meta(papers.get(paper_id)):
            papers[paper_id] = row
    csv_dir = root / "csv"
    discovered = _latest_stats(csv_dir, "m1_discovery_stats", required=True)
    downloads = _latest_stats(csv_dir, "m1_download_stats")
    download_ledger = _latest_jsonl(csv_dir / "m1_download_stats.ledger.jsonl")

    requested = {_id(value) for value in ids} if ids is not None else set(discovered) | set(papers)
    valid = sorted(pid for pid in requested if _valid_meta(papers.get(pid)))
    counters = Counter()
    legacy_statuses = Counter()
    worklist: list[dict] = []
    warnings: list[str] = []

    for paper_id in valid:
        versions, note = _versions(papers[paper_id], discovered.get(paper_id))
        if note:
            if len(warnings) < 30:
                warnings.append(f"{paper_id}: {note}")
            counters["metadata_version_notes"] += 1
        if not versions:
            counters["unknown_version_papers"] += 1
            continue
        count_from_discovery = (discovered.get(paper_id) or {}).get("num_versions")
        if count_from_discovery not in (None, "") and str(len(versions)) != str(count_from_discovery):
            counters["discovery_metadata_version_mismatch"] += 1
            if len(warnings) < 30:
                warnings.append(f"{paper_id}: metadata has {len(versions)} versions, discovery says {count_from_discovery}")
        latest = versions[-1]
        counters["expected_versions"] += len(versions)
        counters["multi_version_papers"] += int(len(versions) > 1)
        old_status = _legacy_status(downloads.get(paper_id), download_ledger.get(paper_id))
        legacy_statuses[old_status] += 1
        for version in versions:
            if version == latest and old_status in {"success", "pdf_only"}:
                counters["legacy_latest_unverified"] += 1
                # No filesystem check: cannot assert an existing version is valid.
                continue
            reason = "earlier_version_not_recorded" if version != latest else "latest_missing_or_failed"
            counters["candidate_missing_versions"] += 1
            if version == latest:
                counters["latest_needs_attention"] += 1
            worklist.append({
                "arxiv_id": paper_id,
                "version": version,
                "versioned_id": paper_id.replace("_", ".", 1) + version,
                "reason": reason,
                "legacy_publication_status": old_status,
            })

    summary = {
        "requested_ids": len(requested),
        "valid_metadata_papers": len(valid),
        "no_valid_metadata_ids": len(requested) - len(valid),
        "expected_versions": counters["expected_versions"],
        "multi_version_papers": counters["multi_version_papers"],
        "legacy_latest_unverified": counters["legacy_latest_unverified"],
        "candidate_missing_versions": counters["candidate_missing_versions"],
        "earlier_versions_candidates": counters["candidate_missing_versions"] - counters["latest_needs_attention"],
        "latest_needs_attention": counters["latest_needs_attention"],
        "unknown_version_papers": counters["unknown_version_papers"],
        "discovery_metadata_version_mismatch": counters["discovery_metadata_version_mismatch"],
        "metadata_version_notes": counters["metadata_version_notes"],
        "legacy_download_statuses": dict(sorted(legacy_statuses.items())),
    }
    return {
        "data_dir": str(root),
        "summary": summary,
        "sample_candidates": worklist[:sample_size],
        "candidate_worklist": worklist,
        "warnings": warnings,
        "limitations": [
            "No directory scan or file hash verification of raw/ was performed.",
            "A successful publication-level download is not proof its latest source remains present.",
            "Candidate missing versions are planning hints, not verified download targets.",
            "Run after stopping active writers; before any production writes, verify each target.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--start-id")
    parser.add_argument("--end-id")
    parser.add_argument("--sample-size", type=int, default=12)
    args = parser.parse_args()
    if (args.start_id is None) != (args.end_id is None):
        parser.error("--start-id and --end-id must be supplied together")
    requested = None
    if args.start_id:
        from src.utils import parse_arxiv_range
        requested = parse_arxiv_range(args.start_id, args.end_id)
    result = audit_missing_versions(args.data_dir, ids=requested, sample_size=args.sample_size)
    for key in ("summary", "sample_candidates", "warnings", "limitations"):
        print(json.dumps({key: result[key]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
