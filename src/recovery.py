"""Durable progress and recovery shared by the three collection stages.

The ledger records attempts, not just completed IDs. CSV tables remain reports;
only the latest state and the actual output determine whether a paper is done.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable, Iterable, Optional

from src import config, utils

logger = logging.getLogger(__name__)


class FetchFailure(RuntimeError):
    """A classified remote failure; local filesystem errors must propagate."""

    def __init__(self, message: str, error_type: str = "transient") -> None:
        super().__init__(message)
        self.error_type = error_type


def durable_append(path: Path, row: dict) -> None:
    """Isolate a torn final line before appending so restart cannot lose a row."""
    utils.append_jsonl(path, row, fsync=True)


def latest_rows(path: Path, key: Callable[[str], str] = utils.clean_arxiv_id) -> dict[str, dict]:
    """Load latest valid object per normalized ID, including legacy files."""
    result: dict[str, dict] = {}
    if path.exists():
        for row in utils.read_jsonl(path):
            if isinstance(row, dict) and row.get('arxiv_id'):
                result[key(str(row['arxiv_id']))] = row
    return result


def load_stats(jsonl: Path, csv: Path,
               key: Callable[[str], str] = utils.clean_arxiv_id) -> dict[str, dict]:
    """Use CSV as a baseline and overlay newer JSONL results without float IDs."""
    import pandas as pd
    result = {}
    if csv.exists() and csv.stat().st_size:
        for row in pd.read_csv(csv, dtype={'arxiv_id': str}, keep_default_na=False).to_dict('records'):
            if row.get('arxiv_id'):
                result[key(str(row['arxiv_id']))] = row
    result.update(latest_rows(jsonl, key))
    return result


def should_skip(record: Optional[dict], *, retry_errors: bool = False,
                retry_not_found: bool = False, retry_partial: bool = False) -> bool:
    """Never equate exhausted retries with success; defer until the cooldown ends."""
    if not record:
        return False
    status = record.get('status')
    if status in {'ok', 'pdf_only'}:
        return True
    if status == 'partial':
        return not retry_partial
    if status == 'not_found':
        return not retry_not_found
    if status == 'blocked':
        return not retry_errors
    if status == 'error':
        if retry_errors:
            return False
        try:
            return float(record.get('next_retry_at') or 0) > time.time()
        except (TypeError, ValueError):
            return False
    return False


def progress_entry(arxiv_id: str, status: str, previous: Optional[dict] = None,
                   *, error: str = '', error_type: str = '', **extra) -> dict:
    """Count consecutive failed paper runs separately from per-request attempts."""
    try:
        attempts = max(0, int((previous or {}).get('attempts') or 0))
    except (TypeError, ValueError):
        attempts = 0
    attempts = attempts + 1 if status in {'error', 'blocked'} else 0
    now = time.time()
    # Cap the exponent as well as the delay for arbitrarily old legacy counters.
    delay = min(config.RECOVERY_COOLDOWN * 2 ** min(max(0, attempts - 1), 16),
                config.RECOVERY_MAX_COOLDOWN)
    return {**extra, 'arxiv_id': arxiv_id, 'status': status, 'attempts': attempts,
            'last_attempt_at': datetime.fromtimestamp(now, timezone.utc).isoformat(),
            'next_retry_at': now + delay if status == 'error' else None,
            'error_type': error_type, 'error': str(error)[:1000]}


class RecoveryLedger:
    """Append-only stage progress, read by latest normalized ID on restart."""

    def __init__(self, path: Path, key: Callable[[str], str] = utils.clean_arxiv_id) -> None:
        self.path, self.key = path, key
        self.rows = latest_rows(path, key)

    def get(self, arxiv_id: str) -> Optional[dict]:
        return self.rows.get(self.key(arxiv_id))

    def record(self, arxiv_id: str, status: str, **extra) -> dict:
        row = progress_entry(self.key(arxiv_id), status, self.get(arxiv_id), **extra)
        durable_append(self.path, row)
        self.rows[self.key(arxiv_id)] = row
        return row


def atomic_write_jsonl(path: Path, rows: Iterable[dict]) -> None:
    """Replace a snapshot only after its complete new contents reach disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + '\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


class RecordStore:
    """Metadata snapshot plus durable journal, compacted once per run.

    The journal is committed before the ledger, so even a crash before snapshot
    replacement leaves a readable success. Replaying it twice is idempotent.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.journal = path.with_name(path.name + '.journal')
        self.rows = latest_rows(path)
        self.rows.update(latest_rows(self.journal))

    def put(self, row: dict) -> None:
        key = utils.clean_arxiv_id(row['arxiv_id'])
        previous = self.rows.get(key)
        # A forced refresh that fails must not destroy usable metadata.
        if previous and not previous.get('error') and row.get('error'):
            return
        durable_append(self.journal, row)
        self.rows[key] = row

    def compact(self) -> None:
        atomic_write_jsonl(self.path, self.rows.values())
        self.journal.unlink(missing_ok=True)


def export_stats(rows: dict[str, dict], columns: list[str], csv: Path):
    """Atomically refresh the report; journal remains the durable history."""
    import pandas as pd
    df = pd.DataFrame(rows.values()).reindex(columns=columns)
    csv.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f'.{csv.name}.', dir=csv.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as handle:
            df.to_csv(handle, index=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, csv)
    finally:
        Path(name).unlink(missing_ok=True)
    return df


def parse_retry_after(value: Optional[str]) -> Optional[float]:
    """Accept both Retry-After seconds and HTTP dates, with a bounded delay."""
    if not value:
        return None
    try:
        delay = float(value)
    except (TypeError, ValueError):
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            delay = date.timestamp() - time.time()
        except (TypeError, ValueError, OverflowError):
            return None
    return min(max(0.0, delay), config.MAX_RETRY_AFTER)
