"""Offline checks for the M1 Jupyter Book summary contract.

Run with: python -m unittest discover -s book/tests -v
No Google Drive mount, API key, or production data is required.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from book._m1_report import load_verified_summary, summary_path


def sample() -> dict:
    return {
        "schema_version": 1,
        "coverage": {
            "valid_metadata_papers": 3,
            "expected_versions": 5,
            "verified_source_versions": 4,
            "source_coverage_pct": 80.0,
            "raw_reference_papers_verified": 2,
        },
        "source_statuses": {
            "verified_success": 3,
            "verified_pdf_only": 1,
            "pending": 0,
            "failed": 1,
            "not_found": 0,
            "conflicts": 0,
            "missing_files": 0,
        },
        "benchmark_evidence": {"official_version_pipeline_metrics": None},
        "missing_evidence": ["CPU-only end-to-end benchmark"],
        "raw_reference_sample": {"verified_reference_papers": 2},
    }


class SummaryContractTests(unittest.TestCase):
    def test_missing_summary_is_draft_not_error(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertIsNone(load_verified_summary(Path(folder) / "absent.json"))

    def test_valid_summary(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "m1_summary.json"
            path.write_text(json.dumps(sample()), encoding="utf-8")
            self.assertEqual(load_verified_summary(path)["coverage"]["expected_versions"], 5)

    def test_invalid_version_totals_fail_closed(self):
        wrong = sample()
        wrong["source_statuses"]["verified_success"] = 4
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "m1_summary.json"
            path.write_text(json.dumps(wrong), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "disagrees"):
                load_verified_summary(path)

    def test_invalid_json_fails_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "m1_summary.json"
            path.write_text("{not valid}", encoding="utf-8")
            with self.assertRaises(json.JSONDecodeError):
                load_verified_summary(path)

    def test_explicit_path_is_honored(self):
        with patch.dict(os.environ, {"M1_SUMMARY_JSON": "/tmp/team-m1.json"}):
            self.assertEqual(summary_path(), Path("/tmp/team-m1.json"))

    def test_no_fake_metrics(self):
        self.assertIsNone(sample()["benchmark_evidence"]["official_version_pipeline_metrics"])


if __name__ == "__main__":
    unittest.main()
