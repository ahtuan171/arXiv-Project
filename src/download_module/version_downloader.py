"""Incremental, version-aware arXiv source downloader for Milestone 1.

Only creates NEW data/raw/<paper_id>/vN/ directories. NEVER replaces legacy
raw/<paper_id>/ files or modifies papers.jsonl and references.jsonl.

Each completed version has a .version-status.json marker with SHA-256 hashes.
The append-only data/versions_manifest.jsonl is an event/recovery log. The
filesystem marker, not an old publication-level CSV success, proves a version
is present. A crash after publishing but before journaling can be recovered.

Use one writer per member Drive directory; Drive FUSE cannot provide a
multi-object atomic transaction. Default API is dry-run.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import random
import re
import shutil
import stat
import tempfile
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import requests

from src import config, recovery, utils
from src.download_module.downloader import extract_source_archive
from src.scripts.audit_missing_versions import _jsonl_rows, _latest_jsonl, _valid_meta, _versions

_VERSION = re.compile(r"^v[1-9]\d*$")
_MARKER = ".version-status.json"
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
SOURCE_BASE_URL = "https://export.arxiv.org/src"
FetchFn = Callable[[str], tuple[str, bytes, str, str]]
ProgressFn = Callable[[int, int, dict], None]


def _sha256(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as src:
        for block in iter(lambda: src.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def _network_id(canonical: str) -> str:
    """Translate validated canonical IDs back to arXiv network syntax."""
    if re.fullmatch(r"\d{4}_\d{4,5}", canonical):
        return canonical.replace("_", ".", 1)
    if re.fullmatch(r"[A-Za-z-]+(?:\.[A-Za-z-]+)?_\d{7}", canonical):
        name, number = canonical.rsplit("_", 1)
        return f"{name}/{number}"
    raise ValueError(f"Unsupported arXiv ID: {canonical!r}")


def _paper_id(value: Any) -> str:
    canonical = utils.canonical_arxiv_id(str(value))
    _network_id(canonical)
    return canonical


def _status_record(raw_dir: Path, paper_id: str, version: str) -> dict | None:
    """Verify one known target, without scanning any other legacy raw folder."""
    target = raw_dir / paper_id / version
    if not target.exists() and not target.is_symlink():
        return None
    if target.is_symlink() or not target.is_dir():
        return {"status": "conflict", "error": "destination is not a regular directory"}
    marker = target / _MARKER
    if not marker.is_file() or marker.is_symlink():
        return {"status": "conflict", "error": "existing version has no trusted verification marker"}
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
        if payload.get("arxiv_id") != paper_id or payload.get("version") != version:
            raise ValueError("marker identity mismatch")
        status = payload["status"]
        if status not in {"success", "pdf_only"}:
            raise ValueError("marker status invalid")
        records = payload["files"]
        if not isinstance(records, list):
            raise ValueError("marker files missing")
        seen = set()
        tex_count = 0
        for record in records:
            rel = record["path"]
            if not isinstance(rel, str) or not rel or rel.startswith("/") or "\\" in rel:
                raise ValueError("unsafe manifest path")
            parts = Path(rel).parts
            if ".." in parts or rel in seen:
                raise ValueError("duplicate or traversing file path")
            seen.add(rel)
            if not (rel.lower().endswith(".tex") or rel.lower().endswith(".bib")):
                raise ValueError("not a permitted source type")
            item = target / rel
            if item.is_symlink() or not item.is_file():
                raise ValueError("missing/untrusted source file")
            if item.stat().st_size != record["bytes"] or _sha256(item) != record["sha256"]:
                raise ValueError("source checksum mismatch")
            tex_count += int(rel.lower().endswith(".tex") and item.stat().st_size > 0)
        if status == "success" and tex_count < 1:
            raise ValueError("success marker without nonempty TeX")
        if status == "pdf_only" and records:
            raise ValueError("PDF-only marker contains source files")
        # Explicitly reject unexpected files (e.g. partial extraction or tampering).
        actual_files = set()
        for file in target.rglob("*"):
            if file.is_symlink():
                raise ValueError("symlink present")
            if file.is_file() and file != marker:
                actual_files.add(file.relative_to(target).as_posix())
        if actual_files != seen:
            raise ValueError("unexpected or missing files")
        return {"status": status, "marker": payload}
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        return {"status": "conflict", "error": f"unverified existing version: {exc}"}


def _make_marker(
    stage: Path, paper_id: str, version: str, status: str,
    url: str, raw_size: int,
) -> dict:
    records = []
    for file in sorted(stage.rglob("*")):
        if file.is_symlink():
            raise ValueError(f"Unsafe extracted symlink: {file}")
        if not file.is_file() or file.name == _MARKER:
            continue
        if file.suffix.lower() not in {".tex", ".bib"}:
            raise ValueError(f"Unexpected extracted source: {file}")
        records.append({
            "path": file.relative_to(stage).as_posix(),
            "bytes": file.stat().st_size,
            "sha256": _sha256(file),
        })
    tex_count = sum(r["path"].lower().endswith(".tex") and r["bytes"] > 0 for r in records)
    if status == "success" and tex_count == 0:
        raise ValueError("Source archive has no nonempty .tex")
    if status == "pdf_only" and records:
        raise ValueError("Unexpected files in PDF-only stage")
    marker = {
        "schema_version": 1, "arxiv_id": paper_id, "version": version,
        "status": status, "source_url": url, "raw_size_bytes": raw_size,
        "cleaned_size_bytes": sum(r["bytes"] for r in records),
        "num_tex_files": tex_count, "files": records,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    (stage / _MARKER).write_text(json.dumps(marker, ensure_ascii=False, indent=2), encoding="utf-8")
    return marker


def _publish_version(stage: Path, raw_dir: Path, paper_id: str, version: str) -> Path:
    """Stage on Drive then rename a NEW version directory; never replace one."""
    parent = raw_dir / paper_id
    if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
        raise FileExistsError(f"Unsafe legacy raw folder: {parent}")
    parent.mkdir(parents=True, exist_ok=True)
    destination = parent / version
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"Version already exists; refusing overwrite: {destination}")
    staging = parent / f".{version}.staging-{uuid.uuid4().hex}"
    try:
        shutil.copytree(stage, staging, symlinks=False)
        # Check every staged file before publishing; marker hashes source content.
        # Verify staged bytes before publishing.
        for file in stage.rglob("*"):
            if file.is_file():
                rel = file.relative_to(stage)
                if not (staging / rel).is_file() or _sha256(file) != _sha256(staging / rel):
                    raise RuntimeError(f"Drive staging verification failed: {rel}")
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(f"Destination appeared during staging: {destination}")
        staging.rename(destination)
    except BaseException:
        print(f"[Versions] Incomplete Drive staging retained for inspection: {staging}", flush=True)
        raise
    return destination


def _request_source(
    url: str,
    *,
    session: requests.Session | None = None,
    limiter: utils.RateLimiter | None = None,
    attempts: int = config.MAX_RETRIES,
    timeout: int = 120,
) -> tuple[str, bytes, str, str]:
    """Return status, body, content-type, and error; retry polite transient errors.

    Resumes interrupted downloads via HTTP Range headers when IncompleteRead or
    network drops occur during streaming.
    """
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    getter = session.get if session is not None else requests.get
    chunks: list[bytes] = []
    downloaded_len = 0
    final_content_type = ""
    total_expected_len: int | None = None

    for attempt in range(attempts):
        if limiter:
            limiter.wait()
        retry_after = None
        error = ""
        req_headers = {"User-Agent": "arXiv-Project/1.0 (academic research)"}
        if downloaded_len > 0:
            req_headers["Range"] = f"bytes={downloaded_len}-"

        try:
            response = getter(url, headers=req_headers, timeout=timeout, stream=True)
            try:
                code = response.status_code
                if code == 404:
                    return "not_found", b"", "", "HTTP 404"
                if code == 429 or code in {500, 502, 503, 504}:
                    retry_after = recovery.parse_retry_after(response.headers.get("Retry-After"))
                    error = f"HTTP {code}"
                elif code == 416:
                    # Requested Range Not Satisfiable: reset and restart from 0
                    downloaded_len = 0
                    chunks.clear()
                    total_expected_len = None
                    error = f"HTTP {code}"
                elif code not in (200, 206):
                    return "failed", b"", "", f"HTTP {code}"
                else:
                    if code == 200:
                        chunks.clear()
                        downloaded_len = 0
                        final_content_type = response.headers.get("Content-Type", "").lower()
                        exp_hdr = response.headers.get("Content-Length")
                        if exp_hdr and exp_hdr.isdigit():
                            total_expected_len = int(exp_hdr)
                    elif code == 206:
                        if not final_content_type:
                            final_content_type = response.headers.get("Content-Type", "").lower()
                        cr = response.headers.get("Content-Range", "")
                        if "/" in cr:
                            tot_str = cr.split("/")[-1].strip()
                            if tot_str.isdigit():
                                total_expected_len = int(tot_str)

                    for block in response.iter_content(chunk_size=256 * 1024):
                        if not block:
                            continue
                        downloaded_len += len(block)
                        if downloaded_len > MAX_ARCHIVE_BYTES:
                            return "failed", b"", "", "Source archive exceeded 128 MiB safety limit"
                        chunks.append(block)

                    if total_expected_len is not None and downloaded_len < total_expected_len:
                        raise requests.RequestException(
                            f"IncompleteRead({downloaded_len} bytes read, {total_expected_len - downloaded_len} more expected)"
                        )

                    return "ok", b"".join(chunks), final_content_type, ""
            finally:
                response.close()
        except (requests.RequestException, http.client.IncompleteRead) as exc:
            error = f"Network failure: {exc}"

        if attempt + 1 < attempts:
            delay = retry_after if retry_after is not None else min(2 ** (attempt + 1), 60)
            if downloaded_len > 0:
                print(f"[Versions] Reconnecting {url} at byte {downloaded_len} in {delay:.1f}s ({error})", flush=True)
            time.sleep(max(config.ARXIV_MIN_INTERVAL, delay) + random.uniform(0, 0.5))
        else:
            return "failed", b"", "", error
    raise AssertionError("unreachable")


def _event(paper_id: str, version: str, status: str, *, marker: dict | None = None,
           error: str = "", elapsed: float = 0,
           previous_attempts: int = 0) -> dict:
    network = _network_id(paper_id)
    # Match the legacy recovery policy: retry transient failures with bounded
    # exponential cooldown; permanent HTTP errors require an explicit retry.
    try:
        prior = max(0, int(previous_attempts))
    except (ValueError, TypeError):
        prior = 0
    attempts = prior + 1 if status == "failed" else 0
    blocked = error.startswith(("HTTP 400", "HTTP 401", "HTTP 403", "HTTP 410",
                                "Source archive exceeded"))
    error_type = ("blocked" if blocked else "transient") if status == "failed" else (
        "not_found" if status == "not_found" else ""
    )
    delay = min(config.RECOVERY_COOLDOWN * (2 ** min(max(0, attempts - 1), 16)),
                config.RECOVERY_MAX_COOLDOWN)
    next_retry_at = time.time() + delay if status == "failed" and not blocked else None
    result = {
        "arxiv_id": paper_id, "version": version,
        "status": status, "source_url": f"{SOURCE_BASE_URL}/{network}{version}",
        "raw_path": f"raw/{paper_id}/{version}" if status in {"success", "pdf_only"} else "",
        "raw_size_bytes": (marker or {}).get("raw_size_bytes", 0),
        "cleaned_size_bytes": (marker or {}).get("cleaned_size_bytes", 0),
        "num_tex_files": (marker or {}).get("num_tex_files", 0),
        "download_time_sec": round(elapsed, 3), "error": error,
        "error_type": error_type, "attempts": attempts, "next_retry_at": next_retry_at,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    return result


def _latest_version_events(path: Path) -> dict[tuple[str, str], dict]:
    """Read the latest append-only event for each (paper, version) pair."""
    latest: dict[tuple[str, str], dict] = {}
    for row in _jsonl_rows(path):
        paper_id, version = row.get("arxiv_id"), row.get("version")
        if not isinstance(paper_id, str) or not isinstance(version, str) or not _VERSION.fullmatch(version):
            continue
        try:
            key = (_paper_id(paper_id), version)
        except ValueError:
            continue
        latest[key] = row
    return latest


def plan_version_downloads(
    data_dir: str | Path,
    ids: Iterable[str] | None = None,
    *,
    retry_errors: bool = False,
    retry_not_found: bool = False,
) -> dict:
    """Plan attempts using verified vN directories and the last manifest event.

    Verified files always win over the journal. A recorded 404 is skipped unless
    explicitly retried. Failed requests obey a persisted recovery cooldown.
    """
    data = Path(data_dir)
    papers_path = data / "papers.jsonl"
    if not papers_path.is_file():
        raise FileNotFoundError(f"Metadata not found: {papers_path}")
    papers = _latest_jsonl(papers_path)
    for key, row in _latest_jsonl(data / "papers.jsonl.journal").items():
        if _valid_meta(row) or not _valid_meta(papers.get(key)):
            papers[key] = row
    requested = sorted({_paper_id(x) for x in ids}) if ids is not None else sorted(papers)
    history = _latest_version_events(data / "versions_manifest.jsonl")
    now = time.time()
    work = []
    counts: Counter = Counter()
    for paper_id in requested:
        paper = papers.get(paper_id)
        if not _valid_meta(paper):
            counts["missing_metadata"] += 1
            continue
        versions, issue = _versions(paper, None)
        if issue or not versions:
            counts["invalid_versions"] += 1
            continue
        for version in versions:
            current = _status_record(data / "raw", paper_id, version)
            counts["expected_versions"] += 1
            if current and current["status"] in {"success", "pdf_only"}:
                counts["verified_existing"] += 1
            elif current:
                counts["conflicts"] += 1
                work.append({"arxiv_id": paper_id, "version": version, "action": "conflict",
                             "reason": current["error"]})
            else:
                previous = history.get((paper_id, version), {})
                previous_status = previous.get("status")
                if previous_status == "not_found" and not retry_not_found:
                    counts["known_not_found"] += 1
                    continue
                if previous_status == "failed" and not retry_errors:
                    if previous.get("error_type") == "blocked":
                        counts["blocked_errors"] += 1
                        continue
                    try:
                        next_retry_at = float(previous.get("next_retry_at") or 0)
                    except (TypeError, ValueError):
                        next_retry_at = 0
                    if next_retry_at > now:
                        counts["deferred_retry"] += 1
                        continue
                counts["to_download"] += 1
                work.append({
                    "arxiv_id": paper_id,
                    "version": version,
                    "action": "download",
                    "previous_attempts": previous.get("attempts", 0),
                })
    return {"data_dir": str(data), "requested_papers": len(requested),
            "summary": dict(counts), "worklist": work}


def run_version_downloads(
    data_dir: str | Path,
    *,
    ids: Iterable[str] | None = None,
    max_downloads: int = 3,
    dry_run: bool = True,
    scratch_parent: str | Path | None = None,
    fetch: FetchFn | None = None,
    min_interval: float = config.ARXIV_MIN_INTERVAL,
    progress_callback: ProgressFn | None = None,
    retry_errors: bool = False,
    retry_not_found: bool = False,
) -> dict:
    """Process only NEW vN directories; dry-run by default. No legacy mutations.

    max_downloads counts network attempts, not existing verified/skipped entries.
    Set max_downloads=0 for no artificial cap (use only after small tests).
    progress_callback(attempts_completed, target_attempts, event) runs only
    after each actual network attempt is durably recorded, never on dry-run.
    The callback reports attempt progress, not success rate or overall coverage.
    retry_errors overrides the failure cooldown (including permanent HTTP errors).
    retry_not_found explicitly retries previous HTTP 404 responses.
    """
    if max_downloads < 0:
        raise ValueError("max_downloads must be >= 0")
    data = Path(data_dir)
    plan = plan_version_downloads(
        data, ids=ids, retry_errors=retry_errors, retry_not_found=retry_not_found
    )
    print("[Versions] Plan:", plan["summary"], flush=True)
    if dry_run:
        print("[Versions] DRY RUN: no download and no Drive changes.", flush=True)
        return {"dry_run": True, "plan": plan, "processed": [], "summary": {"downloaded": 0}}
    raw = data / "raw"
    if raw.is_symlink() or (raw.exists() and not raw.is_dir()):
        raise ValueError("Unsafe raw directory")
    parent = Path(scratch_parent or ("/content" if Path("/content").is_dir() else tempfile.gettempdir()))
    if not parent.is_dir() or parent.resolve() == data.resolve() or data.resolve() in parent.resolve().parents or str(parent.resolve()).startswith("/content/drive/"):
        raise ValueError("Scratch must be a real local directory, not Drive")
    limiter = utils.RateLimiter(max(config.ARXIV_MIN_INTERVAL, min_interval))
    events = []
    counts = Counter()
    attempts = 0
    total_candidates = plan["summary"].get("to_download", 0)
    progress_total = min(max_downloads, total_candidates) if max_downloads else total_candidates
    with requests.Session() as session:
        for task in plan["worklist"]:
            paper_id, version = task["arxiv_id"], task["version"]
            if task["action"] == "conflict":
                counts["conflicts"] += 1
                print(f"[Versions] CONFLICT {paper_id}/{version}: {task['reason']}", flush=True)
                continue
            if max_downloads and attempts >= max_downloads:
                break
            # Recheck immediately before each network request (resume, crash safety).
            current = _status_record(raw, paper_id, version)
            if current is not None:
                counts["conflicts" if current["status"] == "conflict" else "already_present"] += 1
                continue
            url = f"{SOURCE_BASE_URL}/{_network_id(paper_id)}{version}"
            attempts += 1
            start = time.monotonic()
            print(f"[Versions] {attempts}/{max_downloads or 'all'} GET {url}", flush=True)
            try:
                if fetch is not None:
                    state, body, content_type, error = fetch(url)
                else:
                    state, body, content_type, error = _request_source(url, session=session, limiter=limiter)
                if state == "ok":
                    status = "pdf_only" if "application/pdf" in content_type or body.startswith(b"%PDF") else "success"
                    with tempfile.TemporaryDirectory(prefix="m1-version-", dir=parent) as tmp:
                        stage = Path(tmp)
                        if status == "success":
                            extracted, has_tex = extract_source_archive(body, stage)
                            if not extracted or not has_tex:
                                raise ValueError("Source archive has no valid LaTeX (.tex)")
                        marker = _make_marker(stage, paper_id, version, status, url, len(body))
                        output = _publish_version(stage, raw, paper_id, version)
                        # Verify published marker and every TeX/Bib hash, not just staging.
                        checked = _status_record(raw, paper_id, version)
                        if not checked or checked["status"] != status:
                            raise RuntimeError(f"Published files failed verification: {output}")
                    event = _event(paper_id, version, status, marker=marker,
                                   elapsed=time.monotonic() - start)
                else:
                    event = _event(paper_id, version, "not_found" if state == "not_found" else "failed",
                                   error=error or state, elapsed=time.monotonic() - start,
                                   previous_attempts=task.get("previous_attempts", 0))
            except (OSError, RuntimeError, ValueError, requests.RequestException) as exc:
                # An existing final directory or an unverified stage must not be
                # replaced or deleted. Record failure, then continue.
                event = _event(paper_id, version, "failed",
                               error=f"{type(exc).__name__}: {exc}", elapsed=time.monotonic() - start,
                               previous_attempts=task.get("previous_attempts", 0))
            # Append event only after publication; a later run can recover from the marker.
            recovery.durable_append(data / "versions_manifest.jsonl", event)
            events.append(event)
            counts[event["status"]] += 1
            print(f"[Versions] {paper_id}/{version}: {event['status']} "
                  f"({event['download_time_sec']:.1f}s) {event['error'][:130]}", flush=True)
            if progress_callback is not None:
                try:
                    progress_callback(attempts, progress_total, event)
                except Exception as exc:
                    # Progress UI is best-effort; never turn a recorded download into a failure.
                    print(f"[Versions] WARNING: progress callback failed: {exc}", flush=True)
    counts["network_attempts"] = attempts
    counts["remaining_candidates"] = plan["summary"].get("to_download", 0) - attempts
    return {"dry_run": False, "plan": plan, "processed": events, "summary": dict(counts)}
