"""Shared fixtures for the arXiv-Project test suite.

The ``fake_member_range`` autouse-fixture patches :func:`config.load_member_range`
and :func:`config.load_month_sizes` so that crawler tests are fully independent
of the live ``ranges.yaml``.  Tests that call ``load_member_range`` with an
explicit ``path`` argument (e.g. config-loading tests) still hit the real function.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from src import config
from src.config import ArxivRange


# The test IDs 2501.07642 .. 2501.07644 sit inside this range:
_TEST_RANGE = ArxivRange(
    start_month="2025-01",
    start_id=7642,
    end_month="2025-01",
    end_id=12641,
)

_real_load_member_range = config.load_member_range
_real_load_month_sizes = config.load_month_sizes


@pytest.fixture(autouse=True)
def fake_member_range():
    """Patch config so tests that use the default ranges.yaml get a fixed range.

    Calls with an explicit ``path`` keyword are forwarded to the real function
    so that config-loading tests keep working.
    """

    def _patched_load_member_range(member=None, path=config.RANGES_FILE):
        if path != config.RANGES_FILE:
            return _real_load_member_range(member, path)
        return _TEST_RANGE

    def _patched_load_month_sizes(path=config.RANGES_FILE):
        if path != config.RANGES_FILE:
            return _real_load_month_sizes(path)
        return {}

    with (
        patch("src.config.load_member_range", side_effect=_patched_load_member_range),
        patch("src.config.load_month_sizes", side_effect=_patched_load_month_sizes),
    ):
        yield
