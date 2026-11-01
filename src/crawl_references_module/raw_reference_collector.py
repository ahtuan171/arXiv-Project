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
import hashlib
import json
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


def _append(root: Path, records: list[dict], event: dict) -> None:
    if records:
        recovery.durable_append_many(root / "references_raw.jsonl", records)
    recovery.durable_append(root / "references_raw_ledger.jsonl", event)


def collect_raw_references(
    data_dir: str | Path,
    *, ids: Iterable[str],
    max_papers: int = 3,
    dry_run: bool = True,
    retry_errors: bool = False,
    retry_not_found: bool = False,
    client: Any = None,
    progress_callback: ProgressFn | None = None,
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
        return {"dry_run": False, "summary": {**summary, "network_attempts": 0}, "processed": []}
    if (root / "references_raw.jsonl").is_symlink() or (root / "references_raw_ledger.jsonl").is_symlink():
        raise ValueError("Refusing to write through a symlink")

    async def _execute() -> list[dict]:
        active = client if client is not None else S2ApiClient()
        results = []
        if hasattr(active, "start"):
            await active.start(1)
        try:
            for number, paper_id in enumerate(plan[:total], 1):
                aid = utils.canonical_arxiv_id(paper_id)
                print(f"[Raw references] Đang xử lý {number}/{total}: {aid}", flush=True)
                previous = ledger.get(aid, {})
                now = datetime.now(timezone.utc).isoformat()
                try:
                    entries, completeness = await _fetch_all_pages(active, paper_id)
                except S2ApiNotFound:
                    event = {"arxiv_id": aid, "status": "not_found", "raw_refs_total": 0,
                             "fetched_at": now, "error": "HTTP 404"}
                except S2ApiAuthError:
                    # Authentication is a global problem. Do not mark this paper
                    # "not found" or continue sending requests with bad credentials.
                    raise
                except (S2ApiError, ValueError, OSError) as exc:
                    blocked = isinstance(exc, S2ApiPermanentError)
                    state = "blocked" if blocked else "error"
                    detail = recovery.progress_entry(aid, state, previous, error=str(exc))
                    event = {"arxiv_id": aid, "status": state, "raw_refs_total": 0,
                             "fetched_at": now, "error": str(exc),
                             "attempts": detail["attempts"],
                             "next_retry_at": detail["next_retry_at"]}
                else:
                    rows = [_raw_record(paper_id, i, item, now) for i, item in enumerate(entries)]
                    existing = raw.setdefault(aid, {})
                    if any(i >= len(rows) for i in existing):
                        raise ValueError(f"Raw reference count changed for {aid}; manual reconciliation required")
                    if any(existing[i]["raw_reference_entry"] != rows[i]["raw_reference_entry"]
                           for i in existing):
                        raise ValueError(f"Raw reference content changed for {aid}; manual reconciliation required")
                    missing = [row for row in rows if row["ref_index"] not in existing]
                    event = {"arxiv_id": aid, "status": "ok",
                             "raw_refs_total": len(rows),
                             "refs_with_arxiv_id": sum(bool(row["ref_arxiv_id"]) for row in rows),
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
                    continue
                recovery.durable_append(root / "references_raw_ledger.jsonl", event)
                ledger[aid] = event
                results.append(event)
                if progress_callback is not None:
                    try:
                        progress_callback(number, total, event)
                    except Exception as exc:
                        print(f"[Raw references] progress callback warning: {exc}", flush=True)
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
    return {"dry_run": False,
            "summary": {**summary, **dict(counts), "network_attempts": len(processed)},
            "processed": processed}
