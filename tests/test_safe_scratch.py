"""Safe Scratch tests with synthetic filesystem fixtures; never access production Drive."""
import pytest
from src.scripts.safe_scratch import start_scratch_session, sync_scratch_session


def prepare(tmp_path):
    remote = tmp_path / "drive" / "data"
    remote.mkdir(parents=True)
    (remote / "csv").mkdir()
    (remote / "raw").mkdir()
    (remote / "papers.jsonl").write_text('{"id":1}\n')
    (remote / "csv" / "stats.csv").write_text("old")
    (remote / "raw" / "existing_id").mkdir()
    (remote / "raw" / "existing_id" / "main.tex").write_text("ORIGINAL")
    local = tmp_path / "local"
    local.mkdir()
    return remote, local


def test_setup_only_copies_small_files(tmp_path):
    remote, local = prepare(tmp_path)
    session = start_scratch_session(remote, scratch_parent=local)
    assert (session.scratch_dir / "papers.jsonl").read_bytes() == (remote / "papers.jsonl").read_bytes()
    assert (session.scratch_dir / "csv" / "stats.csv").read_text() == "old"
    assert list((session.scratch_dir / "raw").iterdir()) == []
    assert (remote / "raw" / "existing_id" / "main.tex").read_text() == "ORIGINAL"


def test_noop_sync_does_not_modify_drive(tmp_path):
    remote, local = prepare(tmp_path)
    session = start_scratch_session(remote, scratch_parent=local)
    result = sync_scratch_session(session)
    assert result["updated_small_files"] == 0
    assert result["new_raw_folders"] == 0


def test_changed_remote_causes_abort_without_overwrite(tmp_path):
    remote, local = prepare(tmp_path)
    session = start_scratch_session(remote, scratch_parent=local)
    (session.scratch_dir / "papers.jsonl").write_text("LOCAL UPDATE")
    (remote / "papers.jsonl").write_text("OTHER WRITER")
    with pytest.raises(RuntimeError, match="Drive changed"):
        sync_scratch_session(session)
    assert (remote / "papers.jsonl").read_text() == "OTHER WRITER"


def test_new_raw_published_without_overwriting_existing(tmp_path):
    remote, local = prepare(tmp_path)
    session = start_scratch_session(remote, scratch_parent=local)
    folder = session.scratch_dir / "raw" / "new_id"
    folder.mkdir()
    (folder / "main.tex").write_text("NEW")
    assert sync_scratch_session(session)["new_raw_folders"] == 1
    assert (remote / "raw" / "new_id" / "main.tex").read_text() == "NEW"
    assert (remote / "raw" / "existing_id" / "main.tex").read_text() == "ORIGINAL"
    assert sync_scratch_session(session)["new_raw_folders"] == 0


def test_existing_raw_collision_blocks_all_sync(tmp_path):
    remote, local = prepare(tmp_path)
    session = start_scratch_session(remote, scratch_parent=local)
    (session.scratch_dir / "papers.jsonl").write_text("LOCAL")
    folder = session.scratch_dir / "raw" / "existing_id"
    folder.mkdir()
    (folder / "main.tex").write_text("OVERWRITE")
    with pytest.raises(FileExistsError, match="refusing overwrite"):
        sync_scratch_session(session)
    assert (remote / "papers.jsonl").read_text() == '{"id":1}\n'
    assert (remote / "raw" / "existing_id" / "main.tex").read_text() == "ORIGINAL"


def test_small_file_sync_tracks_baseline(tmp_path):
    remote, local = prepare(tmp_path)
    session = start_scratch_session(remote, scratch_parent=local)
    (session.scratch_dir / "csv" / "stats.csv").write_text("updated")
    assert sync_scratch_session(session)["updated_small_files"] == 1
    assert (remote / "csv" / "stats.csv").read_text() == "updated"
    assert sync_scratch_session(session)["updated_small_files"] == 0
