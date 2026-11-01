"""Synthetic tests of the member-scoped backup utility (no Drive access)."""
from pathlib import Path

import pytest

from src.scripts.backup_m1 import backup_dataset, backup_plan, verify_backup


def make_data(root):
    (root / "raw" / "2501_00001").mkdir(parents=True)
    (root / "csv").mkdir()
    (root / "papers.jsonl").write_text('{"arxiv_id":"2501_00001"}\n')
    (root / "references.jsonl").write_text('{"arxiv_id":"2501_00001"}\n')
    (root / "raw" / "2501_00001" / "main.tex").write_text("\\includegraphics{fig.pdf}\n")
    (root / "csv" / "stats.csv").write_text("id,status\n2501_00001,success\n")
    return {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_backup_copies_full_tree_and_never_overwrites(tmp_path):
    data = tmp_path / "data"
    before = make_data(data)
    root = tmp_path / "backups"
    plan = backup_plan(data, root)
    assert plan["raw_file_count"] == 1
    assert not root.exists()  # Preflight must be read-only.
    first = backup_dataset(data, root)
    second = backup_dataset(data, root)
    assert first["status"] == "verified"
    assert first["backup_dir"] != second["backup_dir"]
    assert verify_backup(first["backup_dir"], data)["ok"]
    assert all((Path(first["backup_dir"]) / rel).read_bytes() == contents for rel, contents in before.items())
    assert before == {p.relative_to(data): p.read_bytes() for p in data.rglob("*") if p.is_file()}


def test_verification_detects_corruption(tmp_path):
    data = tmp_path / "data"
    make_data(data)
    result = backup_dataset(data, tmp_path / "backups")
    (Path(result["backup_dir"]) / "papers.jsonl").write_text("altered")
    assert not verify_backup(result["backup_dir"])["ok"]


def test_rejects_unsafe_destinations_and_symlinks(tmp_path):
    data = tmp_path / "data"
    make_data(data)
    with pytest.raises(ValueError, match="destination"):
        backup_plan(data, data / "backups")
    (data / "raw" / "link").symlink_to(data / "papers.jsonl")
    with pytest.raises(ValueError, match="symlink"):
        backup_plan(data, tmp_path / "backups")


def test_rejects_incomplete_dataset(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    with pytest.raises(FileNotFoundError):
        backup_dataset(data, tmp_path / "backups")
