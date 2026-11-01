"""Fast, manually triggered Milestone 1 backups for Colab / Google Drive.

A preflight first scans the source read-only and shows *measured* file counts.
Backup streams one tar.gz to local scratch, checks each archived file,
copies one archive to Drive with progress, and SHA-256-verifies the uploaded
file before writing a verified manifest. Source data is never modified.

IMPORTANT: Stop crawlers and all other writers before scanning or backup.
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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from src.scripts.backup_m1 import _hash_file, _validate

_CHUNK_SIZE = 8 * 1024 * 1024


def _progress(stage: str, count: int, total: int | None, processed: int, started: float) -> None:
    elapsed = time.monotonic() - started
    suffix = f"/{total:,} ({count / total * 100:.1f}%)" if total else ""
    print(
        f"[Backup / {stage}] {count:,}{suffix} files, "
        f"{processed / 1024**2:,.1f} MiB, {elapsed:.0f}s elapsed",
        flush=True,
    )


@dataclass
class BackupScan:
    source: Path
    backup_root: Path
    files: dict[str, tuple[int, int]]  # relative path => (bytes, mtime_ns)
    directories: list[str]

    def summary(self) -> dict:
        return {
            "source_dir": str(self.source),
            "backup_root": str(self.backup_root),
            "file_count": len(self.files),
            "total_bytes": sum(item[0] for item in self.files.values()),
            "raw_file_count": sum(name.startswith("raw/") for name in self.files),
            "csv_file_count": sum(name.startswith("csv/") for name in self.files),
        }


def scan_backup_source(source_dir: str | Path, backup_root: str | Path, *, progress_every: int = 200) -> BackupScan:
    """Read-only preflight; print live counts even before total count is known."""
    if progress_every < 1:
        raise ValueError("progress_every must be >= 1")
    source = Path(source_dir).expanduser().resolve()
    destination = Path(backup_root).expanduser().resolve()
    _validate(source, destination)
    files: dict[str, tuple[int, int]] = {}
    directories = []
    total_bytes = 0
    start = last_log = time.monotonic()
    print(f"[Backup / Preflight] Đang quét dữ liệu: {source}", flush=True)
    for path in source.rglob("*"):
        rel = path.relative_to(source).as_posix()
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(f"Backup refuses symlink: {rel}")
        if stat.S_ISDIR(info.st_mode):
            directories.append(rel)
            continue
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"Backup refuses non-regular file: {rel}")
        files[rel] = (info.st_size, info.st_mtime_ns)
        total_bytes += info.st_size
        now = time.monotonic()
        if len(files) % progress_every == 0 or now - last_log >= 5:
            _progress("Preflight", len(files), None, total_bytes, start)
            last_log = now
    _progress("Preflight HOÀN TẤT", len(files), len(files), total_bytes, start)
    return BackupScan(source, destination, files, sorted(directories))


class _HashingReader:
    def __init__(self, raw):
        self.raw = raw
        self.digest = hashlib.sha256()
        self.count = 0

    def read(self, size=-1):
        data = self.raw.read(size)
        self.digest.update(data)
        self.count += len(data)
        return data


def _verify_members(archive: Path, entries: list[dict], directories: list[str], *, progress_every: int = 200) -> None:
    expected = {row["path"]: row for row in entries}
    seen_files: set[str] = set()
    seen_dirs: set[str] = set()
    nbytes = 0
    start = last_log = time.monotonic()
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
                for block in iter(lambda: stream.read(_CHUNK_SIZE), b""):
                    digest.update(block)
            if digest.hexdigest() != item["sha256"]:
                raise RuntimeError(f"Wrong archived SHA-256: {member.name}")
            seen_files.add(member.name)
            nbytes += member.size
            now = time.monotonic()
            if len(seen_files) % progress_every == 0 or now - last_log >= 5:
                _progress("Verify nội dung", len(seen_files), len(expected), nbytes, start)
                last_log = now
    if seen_files != set(expected) or seen_dirs != set(directories):
        raise RuntimeError("Archive members do not match source inventory")
    _progress("Verify nội dung HOÀN TẤT", len(seen_files), len(expected), nbytes, start)


def _sha256_progress(path: Path, *, label: str) -> str:
    size = path.stat().st_size
    hasher = hashlib.sha256()
    read = 0
    last_log = time.monotonic()
    start = last_log
    with path.open("rb") as stream:
        while chunk := stream.read(_CHUNK_SIZE):
            hasher.update(chunk)
            read += len(chunk)
            now = time.monotonic()
            if now - last_log >= 5 or read == size:
                print(f"[Backup / {label}] {read / 1024**2:,.1f}/{size / 1024**2:,.1f} MiB "
                      f"({100 * read / max(size, 1):.1f}%), {now - start:.0f}s elapsed", flush=True)
                last_log = now
    return hasher.hexdigest()


def verify_backup_archive(archive_path: str | Path, *, deep: bool = False) -> dict:
    """Verify published backup by archive digest; optional full member validation."""
    archive = Path(archive_path).expanduser().resolve()
    manifest_file = archive.with_name(archive.name.removesuffix(".tar.gz") + ".manifest.json")
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    issues = []
    if manifest.get("status") != "verified":
        issues.append("Manifest does not record a completed verification")
    if not archive.is_file() or archive.stat().st_size != manifest.get("archive_bytes"):
        issues.append("Archive missing or wrong size")
    elif _sha256_progress(archive, label="Kiểm tra backup") != manifest.get("archive_sha256"):
        issues.append("Archive checksum mismatch")
    elif deep:
        try:
            _verify_members(archive, manifest["files"], manifest["directories"])
        except (OSError, RuntimeError, tarfile.TarError) as exc:
            issues.append(f"Archive contents mismatch: {exc}")
    return {"ok": not issues, "archive": str(archive), "file_count": manifest.get("file_count"), "issues": issues}


def backup_dataset_archive(
    source_dir: str | Path,
    backup_root: str | Path,
    *,
    temp_dir: str | Path | None = None,
    progress_every: int = 200,
    scan: BackupScan | None = None,
) -> dict:
    """Archive all data; reuse an existing preflight scan to avoid rescanning."""
    if progress_every < 1:
        raise ValueError("progress_every must be >= 1")
    source = Path(source_dir).expanduser().resolve()
    backup_root = Path(backup_root).expanduser().resolve()
    if scan is None:
        scan = scan_backup_source(source, backup_root, progress_every=progress_every)
    if scan.source != source or scan.backup_root != backup_root:
        raise ValueError("Preflight scan belongs to a different source/destination")
    inventory = scan.files
    directories = scan.directories
    total_bytes = scan.summary()["total_bytes"]
    scratch = Path(temp_dir).expanduser().resolve() if temp_dir else Path(tempfile.gettempdir()).resolve()
    if not scratch.is_dir() or scratch == source or source in scratch.parents:
        raise ValueError("Local scratch must exist outside the source data directory")
    if backup_root == scratch or backup_root in scratch.parents:
        raise ValueError("Scratch cannot be the backup destination")
    if str(scratch).startswith("/content/drive/"):
        raise ValueError("Choose local /content scratch, not mounted Google Drive")
    required = int(total_bytes * 1.05) + (len(inventory) + len(directories)) * 1024 + 32 * 1024 * 1024
    free = shutil.disk_usage(scratch).free
    print(f"[Backup / Dung lượng] Scratch: {free / 1024**3:.2f} GiB trống; "
          f"ước lượng cần tối đa {required / 1024**3:.2f} GiB", flush=True)
    if free < required:
        raise RuntimeError(f"Not enough LOCAL scratch space: need ~{required / 1024**3:.2f} GiB, "
                           f"available {free / 1024**3:.2f} GiB. Clear /content or choose another temp_dir.")
    backup_root.mkdir(parents=True, exist_ok=True)
    stem = datetime.now(timezone.utc).strftime("m1_baseline_%Y%m%dT%H%M%S_%fZ") + "_" + uuid.uuid4().hex[:8]
    final = backup_root / f"{stem}.tar.gz"
    incomplete = backup_root / f".{stem}.tar.gz.incomplete"
    manifest_path = backup_root / f"{stem}.manifest.json"
    records = []
    nbytes = 0
    begin = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="m1-archive-", dir=scratch) as work:
        local_archive = Path(work) / "snapshot.tar.gz"
        with tarfile.open(local_archive, "w:gz", compresslevel=1) as tar:
            for relative in directories:
                info = tarfile.TarInfo(relative + "/")
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tar.addfile(info)
            last_log = time.monotonic()
            for i, (relative, (expected_size, expected_mtime_ns)) in enumerate(sorted(inventory.items()), 1):
                path = source / relative
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_size != expected_size or info.st_mtime_ns != expected_mtime_ns:
                    raise RuntimeError(f"Source changed before archiving: {relative}")
                header = tarfile.TarInfo(relative)
                header.size = expected_size
                header.mode = stat.S_IMODE(info.st_mode)
                header.mtime = int(info.st_mtime)
                with path.open("rb") as original:
                    reader = _HashingReader(original)
                    tar.addfile(header, reader)
                after = path.lstat()
                if reader.count != expected_size or after.st_size != expected_size or after.st_mtime_ns != expected_mtime_ns:
                    raise RuntimeError(f"Source changed during archiving: {relative}")
                records.append({"path": relative, "bytes": expected_size, "sha256": reader.digest.hexdigest()})
                nbytes += expected_size
                now = time.monotonic()
                if i % progress_every == 0 or now - last_log >= 5:
                    _progress("Đóng gói", i, len(inventory), nbytes, begin)
                    last_log = now
        _progress("Đóng gói HOÀN TẤT", len(records), len(inventory), nbytes, begin)
        print("[Backup / Kiểm tra nguồn] Đang quét lại danh sách file (chỉ metadata) ...", flush=True)
        latest = scan_backup_source(source, backup_root, progress_every=progress_every)
        if latest.files != inventory or latest.directories != directories:
            raise RuntimeError("Source inventory changed during backup; stop all writers")
        print("[Backup / Verify] Đang kiểm tra lại từng file bên trong archive...", flush=True)
        _verify_members(local_archive, records, directories, progress_every=progress_every)
        local_sha = _sha256_progress(local_archive, label="SHA-256 bản nén")
        archive_bytes = local_archive.stat().st_size
        print(f"[Backup / Upload] Bắt đầu ghi {archive_bytes / 1024**2:,.1f} MiB lên Google Drive...", flush=True)
        uploaded = 0
        last_log = time.monotonic()
        up_start = last_log
        with local_archive.open("rb") as src, incomplete.open("xb") as dest:
            while chunk := src.read(_CHUNK_SIZE):
                dest.write(chunk)
                uploaded += len(chunk)
                now = time.monotonic()
                if now - last_log >= 5 or uploaded == archive_bytes:
                    print(f"[Backup / Upload] {uploaded / 1024**2:,.1f}/{archive_bytes / 1024**2:,.1f} MiB "
                          f"({100 * uploaded / max(archive_bytes, 1):.1f}%), "
                          f"{now - up_start:.0f}s elapsed", flush=True)
                    last_log = now
        print("[Backup / Drive] Đang đọc lại archive trên Drive để so SHA-256...", flush=True)
        if incomplete.stat().st_size != archive_bytes or _sha256_progress(incomplete, label="Verify Drive") != local_sha:
            raise RuntimeError(f"Drive copy failed integrity check; incomplete backup kept: {incomplete}")
        manifest = {
            "schema_version": 3,
            "status": "verified",
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "source_dir": str(source),
            "archive_bytes": archive_bytes,
            "archive_sha256": local_sha,
            "file_count": len(records),
            "total_source_bytes": total_bytes,
            "directories": directories,
            "files": records,
        }
        incomplete.rename(final)
        manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[Backup / HOÀN TẤT] VERIFIED: {final}", flush=True)
    return {
        "status": "verified", "archive": str(final), "manifest": str(manifest_path),
        "file_count": len(records), "source_bytes": total_bytes,
        "archive_bytes": archive_bytes, "archive_sha256": local_sha,
        "elapsed_sec": round(time.monotonic() - begin, 1),
    }
