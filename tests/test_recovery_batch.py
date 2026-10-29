from pathlib import Path
from src import recovery, utils


def test_durable_append_many(tmp_path):
    p = tmp_path / "test.jsonl"
    rows = [{"arxiv_id": "2501.00001", "v": 1}, {"arxiv_id": "2501.00002", "v": 2}]
    count = recovery.durable_append_many(p, rows)
    assert count == 2

    loaded = list(utils.read_jsonl(p))
    assert len(loaded) == 2
    assert loaded[0]["arxiv_id"] == "2501.00001"
    assert loaded[1]["arxiv_id"] == "2501.00002"


def test_recovery_ledger_record_many(tmp_path):
    ledger_p = tmp_path / "test.ledger.jsonl"
    ledger = recovery.RecoveryLedger(ledger_p)

    entries = [
        ("2501.00001", "ok", {"refs": 5}),
        ("2501.00002", "not_found", {"error": "404"}),
        {"arxiv_id": "2501.00003", "status": "error", "error": "timeout"},
    ]
    recorded = ledger.record_many(entries)
    assert len(recorded) == 3
    assert ledger.get("2501.00001")["status"] == "ok"
    assert ledger.get("2501.00002")["status"] == "not_found"
    assert ledger.get("2501.00003")["status"] == "error"

    # Reload from disk
    ledger2 = recovery.RecoveryLedger(ledger_p)
    assert ledger2.get("2501.00001")["status"] == "ok"
    assert ledger2.get("2501.00002")["status"] == "not_found"
    assert ledger2.get("2501.00003")["status"] == "error"


def test_record_store_put_many(tmp_path):
    store_p = tmp_path / "papers.jsonl"
    store = recovery.RecordStore(store_p)

    rows = [
        {"arxiv_id": "2501.00001", "title": "Paper 1"},
        {"arxiv_id": "2501.00002", "title": "Paper 2"},
    ]
    store.put_many(rows)
    assert store.rows["2501_00001"]["title"] == "Paper 1"
    assert store.rows["2501_00002"]["title"] == "Paper 2"

    store.compact()
    assert store_p.exists()
    assert not store.journal.exists()

    store2 = recovery.RecordStore(store_p)
    assert store2.rows["2501_00001"]["title"] == "Paper 1"


def test_empty_batch_operations(tmp_path):
    p = tmp_path / "empty.jsonl"
    assert recovery.durable_append_many(p, []) == 0
    assert not p.exists()

    ledger = recovery.RecoveryLedger(tmp_path / "ledger.jsonl")
    assert ledger.record_many([]) == []

    store = recovery.RecordStore(tmp_path / "store.jsonl")
    store.put_many([])
    assert len(store.rows) == 0


def test_download_papers_range_flushes_buffer(tmp_path, monkeypatch):
    from src.download_module.downloader import DownloadStats, download_papers_range
    monkeypatch.setattr(
        "src.download_module.downloader.download_single_paper",
        lambda arxiv_id, **kw: DownloadStats(arxiv_id, "success", True, 100, 50, 1, 0.01),
    )
    raw_dir = tmp_path / "raw"
    csv_dir = tmp_path / "csv"
    # Run with 2 papers and batch_size=10; must still flush all 2 to disk
    df = download_papers_range(
        raw_dir=raw_dir,
        csv_dir=csv_dir,
        id_list=["2502.00001", "2502.00002"],
        batch_size=10,
        resume=False,
        lock=False,
    )
    assert len(df) == 2
    jsonl_stats = list(utils.read_jsonl(csv_dir / "m1_download_stats.jsonl"))
    assert len(jsonl_stats) == 2
    ledger_stats = list(utils.read_jsonl(csv_dir / "m1_download_stats.ledger.jsonl"))
    assert len(ledger_stats) == 2

