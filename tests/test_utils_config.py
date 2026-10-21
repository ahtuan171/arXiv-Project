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


def test_rate_limiter_enforces_interval():
    limiter = utils.RateLimiter(0.2)
    limiter.wait()
    start = time.monotonic()
    limiter.wait()
    assert time.monotonic() - start >= 0.19


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
