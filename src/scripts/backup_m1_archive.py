"""Fast, manually triggered Milestone 1 backups for Colab + mounted Google Drive.

Create a single compressed archive on local scratch, copy it to Drive once,
verify SHA-256, then publish archive + manifest. Production data is read-only.
Call only after all writers (other Colab notebooks/crawlers) have stopped.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import stat
import tarfile
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from src.scripts.backup_m1 import _hash_file, _inventory, backup_plan


class _HashingReader:
    """Compute per-file SHA-256 while tarfile consumes the source only once."""

    def __init__(self, raw):
        self.raw = raw
        self.digest = hashlib.sha256()
        self.count = 0

    def read(self, size=-1):
        data = self.raw.read(size)
        self.digest.update(data)
        self.count += len(data)
        return data


def _verify_members(archive: Path, entries: list[dict], directories: list[str]) -> None:
    expected = {item["path"]: item for item in entries}
    seen_files = set()
    seen_dirs = set()
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar:
            if member.isdir():
                name = member.name.rstrip("/")
                if name not in directories or name in seen_dirs:
                    raise RuntimeError(f"Unexpected/duplicate directory in archive: {name}")
                seen_dirs.add(name)
                continue
            if not member.isfile() or member.name not in expected or member.name in seen_files:
                raise RuntimeError(f"Unexpected/duplicate file in archive: {member.name}")
            item = expected[member.name]
            if member.size != item["bytes"]:
                raise RuntimeError(f"Wrong archived size: {member.name}")
            stream = tar.extractfile(member)
            if stream is None:
                raise RuntimeError(f"Cannot read archived file: {member.name}")
            digest = hashlib.sha256()
            with stream:
                for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                    digest.update(block)
            if digest.hexdigest() != item["sha256"]:
                raise RuntimeError(f"Wrong archived SHA-256: {member.name}")
            seen_files.add(member.name)
    if seen_files != set(expected) or seen_dirs != set(directories):
        raise RuntimeError("Archive members do not match source inventory")


def verify_backup_archive(archive_path: str | Path, *, deep: bool = False) -> dict:
    """Check a published backup and its sidecar manifest (deep checks file hashes)."""
    archive = Path(archive_path).expanduser().resolve()
    manifest_path = archive.with_name(archive.name.removesuffix(".tar.gz") + ".manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    errors = []
    if manifest.get("status") != "verified":
        errors.append("Manifest does not record a completed verification")
    if not archive.is_file() or archive.stat().st_size != manifest.get("archive_bytes"):
        errors.append("Archive missing or wrong size")
    elif _hash_file(archive) != manifest.get("archive_sha256"):
        errors.append("Archive checksum mismatch")
    elif deep:
        try:
            _verify_members(archive, manifest["files"], manifest["directories"])
        except (OSError, RuntimeError, tarfile.TarError) as exc:
            errors.append(f"Archive contents mismatch: {exc}")
    return {"ok": not errors, "archive": str(archive), "file_count": manifest.get("file_count"), "issues": errors}


def backup_dataset_archive(
    source_dir: str | Path,
    backup_root: str | Path,
    *,
    temp_dir: str | Path | None = None,
    progress_every: int = 200,
) -> dict:
    """Create a verified .tar.gz snapshot without copying raw files individually to Drive.

    A .tar.gz.incomplete file remains on failure; it must never be restored.
    The scratch archive is temporary and removed automatically after exit.
    """
    if progress_every < 1:
        raise ValueError("progress_every must be >= 1")
    plan = backup_plan(source_dir, backup_root)
    source = Path(plan["source_dir"])
    backup_root = Path(plan["backup_root"])
    scratch = Path(temp_dir).expanduser().resolve() if temp_dir else Path(tempfile.gettempdir()).resolve()
    if not scratch.is_dir() or source == scratch or source in scratch.parents:
        raise ValueError("Local scratch must exist outside the source data directory")
    if backup_root == scratch or backup_root in scratch.parents:
        raise ValueError("Scratch cannot be the backup destination")
    if str(scratch).startswith("/content/drive/"):
        raise ValueError("Choose local /content scratch, not mounted Google Drive")
    inventory = _inventory(source)
    directory_names = sorted(
        p.relative_to(source).as_posix() for p in source.rglob("*") if p.is_dir()
    )
    required = int(plan["total_bytes"] * 1.05) + (len(inventory) + len(directory_names)) * 1024 + 32 * 1024 * 1024
    free = shutil.disk_usage(scratch).free
    if free < required:
        raise RuntimeError(
            f"Not enough LOCAL scratch space: need ~{required / 1024**3:.2f} GiB, "
            f"available {free / 1024**3:.2f} GiB. Clear /content or choose another temp_dir."
        )

    backup_root.mkdir(parents=True, exist_ok=True)
    stem = datetime.now(timezone.utc).strftime("m1_baseline_%Y%m%dT%H%M%S_%fZ") + "_" + uuid.uuid4().hex[:8]
    final = backup_root / f"{stem}.tar.gz"
    incomplete = backup_root / f".{stem}.tar.gz.incomplete"
    manifest_path = backup_root / f"{stem}.manifest.json"
    processed = 0
    start = time.monotonic()
    records = []
    with tempfile.TemporaryDirectory(prefix="m1-archive-", dir=scratch) as work:
        local_archive = Path(work) / "snapshot.tar.gz"
        with tarfile.open(local_archive, "w:gz", compresslevel=1) as tar:
            for relative in directory_names:
                info = tarfile.TarInfo(relative + "/")
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tar.addfile(info)
            for i, (relative, (original_size, original_mtime_ns)) in enumerate(sorted(inventory.items()), 1):
                filename = source / relative
                file_stat = filename.lstat()
                if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size != original_size or file_stat.st_mtime_ns != original_mtime_ns:
                    raise RuntimeError(f"Source changed before archiving: {relative}")
                header = tarfile.TarInfo(relative)
                header.size = original_size
                header.mode = stat.S_IMODE(file_stat.st_mode)
                header.mtime = int(file_stat.st_mtime)
                with filename.open("rb") as raw:
                    reader = _HashingReader(raw)
                    tar.addfile(header, reader)
                stat_after = filename.lstat()
                if (reader.count != original_size or stat_after.st_size != original_size
                        or stat_after.st_mtime_ns != original_mtime_ns):
                    raise RuntimeError(f"Source changed during archiving: {relative}")
                records.append({"path": relative, "bytes": original_size, "sha256": reader.digest.hexdigest()})
                processed += original_size
                if i % progress_every == 0 or i == len(inventory):
                    print(f"[Backup] Packed {i:,}/{len(inventory):,} files "
                          f"({processed / 1024**2:,.1f} MiB); elapsed {time.monotonic() - start:.0f}s", flush=True)
        if _inventory(source) != inventory:
            raise RuntimeError("Source inventory changed during backup; stop all writers")
        print("[Backup] Checking local archive file-by-file ...", flush=True)
        _verify_members(local_archive, records, directory_names)
        expected_sha = _hash_file(local_archive)
        expected_size = local_archive.stat().st_size
        print(f"[Backup] Uploading one {expected_size / 1024**2:,.1f} MiB archive to Drive ...", flush=True)
        with local_archive.open("rb") as src, incomplete.open("xb") as dest:
            shutil.copyfileobj(src, dest, length=8 * 1024 * 1024)
        print("[Backup] Verifying uploaded archive SHA-256 ...", flush=True)
        if incomplete.stat().st_size != expected_size or _hash_file(incomplete) != expected_sha:
            raise RuntimeError(f"Drive copy failed integrity check; incomplete backup kept: {incomplete}")
        manifest = {
            "schema_version": 2,
            "status": "verified",
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "source_dir": str(source),
            "archive_bytes": expected_size,
            "archive_sha256": expected_sha,
            "file_count": len(records),
            "total_source_bytes": plan["total_bytes"],
            "directories": directory_names,
            "files": records,
        }
        incomplete.rename(final)
        manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return {
        "status": "verified",
        "archive": str(final),
        "manifest": str(manifest_path),
        "file_count": len(records),
        "source_bytes": plan["total_bytes"],
        "archive_bytes": expected_size,
        "archive_sha256": expected_sha,
        "elapsed_sec": round(time.monotonic() - start, 1),
    }
