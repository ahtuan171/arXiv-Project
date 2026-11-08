"""Conflict-aware Google Drive <-> Colab scratch synchronization for Milestone 1.

Safety properties:
- A NEW scratch directory is created on every start (never reuse stale /content/data).
- Only small snapshots, journals and CSV/ledger files are copied in; raw/ is not scanned.
- Outbound writes compare the original Drive SHA-256 with the session baseline.
  If another writer changed a destination, STOP instead of overwriting it.
- Files are staged into unique temporary siblings, verified and then replaced.
- New raw/<id>/ directories are published only when the Drive ID does not exist.
- No Drive file or directory is ever deleted by this module.

NOTE: This sync layer prevents stale/clashing saves; it does NOT make the old
publication-level crawler append-only. Keep legacy crawling disabled until the
version-aware writer is tested. Do not run parallel writers to one Drive folder.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

ROOT_FILES = frozenset({
    "papers.jsonl", "papers.jsonl.journal", "references.jsonl",
    "references_raw.jsonl", "versions_manifest.jsonl",
})
CHUNK_SIZE = 1024 * 1024


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_regular(path: Path) -> None:
    if path.is_symlink() or not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError(f"Only regular files are allowed: {path}")


def _tracked_files(root: Path) -> dict[str, Path]:
    """List only root snapshots and direct children of csv/, never raw/."""
    result: dict[str, Path] = {}
    if not root.is_dir():
        return result
    for name in sorted(ROOT_FILES):
        file = root / name
        if file.is_symlink():
            raise ValueError(f"Unsafe symlink: {file}")
        if file.exists():
            _safe_regular(file)
            result[name] = file
    csv_dir = root / "csv"
    if csv_dir.is_symlink():
        raise ValueError(f"Unsafe symlink: {csv_dir}")
    if csv_dir.is_dir():
        for file in csv_dir.iterdir():
            if file.is_symlink():
                raise ValueError(f"Unsafe symlink: {file}")
            if file.is_file():
                _safe_regular(file)
                result[f"csv/{file.name}"] = file
    return result


@dataclass
class ScratchSession:
    drive_dir: Path
    scratch_dir: Path
    baseline: dict[str, str]
    published_raw: set[str] = field(default_factory=set)


def start_scratch_session(
    drive_dir: str | Path,
    *,
    scratch_parent: str | Path = "/content",
) -> ScratchSession:
    """Make a fresh scratch tree, capture Drive hashes and copy only small files."""
    remote = Path(drive_dir).expanduser().resolve()
    local_parent = Path(scratch_parent).expanduser().resolve()
    if not remote.is_dir():
        raise FileNotFoundError(f"Persistent Drive data directory missing: {remote}")
    if not local_parent.is_dir() or local_parent == remote or remote in local_parent.parents:
        raise ValueError("Scratch parent must be a separate existing local directory")
    if str(local_parent).startswith("/content/drive/"):
        raise ValueError("Scratch cannot be located on mounted Google Drive")
    scratch = Path(tempfile.mkdtemp(prefix="arxiv-m1-", dir=local_parent))
    baseline: dict[str, str] = {}
    copied = 0
    try:
        for rel, path in _tracked_files(remote).items():
            original_hash = _hash_file(path)
            target = scratch / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            if _hash_file(target) != original_hash or _hash_file(path) != original_hash:
                raise RuntimeError(f"Persistent data changed while starting scratch: {rel}")
            baseline[rel] = original_hash
            copied += 1
        (scratch / "csv").mkdir(exist_ok=True)
        (scratch / "raw").mkdir(exist_ok=True)
        print(f"[Safe Scratch] Session created: {scratch} | {copied} metadata/CSV files copied; raw/ NOT scanned")
        return ScratchSession(remote, scratch, baseline)
    except BaseException:
        # Preserve the scratch folder for diagnosis. Drive files remain untouched.
        print(f"[Safe Scratch] Startup failed; inspect local scratch: {scratch}")
        raise


def _validate_new_raw(source: Path, dest: Path) -> None:
    """Preflight raw ID; don't read or change existing Drive paper directories."""
    if source.is_symlink() or not source.is_dir():
        raise ValueError(f"Unsafe raw source folder: {source}")
    if dest.exists() or dest.is_symlink():
        raise FileExistsError(f"Drive raw folder already exists; refusing overwrite: {dest}")
    for item in source.rglob("*"):
        if item.is_symlink():
            raise ValueError(f"Unsafe raw symlink: {item}")
        if not (item.is_file() or item.is_dir()):
            raise ValueError(f"Unexpected special raw entry: {item}")


def _publish_new_raw(source: Path, dest: Path) -> None:
    """Stage new raw tree on Drive, verify bytes, then rename to final path."""
    staging = dest.parent / f".{dest.name}.staging-{uuid.uuid4().hex}"
    try:
        shutil.copytree(source, staging, symlinks=False)
        for file in source.rglob("*"):
            if file.is_file():
                rel = file.relative_to(source)
                if _hash_file(file) != _hash_file(staging / rel):
                    raise RuntimeError(f"Raw copy verification failed: {rel}")
        if dest.exists():
            raise FileExistsError(f"Raw destination appeared during staging: {dest}")
        staging.rename(dest)
    except BaseException:
        # Keep partial staging for investigation; original destination untouched.
        print(f"[Safe Scratch] Incomplete raw staging retained at: {staging}")
        raise


def _write_checked(src: Path, target: Path, expected_remote_hash: str | None) -> None:
    """Stage verified bytes and replace iff remote still matches the baseline."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.parent / f".{target.name}.sync-{uuid.uuid4().hex}"
    try:
        shutil.copy2(src, temp)
        if _hash_file(temp) != _hash_file(src):
            raise RuntimeError(f"Staged file differs from scratch: {src}")
        current = _hash_file(target) if target.is_file() else None
        if current != expected_remote_hash or target.is_symlink():
            raise RuntimeError(f"Drive was changed by another session; refusing overwrite: {target}")
        os.replace(temp, target)
    except BaseException:
        print(f"[Safe Scratch] Incomplete file staging retained at: {temp}")
        raise


def sync_scratch_session(session: ScratchSession) -> dict:
    """Publish only changed small files and NEW raw IDs; abort on conflicts.

    Call from finally, AFTER crawler has stopped writing. Raise on failures:
    the caller must not report that Drive is safely synced when it is not.
    """
    remote, scratch = session.drive_dir, session.scratch_dir
    if not remote.is_dir() or not scratch.is_dir():
        raise RuntimeError("Persistent/scratch directory is unavailable")
    local = _tracked_files(scratch)
    baseline = session.baseline

    changed: dict[str, tuple[Path, str, str | None]] = {}
    for rel, path in local.items():
        local_sha = _hash_file(path)
        if local_sha != baseline.get(rel):
            changed[rel] = (path, local_sha, baseline.get(rel))

    # Preflight ALL changed file destinations before performing any writes.
    for rel, (_path, _sha, expected) in changed.items():
        target = remote / rel
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise RuntimeError(f"Unsafe Drive destination: {target}")
        current = _hash_file(target) if target.is_file() else None
        if current != expected:
            raise RuntimeError(f"Drive changed since scratch started: {rel}; nothing synced")

    scratch_raw = scratch / "raw"
    raw_new: list[tuple[Path, Path, str]] = []
    if scratch_raw.is_dir():
        remote_raw = remote / "raw"
        for folder in scratch_raw.iterdir():
            if folder.name in session.published_raw:
                continue
            if folder.name in {".", ".."} or folder.name.startswith("."):
                # Do not publish temporary downloader artifacts/lockfiles.
                continue
            if not folder.is_dir() or folder.is_symlink():
                continue
            target = remote_raw / folder.name
            _validate_new_raw(folder, target)
            raw_new.append((folder, target, folder.name))

    # Don't write to Drive until every conflict has been checked.
    copied_raw = 0
    for source, dest, name in raw_new:
        dest.parent.mkdir(parents=True, exist_ok=True)
        _publish_new_raw(source, dest)
        session.published_raw.add(name)
        copied_raw += 1

    copied_files = 0
    for rel, (source, new_sha, expected) in changed.items():
        _write_checked(source, remote / rel, expected)
        baseline[rel] = new_sha
        copied_files += 1

    print(f"[Safe Scratch] Synced {copied_files} changed metadata/CSV files, "
          f"{copied_raw} NEW raw paper folders. Existing raw/ unchanged.")
    return {"updated_small_files": copied_files, "new_raw_folders": copied_raw, "scratch_path": str(scratch)}
