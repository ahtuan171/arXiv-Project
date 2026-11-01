"""Safe, repeatable *manual* backups of each member's Milestone 1 data directory.

This module does not run when imported, and never modifies its input directory.
It copies the entire input tree (including raw/ and csv/) into a unique dated
backup in a sibling backup root, records per-file SHA-256, and checks the copy.

Use only while the crawler and other writers are stopped.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path

REQUIRED_FILES = ("papers.jsonl", "references.jsonl")
REQUIRED_DIRS = ("raw", "csv")
MANIFEST = "backup_manifest.json"
STATUS = "backup_status.json"


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _inventory(root: Path) -> dict[str, tuple[int, int]]:
    """Reject symlinks/special files, including symlink directories."""
    result: dict[str, tuple[int, int]] = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise ValueError(f"Backup refuses symlink: {relative}")
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            raise ValueError(f"Backup refuses non-regular file: {relative}")
        file_stat = path.stat()
        result[relative] = (file_stat.st_size, file_stat.st_mtime_ns)
    return result


def _validate(source_dir: Path, backup_root: Path) -> None:
    if not source_dir.is_dir():
        raise FileNotFoundError(f"Dataset directory does not exist: {source_dir}")
    if backup_root == source_dir or source_dir in backup_root.parents:
        raise ValueError("Backup destination must not be inside the source data directory")
    for name in REQUIRED_FILES:
        if not (source_dir / name).is_file():
            raise FileNotFoundError(f"Required dataset file missing: {source_dir / name}")
    for name in REQUIRED_DIRS:
        if not (source_dir / name).is_dir():
            raise FileNotFoundError(f"Required dataset folder missing: {source_dir / name}")


def backup_plan(source_dir: str | Path, backup_root: str | Path) -> dict:
    """Scan without creating files or folders; useful for Colab preflight."""
    source = Path(source_dir).expanduser().resolve()
    destination = Path(backup_root).expanduser().resolve()
    _validate(source, destination)
    entries = _inventory(source)
    return {
        "source_dir": str(source),
        "backup_root": str(destination),
        "file_count": len(entries),
        "total_bytes": sum(size for size, _ in entries.values()),
        "raw_file_count": sum(name.startswith("raw/") for name in entries),
        "csv_file_count": sum(name.startswith("csv/") for name in entries),
    }


def verify_backup(backup_dir: str | Path, source_dir: str | Path | None = None) -> dict:
    """Re-hash every copied file; optionally confirm the original is unchanged."""
    dest = Path(backup_dir).expanduser().resolve()
    manifest = json.loads((dest / MANIFEST).read_text(encoding="utf-8"))
    errors: list[str] = []
    expected = {row["path"]: row for row in manifest["files"]}
    actual = set(_inventory(dest)) - {MANIFEST, STATUS}
    if actual != set(expected):
        errors.append(f"Backup inventory mismatch: missing={sorted(set(expected) - actual)[:5]}, "
                      f"extra={sorted(actual - set(expected))[:5]}")
    source = Path(source_dir).expanduser().resolve() if source_dir is not None else None
    for rel, item in expected.items():
        target = dest / rel
        if not target.is_file() or target.is_symlink():
            errors.append(f"Missing/non-file backup: {rel}")
            continue
        if target.stat().st_size != item["bytes"] or _hash_file(target) != item["sha256"]:
            errors.append(f"Backup content mismatch: {rel}")
        if source is not None:
            original = source / rel
            if not original.is_file() or original.is_symlink() or _hash_file(original) != item["sha256"]:
                errors.append(f"Source changed or missing: {rel}")
    if source is not None and set(_inventory(source)) != set(expected):
        errors.append("Source file inventory changed during backup")
    return {"ok": not errors, "checked_files": len(expected), "issues": errors}


def backup_dataset(source_dir: str | Path, backup_root: str | Path) -> dict:
    """Create a new snapshot, never overwrite backups, and verify all file hashes.

    The working folder stays .incomplete if copying/checks fail. A successful
    folder is renamed to m1_baseline_... only after full SHA-256 verification.
    """
    plan = backup_plan(source_dir, backup_root)
    source = Path(plan["source_dir"])
    root = Path(plan["backup_root"])
    before = _inventory(source)
    root.mkdir(parents=True, exist_ok=True)
    unique = datetime.now(timezone.utc).strftime("m1_baseline_%Y%m%dT%H%M%S_%fZ") + "_" + uuid.uuid4().hex[:8]
    target = root / unique
    staging = root / ("." + unique + ".incomplete")
    staging.mkdir(exist_ok=False)
    (staging / STATUS).write_text(json.dumps({"status": "incomplete"}), encoding="utf-8")
    records = []
    try:
        for relative in sorted(before):
            src_file = source / relative
            dst_file = staging / relative
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dst_file)
            records.append({
                "path": relative,
                "bytes": before[relative][0],
                "sha256": _hash_file(src_file),
            })
        if _inventory(source) != before:
            raise RuntimeError("Dataset changed while copying; stop crawler/writers and retry")
        manifest = {
            "schema_version": 1,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "source_dir": str(source),
            "file_count": len(records),
            "total_bytes": sum(row["bytes"] for row in records),
            "files": records,
        }
        (staging / MANIFEST).write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        verification = verify_backup(staging, source)
        if not verification["ok"]:
            raise RuntimeError("Backup verification failed: " + "; ".join(verification["issues"][:5]))
        (staging / STATUS).write_text(json.dumps({"status": "verified", "file_count": len(records)}), encoding="utf-8")
        staging.rename(target)
        return {"status": "verified", "backup_dir": str(target), **plan, "verification": verification}
    except Exception:
        # Keep the incomplete folder and marker for inspection. Never delete input.
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--backup-root", type=Path, required=True)
    parser.add_argument("--run", action="store_true", help="Perform backup (default: preflight only)")
    args = parser.parse_args()
    result = backup_dataset(args.data_dir, args.backup_root) if args.run else backup_plan(args.data_dir, args.backup_root)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
