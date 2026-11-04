"""Opt-in, version-unattributed Semantic Scholar raw reference collection.

Unlike the publication-level references.jsonl, this dataset retains EACH
reference position, including entries without S2/arXiv IDs or citedPaper.
It does not infer which arXiv revision contained a reference.

Output: append-only data/references_raw.jsonl and
data/references_raw_ledger.jsonl. One writer per data directory; stop all
other writers before running on Drive. No existing published data is changed.

A paper's raw rows are written only after pagination is complete. If a crash
occurs between appending rows and writing the ledger, the next run verifies
and reuses matching (arxiv_id, ref_index) rows without duplicating them.
Conflicting historical rows are NEVER overwritten automatically.
"""
from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import os
import tempfile
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import quote

from src import config, recovery, utils
from src.crawl_references_module.s2_api_client import (
    S2ApiAuthError, S2ApiClient, S2ApiError, S2ApiNotFound,
    S2ApiPermanentError,
)

SOURCE_ENDPOINT = "https://api.semanticscholar.org/graph/v1/paper/arXiv:{id}/references"
ProgressFn = Callable[[int, int, dict], None]


def _network_id(value: str) -> str:
    raw = utils.normalize_arxiv_id(value)
    if not utils.is_valid_arxiv_id(raw):
        raise ValueError(f"Invalid arXiv identifier: {value!r}")
    return raw.replace("_", ".", 1) if raw[:4].isdigit() and "_" in raw else raw


def _read_records(path: Path) -> list[dict]:
    """Strict reads: bad/truncated JSONL blocks further writes (fail closed)."""
    if not path.is_file():
        return []
    rows: list[dict] = []
    with path.open("rb") as fh:
        for lineno, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except (ValueError, UnicodeError) as exc:
                raise ValueError(f"Invalid JSONL {path}:{lineno}; repair before resuming") from exc
            if not isinstance(row, dict):
                raise ValueError(f"Non-object row {path}:{lineno}")
            rows.append(row)
    return rows


def _digest(rows: list[dict]) -> str:
    payload = json.dumps(
        [r["raw_reference_entry"] for r in rows],
        sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _load_state(root: Path) -> tuple[dict[str, dict[int, dict]], dict[str, dict]]:
    raw: dict[str, dict[int, dict]] = {}
    for row in _read_records(root / "references_raw.jsonl"):
        paper_id, index = row.get("arxiv_id"), row.get("ref_index")
        if not isinstance(paper_id, str) or not isinstance(index, int) or isinstance(index, bool) or index < 0:
            raise ValueError("Invalid raw reference key in saved dataset")
        previous = raw.setdefault(paper_id, {}).get(index)
        if previous is not None:
            if previous.get("raw_reference_entry") != row.get("raw_reference_entry"):
                raise ValueError(f"Conflicting raw reference rows for {paper_id}/{index}")
            raise ValueError(f"Duplicate raw reference key for {paper_id}/{index}")
        if "raw_reference_entry" not in row:
            raise ValueError(f"Missing source payload for {paper_id}/{index}")
        raw[paper_id][index] = row
    ledger: dict[str, dict] = {}
    for event in _read_records(root / "references_raw_ledger.jsonl"):
        aid = event.get("arxiv_id")
        if isinstance(aid, str):
            ledger[aid] = event
    return raw, ledger


def _done(event: dict, existing: dict[int, dict]) -> bool:
    if event.get("status") != "ok":
        return False
    count = event.get("raw_refs_total")
    if not isinstance(count, int) or count < 0 or set(existing) != set(range(count)):
        return False
    ordered = [existing[i] for i in range(count)]
    return bool(event.get("payload_sha256")) and _digest(ordered) == event["payload_sha256"]


def _should_skip(event: dict, existing: dict[int, dict], *, retry_errors: bool,
                 retry_not_found: bool) -> str:
    if _done(event, existing):
        return "verified_existing"
    status = event.get("status")
    if status == "not_found" and not retry_not_found:
        return "known_not_found"
    if status == "blocked" and not retry_errors:
        return "blocked_errors"
    if status == "error" and not retry_errors:
        try:
            if float(event.get("next_retry_at") or 0) > time.time():
                return "deferred_retry"
        except (ValueError, TypeError):
            pass
    return ""


def _raw_record(citing_id: str, index: int, entry: Any, fetched_at: str) -> dict:
    """Do not discard a null citedPaper or a reference without identifiers."""
    raw_cited = entry.get("citedPaper") if isinstance(entry, dict) else None
    cited = raw_cited if isinstance(raw_cited, dict) else {}
    external = cited.get("externalIds")
    external = external if isinstance(external, dict) else {}
    candidate = utils.normalize_arxiv_id(external.get("ArXiv") or external.get("arxiv"))
    ref_arxiv = utils.canonical_arxiv_id(candidate) if utils.is_valid_arxiv_id(candidate) else ""
    authors = []
    for item in cited.get("authors") or []:
        if isinstance(item, dict):
            name = item.get("name")
            if isinstance(name, str) and name.strip():
                authors.append(name.strip())
        elif isinstance(item, str) and item.strip():
            authors.append(item.strip())
    return {
        "arxiv_id": utils.canonical_arxiv_id(citing_id),
        "version": None, "version_attribution": "unknown_s2_publication_level",
        "ref_index": index,
        "ref_s2_id": str(cited.get("paperId") or ""),
        "ref_arxiv_id": ref_arxiv,
        "ref_title": str(cited.get("title") or ""),
        "ref_authors": authors,
        "ref_submission_date": str(cited.get("publicationDate") or ""),
        "external_ids": external,
        "retrieved_at": fetched_at,
        "source_endpoint": SOURCE_ENDPOINT.format(id=citing_id),
        "raw_cited_paper": raw_cited,
        "raw_reference_entry": entry,
    }


async def _fetch_all_pages(client: Any, paper_id: str, *, max_refs: int = 50000) -> tuple[list, str]:
    """Retain every response entry, fail on truncation/ambiguous pagination."""
    limit = max(1, min(int(getattr(client, "page_size", 500)), 1000))
    offset = 0
    items: list = []
    known_total: int | None = None
    while True:
        payload = await client._request(
            "GET", f"/paper/arXiv:{quote(paper_id, safe='/')}/references",
            params={"fields": config.S2_API_REF_FIELDS, "offset": offset, "limit": limit},
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise ValueError(f"Unexpected reference page shape for {paper_id}")
        chunk = payload["data"]
        if len(chunk) > limit:
            raise ValueError("API returned more references than requested page limit")
        total = payload.get("total")
        if total is not None:
            if not isinstance(total, int) or isinstance(total, bool) or total < 0:
                raise ValueError("Invalid API total")
            if known_total is not None and total != known_total:
                raise ValueError("Reference total changed during pagination")
            known_total = total
        if len(items) + len(chunk) > max_refs:
            raise ValueError(f"Reference count exceeds safe limit ({max_refs})")
        items.extend(chunk)
        next_page = payload.get("next")
        if next_page is not None:
            if (not isinstance(next_page, int) or isinstance(next_page, bool)
                    or next_page != offset + len(chunk) or not chunk):
                raise ValueError("Invalid/noncontiguous API pagination offset")
            offset = next_page
            continue
        if known_total is not None:
            if len(items) != known_total:
                raise ValueError(f"Incomplete API pagination: received {len(items)} / {known_total}")
            return items, "confirmed_total"
        if len(chunk) >= limit:
            raise ValueError("Full final page without total/next: completeness unknown")
        return items, "inferred_short_page"


async def _fetch_batch_papers(active: Any, batch_papers: list[str]) -> list[tuple[str, list[dict] | None]]:
    """Fetch raw reference entries for a batch of paper IDs via POST /paper/batch.

    Returns a list of tuples: (paper_id, entries_or_none)
    If a paper is not found (HTTP 404 / None in API), entries_or_none is None.
    If found, entries_or_none is a list of {"citedPaper": ...} entries.
    """
    if hasattr(active, "fetch_papers_batch_raw"):
        clean_ids = [
            active._clean_id(cid) if hasattr(active, "_clean_id") else _network_id(cid)
            for cid in batch_papers
        ]
        formatted_ids = [f"ARXIV:{cid}" for cid in clean_ids]
        fields_str = getattr(
            config,
            "S2_API_BATCH_REF_FIELDS",
            "paperId,title,references.paperId,references.title,references.authors,references.year,references.publicationDate,references.externalIds",
        )
        raw_items = await active.fetch_papers_batch_raw(formatted_ids, fields=fields_str)
        batch_out: list[tuple[str, list[dict] | None]] = []
        for pid, item in zip(batch_papers, raw_items):
            if item is None:
                batch_out.append((pid, None))
            else:
                raw_refs = item.get("references")
                if not isinstance(raw_refs, list):
                    raw_refs = []
                entries = [
                    {"citedPaper": ref} if isinstance(ref, dict) else {"citedPaper": None}
                    for ref in raw_refs
                ]
                batch_out.append((pid, entries))
        return batch_out

    if hasattr(active, "get_references_batch"):
        batch_results = await active.get_references_batch(batch_papers)
        batch_out = []
        for item in batch_results:
            pid = item.get("arxiv_id")
            status = item.get("status")
            if status == "not_found":
                batch_out.append((pid, None))
            else:
                refs = item.get("refs") or []
                entries = []
                for ref in refs:
                    if hasattr(ref, "paper_id"):
                        ref_dict = {
                            "paperId": ref.paper_id or "",
                            "title": ref.title or "",
                            "authors": [{"name": a} for a in (ref.authors or [])],
                            "year": getattr(ref, "year", None),
                            "publicationDate": getattr(ref, "publication_date", "") or "",
                            "externalIds": {"ArXiv": ref.arxiv_id} if getattr(ref, "arxiv_id", None) else {},
                        }
                        entries.append({"citedPaper": ref_dict})
                    elif isinstance(ref, dict):
                        entries.append({"citedPaper": ref.get("citedPaper", ref)})
                    else:
                        entries.append({"citedPaper": None})
                batch_out.append((pid, entries))
        return batch_out

    raise NotImplementedError("Batch fetching not supported on this client")


def _append(root: Path, records: list[dict], event: dict) -> None:
    if records:
        recovery.durable_append_many(root / "references_raw.jsonl", records)
    recovery.durable_append(root / "references_raw_ledger.jsonl", event)


def collect_raw_references(
    data_dir: str | Path,
    *, ids: Iterable[str],
    max_papers: int = 3,
    batch_size: int = 100,
    dry_run: bool = True,
    retry_errors: bool = False,
    retry_not_found: bool = False,
    client: Any = None,
    progress_callback: ProgressFn | None = None,
    export_csv: bool = True,
) -> dict:
    """Opt-in bounded collection; retry/resume keys are publication IDs.

    A reference is recorded with version=None: Semantic Scholar does not prove
    which revision cites it. max_papers counts network attempts, not skips.
    """
    if max_papers < 0:
        raise ValueError("max_papers must be >= 0")
    root = Path(data_dir)
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise ValueError("Unsafe data directory")
    requested = list(dict.fromkeys(_network_id(str(x)) for x in ids))
    raw, ledger = _load_state(root)
    plan = []
    skipped = Counter()
    for paper_id in requested:
        aid = utils.canonical_arxiv_id(paper_id)
        reason = _should_skip(
            ledger.get(aid, {}), raw.get(aid, {}),
            retry_errors=retry_errors, retry_not_found=retry_not_found,
        )
        if reason:
            skipped[reason] += 1
        else:
            plan.append(paper_id)
    total = min(len(plan), max_papers) if max_papers else len(plan)
    summary = {"requested_papers": len(requested), "to_fetch": len(plan),
               "planned_this_run": total, **dict(skipped)}
    if dry_run:
        return {"dry_run": True, "summary": summary, "processed": []}
    if total == 0:
        csv_file = None
        if export_csv:
            try:
                csv_file = export_raw_reference_stats(root)
            except Exception as exc:
                print(f"[Raw references] Export CSV warning: {exc}", flush=True)
        return {"dry_run": False, "summary": {**summary, "network_attempts": 0}, "processed": [],
                "csv_path": str(csv_file) if csv_file else None}
    if (root / "references_raw.jsonl").is_symlink() or (root / "references_raw_ledger.jsonl").is_symlink():
        raise ValueError("Refusing to write through a symlink")

    async def _execute() -> list[dict]:
        active = client if client is not None else S2ApiClient()
        results = []
        if hasattr(active, "start"):
            await active.start(1)
        eff_batch_size = max(1, min(int(batch_size), 500)) if batch_size is not None else 100
        if hasattr(active, "batch_size"):
            active.batch_size = eff_batch_size
        can_batch = hasattr(active, "fetch_papers_batch_raw") or hasattr(active, "get_references_batch")
        to_process = plan[:total]

        async def _fetch_and_record_single(paper_id: str, number: int):
            aid = utils.canonical_arxiv_id(paper_id)
            print(f"[Raw references] Đang xử lý {number}/{total}: {aid}", flush=True)
            previous = ledger.get(aid, {})
            now = datetime.now(timezone.utc).isoformat()
            t0 = time.perf_counter()
            try:
                entries, completeness = await _fetch_all_pages(active, paper_id)
            except S2ApiNotFound:
                duration = round(time.perf_counter() - t0, 4)
                event = {"arxiv_id": aid, "status": "not_found", "raw_refs_total": 0,
                         "refs_with_arxiv_id": 0, "ref_time_sec": duration,
                         "fetched_at": now, "error": "HTTP 404"}
            except S2ApiAuthError:
                # Authentication is a global problem. Do not mark this paper
                # "not found" or continue sending requests with bad credentials.
                raise
            except (S2ApiError, ValueError, OSError) as exc:
                duration = round(time.perf_counter() - t0, 4)
                blocked = isinstance(exc, S2ApiPermanentError)
                state = "blocked" if blocked else "error"
                detail = recovery.progress_entry(aid, state, previous, error=str(exc))
                event = {"arxiv_id": aid, "status": state, "raw_refs_total": 0,
                         "refs_with_arxiv_id": 0, "ref_time_sec": duration,
                         "fetched_at": now, "error": str(exc),
                         "attempts": detail["attempts"],
                         "next_retry_at": detail["next_retry_at"]}
            else:
                duration = round(time.perf_counter() - t0, 4)
                rows = [_raw_record(paper_id, i, item, now) for i, item in enumerate(entries)]
                existing = raw.setdefault(aid, {})
                if any(i >= len(rows) for i in existing):
                    raise ValueError(f"Raw reference count changed for {aid}; manual reconciliation required")
                if any(existing[i]["raw_reference_entry"] != rows[i]["raw_reference_entry"]
                       for i in existing):
                    raise ValueError(f"Raw reference content changed for {aid}; manual reconciliation required")
                missing = [row for row in rows if row["ref_index"] not in existing]
                refs_with_arxiv = sum(bool(row["ref_arxiv_id"]) for row in rows)
                event = {"arxiv_id": aid, "status": "ok",
                         "raw_refs_total": len(rows),
                         "refs_with_arxiv_id": refs_with_arxiv,
                         "ref_time_sec": duration,
                         "payload_sha256": _digest(rows),
                         "completeness": completeness, "fetched_at": now, "error": ""}
                # Append only after ALL pages verified; event comes last.
                _append(root, missing, event)
                existing.update({r["ref_index"]: r for r in missing})
                ledger[aid] = event
                results.append(event)
                if progress_callback is not None:
                    try:
                        progress_callback(number, total, event)
                    except Exception as exc:
                        print(f"[Raw references] progress callback warning: {exc}", flush=True)
                return
            recovery.durable_append(root / "references_raw_ledger.jsonl", event)
            ledger[aid] = event
            results.append(event)
            if progress_callback is not None:
                try:
                    progress_callback(number, total, event)
                except Exception as exc:
                    print(f"[Raw references] progress callback warning: {exc}", flush=True)

        try:
            if can_batch:
                for b_start in range(0, len(to_process), eff_batch_size):
                    batch_papers = to_process[b_start : b_start + eff_batch_size]
                    t0 = time.perf_counter()
                    try:
                        batch_out = await _fetch_batch_papers(active, batch_papers)
                    except S2ApiAuthError:
                        raise
                    except Exception as exc:
                        print(f"[Raw references] Lỗi batch ({exc}); chuyển sang xử lý tuần tự từng bài...", flush=True)
                        for offset, pid in enumerate(batch_papers):
                            await _fetch_and_record_single(pid, b_start + offset + 1)
                        continue

                    batch_elapsed = max(0.0, time.perf_counter() - t0)
                    per_item_time = round(batch_elapsed / max(1, len(batch_papers)), 4)

                    for offset, (paper_id, entries) in enumerate(batch_out):
                        number = b_start + offset + 1
                        aid = utils.canonical_arxiv_id(paper_id)
                        now = datetime.now(timezone.utc).isoformat()
                        if entries is None:
                            event = {
                                "arxiv_id": aid,
                                "status": "not_found",
                                "raw_refs_total": 0,
                                "refs_with_arxiv_id": 0,
                                "ref_time_sec": per_item_time,
                                "fetched_at": now,
                                "error": "HTTP 404",
                            }
                            recovery.durable_append(root / "references_raw_ledger.jsonl", event)
                            ledger[aid] = event
                            results.append(event)
                            if progress_callback is not None:
                                try:
                                    progress_callback(number, total, event)
                                except Exception as exc:
                                    print(f"[Raw references] progress callback warning: {exc}", flush=True)
                        else:
                            rows = [_raw_record(paper_id, i, item, now) for i, item in enumerate(entries)]
                            existing = raw.setdefault(aid, {})
                            if any(i >= len(rows) for i in existing):
                                raise ValueError(f"Raw reference count changed for {aid}; manual reconciliation required")
                            if any(existing[i]["raw_reference_entry"] != rows[i]["raw_reference_entry"] for i in existing):
                                raise ValueError(f"Raw reference content changed for {aid}; manual reconciliation required")
                            missing = [row for row in rows if row["ref_index"] not in existing]
                            refs_with_arxiv = sum(bool(row["ref_arxiv_id"]) for row in rows)
                            event = {
                                "arxiv_id": aid,
                                "status": "ok",
                                "raw_refs_total": len(rows),
                                "refs_with_arxiv_id": refs_with_arxiv,
                                "ref_time_sec": per_item_time,
                                "payload_sha256": _digest(rows),
                                "completeness": "batch_endpoint",
                                "fetched_at": now,
                                "error": "",
                            }
                            _append(root, missing, event)
                            existing.update({r["ref_index"]: r for r in missing})
                            ledger[aid] = event
                            results.append(event)
                            if progress_callback is not None:
                                try:
                                    progress_callback(number, total, event)
                                except Exception as exc:
                                    print(f"[Raw references] progress callback warning: {exc}", flush=True)
            else:
                for number, paper_id in enumerate(to_process, 1):
                    await _fetch_and_record_single(paper_id, number)
        finally:
            if client is None:
                await active.close()
        return results

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        processed = asyncio.run(_execute())
    else:
        # Colab already has a running event loop; execute synchronously in one
        # owned worker and return only after all network and file I/O finishes.
        with ThreadPoolExecutor(max_workers=1) as pool:
            processed = pool.submit(lambda: asyncio.run(_execute())).result()
    counts = Counter(event["status"] for event in processed)
    csv_file = None
    if export_csv:
        try:
            csv_file = export_raw_reference_stats(root)
        except Exception as exc:
            print(f"[Raw references] Export CSV warning: {exc}", flush=True)
    return {"dry_run": False,
            "summary": {**summary, **dict(counts), "network_attempts": len(processed)},
            "processed": processed,
            "csv_path": str(csv_file) if csv_file else None}


def export_raw_reference_stats(
    data_dir: str | Path,
    *,
    csv_path: str | Path | None = None,
    ids: Iterable[str] | None = None,
) -> Path:
    """Atomically write Table 3 (m1_reference_stats.csv) snapshot matching project specs."""
    root = Path(data_dir)
    target_csv = Path(csv_path) if csv_path is not None else root / "csv" / "m1_reference_stats.csv"
    if target_csv.is_symlink() or (target_csv.exists() and not target_csv.is_file()):
        raise ValueError(f"Unsafe destination: {target_csv}")
    target_csv.parent.mkdir(parents=True, exist_ok=True)

    raw, ledger = _load_state(root)
    if ids is not None:
        target_aids = {utils.canonical_arxiv_id(str(x)) for x in ids if utils.canonical_arxiv_id(str(x)) in ledger}
    else:
        target_aids = set(ledger.keys())

    rows = []
    for aid in sorted(target_aids):
        event = ledger.get(aid, {})
        status = event.get("status", "pending")
        if status == "ok":
            ref_status = "success"
        elif status == "not_found":
            ref_status = "not_found"
        elif status in {"error", "blocked"}:
            ref_status = "failed"
        else:
            ref_status = "failed"

        crawled = int(event.get("raw_refs_total") if event.get("raw_refs_total") is not None else len(raw.get(aid, {})))
        if "refs_with_arxiv_id" in event:
            arxiv_refs = int(event["refs_with_arxiv_id"])
        else:
            arxiv_refs = sum(bool(r.get("ref_arxiv_id")) for r in raw.get(aid, {}).values())

        duration = round(float(event.get("ref_time_sec") or 0.0), 4)

        rows.append({
            "arxiv_id": _network_id(aid),
            "ref_status": ref_status,
            "num_crawled_refs": crawled,
            "num_arxiv_refs": arxiv_refs,
            "ref_time_sec": duration,
        })

    fieldnames = ["arxiv_id", "ref_status", "num_crawled_refs", "num_arxiv_refs", "ref_time_sec"]
    fd, tmp = tempfile.mkstemp(prefix=f".{target_csv.name}.", dir=target_csv.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, target_csv)
    finally:
        Path(tmp).unlink(missing_ok=True)
    return target_csv


def publish_references(
    data_dir: str | Path,
    *,
    out_path: str | Path | None = None,
) -> Path:
    """Publish unique verified reference edges from raw references into references.jsonl."""
    root = Path(data_dir)
    target = Path(out_path) if out_path is not None else root / "references.jsonl"
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise ValueError(f"Unsafe destination: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)

    raw_path = root / "references_raw.jsonl"
    existing_pairs = set()
    rows = []
    if target.is_file():
        for rec in _read_records(target):
            src, dst = rec.get("arxiv_id"), rec.get("ref_arxiv_id")
            if src and dst and utils.is_valid_arxiv_id(src) and utils.is_valid_arxiv_id(dst):
                pair = (utils.canonical_arxiv_id(src), utils.canonical_arxiv_id(dst))
                if pair not in existing_pairs:
                    existing_pairs.add(pair)
                    rows.append(rec)

    if raw_path.is_file():
        for item in _read_records(raw_path):
            ref_id = item.get("ref_arxiv_id")
            citing_id = item.get("arxiv_id")
            if not ref_id or not citing_id:
                continue
            if not utils.is_valid_arxiv_id(citing_id) or not utils.is_valid_arxiv_id(ref_id):
                continue
            canon_citing = utils.canonical_arxiv_id(citing_id)
            canon_ref = utils.canonical_arxiv_id(ref_id)
            pair = (canon_citing, canon_ref)
            if pair not in existing_pairs:
                existing_pairs.add(pair)
                rows.append({
                    "arxiv_id": canon_citing,
                    "ref_arxiv_id": canon_ref,
                    "ref_title": str(item.get("ref_title") or ""),
                    "ref_authors": list(item.get("ref_authors") or []),
                    "ref_submission_date": str(item.get("ref_submission_date") or ""),
                    "ref_s2_id": str(item.get("ref_s2_id") or ""),
                })

    fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            for r in rows:
                stream.write(json.dumps(r, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, target)
    finally:
        Path(tmp).unlink(missing_ok=True)
    return target


def prune_unverified_references(data_dir: str | Path) -> list[str]:
    """Prune partially written or mismatched raw reference and ledger entries.

    When network drops or interrupted batch writes leave ledger entries whose count
    or SHA-256 payload does not match references_raw.jsonl, this removes the broken
    entries so the affected papers can be cleanly re-crawled without conflicts.

    Returns the list of pruned canonical arXiv IDs.
    """
    root = Path(data_dir)
    ledger_path = root / "references_raw_ledger.jsonl"
    raw_path = root / "references_raw.jsonl"
    if not ledger_path.is_file():
        return []

    try:
        raw, ledger = _load_state(root)
    except Exception:
        raw = {}
        ledger = {}
        for row in utils.read_jsonl(ledger_path):
            if isinstance(row, dict) and row.get("arxiv_id"):
                ledger[row["arxiv_id"]] = row
        if raw_path.is_file():
            for row in utils.read_jsonl(raw_path):
                if isinstance(row, dict) and row.get("arxiv_id"):
                    idx = row.get("ref_index")
                    if isinstance(idx, int):
                        raw.setdefault(row["arxiv_id"], {})[idx] = row

    broken_ids = [
        aid for aid, event in ledger.items()
        if event.get("status") == "ok" and not _done(event, raw.get(aid, {}))
    ]
    if not broken_ids:
        return []

    broken_set = set(broken_ids)
    if raw_path.is_file():
        valid_raw = [r for r in utils.read_jsonl(raw_path) if r.get("arxiv_id") not in broken_set]
        utils.write_jsonl(raw_path, valid_raw)

    valid_ledger = [r for r in utils.read_jsonl(ledger_path) if r.get("arxiv_id") not in broken_set]
    utils.write_jsonl(ledger_path, valid_ledger)

    return broken_ids


