"""Synthetic tests of compressed M1 backup on local fixtures, no Drive writes."""
import tarfile
import pytest
from pathlib import Path
from src.scripts.backup_m1_archive import backup_dataset_archive, verify_backup_archive

def make_dataset(root):
    (root / "raw" / "2501_00001").mkdir(parents=True)
    (root / "csv").mkdir()
    (root / "papers.jsonl").write_text('{"arxiv_id":"2501_00001"}\n')
    (root / "references.jsonl").write_text('{"arxiv_id":"2501_00001"}\n')
    (root / "raw" / "2501_00001" / "main.tex").write_text("LaTeX source")
    (root / "csv" / "stats.csv").write_text("a,b\n1,2\n")
    return {p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob("*") if p.is_file()}

def test_verified_archive_and_source_untouched(tmp_path):
    before=make_dataset(tmp_path/"data")
    out=backup_dataset_archive(tmp_path/"data",tmp_path/"backups",temp_dir=tmp_path)
    assert out["status"]=="verified"
    assert verify_backup_archive(out["archive"],deep=True)["ok"]
    with tarfile.open(out["archive"],"r:gz") as tar:
        assert {name:tar.extractfile(name).read() for name in before}==before
    assert {p.relative_to(tmp_path/"data").as_posix():p.read_bytes() for p in (tmp_path/"data").rglob("*") if p.is_file()}==before

def test_corruption_detected(tmp_path):
    make_dataset(tmp_path/"data")
    out=backup_dataset_archive(tmp_path/"data",tmp_path/"backups",temp_dir=tmp_path)
    with open(out["archive"],"ab") as f: f.write(b"bad")
    assert not verify_backup_archive(out["archive"])["ok"]

def test_rejects_bad_destination_and_symlink(tmp_path):
    make_dataset(tmp_path/"data")
    with pytest.raises(ValueError):
        backup_dataset_archive(tmp_path/"data",tmp_path/"data"/"backups",temp_dir=tmp_path)
    (tmp_path/"data"/"raw"/"link").symlink_to(tmp_path/"data"/"papers.jsonl")
    with pytest.raises(ValueError,match="symlink"):
        backup_dataset_archive(tmp_path/"data",tmp_path/"backups",temp_dir=tmp_path)

def test_missing_dataset_fails(tmp_path):
    with pytest.raises(FileNotFoundError):
        backup_dataset_archive(tmp_path/"missing",tmp_path/"backups",temp_dir=tmp_path)

def test_insufficient_local_space(tmp_path,monkeypatch):
    make_dataset(tmp_path/"data")
    import src.scripts.backup_m1_archive as mod
    monkeypatch.setattr(mod.shutil,"disk_usage",lambda _ : type("D",(),{"free":0})())
    with pytest.raises(RuntimeError,match="Not enough LOCAL scratch space"):
        backup_dataset_archive(tmp_path/"data",tmp_path/"backups",temp_dir=tmp_path)
