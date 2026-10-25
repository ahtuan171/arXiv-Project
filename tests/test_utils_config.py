import asyncio
import json
import time

import pytest

from src import config, utils


def test_clean_arxiv_id():
    assert utils.clean_arxiv_id("cs/0101001") == "cs_0101001"
    assert utils.clean_arxiv_id("2301.12345v2") == "2301_12345v2"
    assert utils.clean_arxiv_id("arXiv:2301.12345") == "arXiv_2301_12345"


def test_range_within_month():
    rng = config.ArxivRange("2025-04", 12523, "2504", 12526)
    assert list(rng.iter_ids()) == ["2504.12523", "2504.12524", "2504.12525", "2504.12526"]


def test_range_crossing_month_needs_sizes():
    rng = config.ArxivRange("2504", 17523, "2505", 2)
    with pytest.raises(ValueError):
        list(rng.iter_ids())
    ids = list(rng.iter_ids({"2504": 17524}))
    assert ids == ["2504.17523", "2504.17524", "2505.00001", "2505.00002"]
    assert rng.count({"2504": 17524}) == 4


def test_range_crossing_year():
    rng = config.ArxivRange("2512", 1, "2601", 1)
    assert list(rng.iter_ids({"2512": 2})) == ["2512.00001", "2512.00002", "2601.00001"]


def test_invalid_range():
    with pytest.raises(ValueError):
        config.ArxivRange("2505", 1, "2504", 1)


def test_ranges_yaml_ids_are_decimal_not_octal(tmp_path):
    path = tmp_path / "ranges.yaml"
    path.write_text(
        "members:\n"
        "  m1: {start_month: '2025-01', start_id: 07642, end_month: '2025-01', end_id: 07644}\n"
    )
    rng = config.load_member_range("m1", path)
    assert (rng.start_id, rng.end_id) == (7642, 7644)
    assert list(rng.iter_ids()) == ["2501.07642", "2501.07643", "2501.07644"]


def test_load_member_range_requires_values(tmp_path):
    path = tmp_path / "ranges.yaml"
    path.write_text("members:\n  m1: {start_month: null, start_id: null, end_month: null, end_id: null}\n")
    with pytest.raises(ValueError):
        config.load_member_range("m1", path)
    path.write_text("members:\n  m1: {start_month: '2025-04', start_id: 1, end_month: '2025-04', end_id: 5}\n")
    assert config.load_member_range("m1", path).count() == 5


def test_load_env_file_strips_spaces_and_quotes(tmp_path, monkeypatch):
    monkeypatch.delenv("S2_API_KEY", raising=False)
    monkeypatch.delenv("ARXIV_MEMBER", raising=False)
    env = tmp_path / ".env"
    env.write_text('S2_API_KEY = "s2k-test"\nexport ARXIV_MEMBER=member5\n', encoding="utf-8")
    config.load_env_file(env)
    assert config.os.environ["S2_API_KEY"] == "s2k-test"
    assert config.os.environ["ARXIV_MEMBER"] == "member5"


def test_load_env_file_does_not_override_existing(tmp_path, monkeypatch):
    monkeypatch.setenv("S2_API_KEY", "from-shell")
    env = tmp_path / ".env"
    env.write_text("S2_API_KEY=from-file\n", encoding="utf-8")
    config.load_env_file(env)
    assert config.os.environ["S2_API_KEY"] == "from-shell"


def test_jsonl_roundtrip_and_resume(tmp_path):
    path = tmp_path / "papers.jsonl"
    utils.append_jsonl(path, {"arxiv_id": "2504_00001", "title": "Tiếng Việt\nxuống dòng"})
    utils.append_jsonl(path, {"arxiv_id": "2504_00002", "title": ""})
    with open(path, "a", encoding="utf-8") as handle:
        handle.write('{"arxiv_id": "2504_0')  # truncated by a crash
    assert len(path.read_text(encoding="utf-8").splitlines()) == 3
    assert utils.load_done_ids(path) == {"2504_00001", "2504_00002"}
    assert json.loads(path.read_text(encoding="utf-8").splitlines()[0])["title"].startswith("Tiếng")


def test_append_jsonl_many_writes_one_batch(tmp_path):
    path = tmp_path / "records.jsonl"
    written = utils.append_jsonl_many(path, [{"id": index} for index in range(3)], fsync=True)
    assert written == 3
    assert [record["id"] for record in utils.read_jsonl(path)] == [0, 1, 2]


def test_load_latest_records_keeps_last_per_id(tmp_path):
    path = tmp_path / "ledger.jsonl"
    utils.append_jsonl_many(path, [
        {"arxiv_id": "a", "status": "error", "attempts": 1},
        {"arxiv_id": "a", "status": "ok", "attempts": 1},
        {"arxiv_id": "b", "status": "not_found"},
    ])
    latest = utils.load_latest_records(path)
    assert latest["a"]["status"] == "ok"
    assert latest["b"]["status"] == "not_found"


def test_normalize_and_canonical_arxiv_id():
    assert utils.normalize_arxiv_id("arXiv:2504.12345v3") == "2504.12345"
    assert utils.normalize_arxiv_id("hep-th/9901001v2") == "hep-th/9901001"
    assert utils.normalize_arxiv_id(None) == ""
    assert utils.canonical_arxiv_id("arXiv:2504.12345v3") == "2504_12345"


def test_rate_limiter_enforces_interval():
    limiter = utils.RateLimiter(0.2)
    limiter.wait()
    start = time.monotonic()
    limiter.wait()
    assert time.monotonic() - start >= 0.18


def test_async_rate_limiter_block_pauses_all_tabs():
    async def scenario():
        limiter = utils.AsyncRateLimiter(0)
        limiter.block(0.2)
        start = time.monotonic()
        await limiter.wait()
        return time.monotonic() - start

    assert asyncio.run(scenario()) >= 0.19


def test_retry_gives_up_and_succeeds():
    calls = []

    @utils.retry(max_attempts=3, backoff=0.01)
    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("boom")
        return "ok"

    assert flaky() == "ok"

    @utils.retry(max_attempts=2, backoff=0.01)
    def broken():
        raise RuntimeError("always")

    with pytest.raises(RuntimeError):
        broken()


def test_measure_reports_time():
    with utils.measure("sleep") as result:
        time.sleep(0.05)
    assert result.seconds >= 0.05
