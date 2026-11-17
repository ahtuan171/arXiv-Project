"""Offline checks for Duy's integrated EDA and the TV5 Jupyter Book."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from book._m1_report import load_verified_summary, summary_path

BOOK = Path(__file__).resolve().parents[1]
ROOT = BOOK.parent
SOURCE = BOOK / "_data" / "m1_summary.json"


def base_report() -> dict:
    return json.loads(SOURCE.read_text(encoding="utf-8"))


def validate(data: dict):
    with tempfile.TemporaryDirectory() as tmp:
        file = Path(tmp) / "summary.json"
        file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return load_verified_summary(file)


class M1BookContractTests(unittest.TestCase):
    def test_missing_summary_renders_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(load_verified_summary(Path(tmp) / "missing.json"))

    def test_current_summary_is_reconciled(self):
        data = load_verified_summary(SOURCE)
        self.assertEqual(data["scope"]["assigned_target_papers"], 25000)
        self.assertEqual(data["scope"]["actual_collected_papers"], 24999)
        self.assertEqual(data["benchmark_evidence"]["batch_aggregated_metrics"]["batch_count"], 36)
        self.assertEqual(len(data["benchmark_batches"]), 36)
        self.assertIsNone(data["coverage"]["raw_reference_papers_verified"])

    def test_paper_scope_disagreement_fails_closed(self):
        invalid = base_report()
        invalid["scope"]["missing_papers_count"] = 0
        with self.assertRaisesRegex(ValueError, "Paper scope/counts disagree"):
            validate(invalid)

    def test_source_status_disagreement_fails_closed(self):
        invalid = base_report()
        invalid["source_statuses"]["tex_source_success"] += 1
        with self.assertRaisesRegex(ValueError, "Version status totals disagree"):
            validate(invalid)

    def test_source_rate_disagreement_fails_closed(self):
        invalid = base_report()
        invalid["coverage"]["source_coverage_pct"] = 99.9
        with self.assertRaisesRegex(ValueError, "Source coverage percentage disagrees"):
            validate(invalid)

    def test_unverified_reference_count_must_not_be_asserted(self):
        invalid = base_report()
        invalid["coverage"]["raw_reference_papers_verified"] = 24941
        with self.assertRaisesRegex(ValueError, "without SHA-256 evidence"):
            validate(invalid)

    def test_incorrect_batch_count_fails_closed(self):
        invalid = base_report()
        invalid["benchmark_evidence"]["batch_aggregated_metrics"]["batch_count"] = 11
        with self.assertRaisesRegex(ValueError, "Benchmark batch count disagrees"):
            validate(invalid)

    def test_nan_json_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "nan.json"
            file.write_text('{"schema_version":1,"scope":NaN}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Non-standard JSON constant"):
                load_verified_summary(file)

    def test_explicit_summary_path(self):
        with patch.dict(os.environ, {"M1_SUMMARY_JSON": "/tmp/other-m1.json"}):
            self.assertEqual(summary_path(), Path("/tmp/other-m1.json"))

    def test_book_and_eda_notebook_code_compiles(self):
        for path in (BOOK / "01_m1_crawling.ipynb", ROOT / "notebooks/01_eda_m1.ipynb"):
            with self.subTest(path=path):
                notebook = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(notebook["nbformat"], 4)
                for index, cell in enumerate(notebook["cells"]):
                    if cell["cell_type"] == "code":
                        source = cell["source"]
                        compile("".join(source) if isinstance(source, list) else source, f"{path} cell {index}", "exec")

    def test_no_hardcoded_fallbacks_or_outdated_metric_keys(self):
        notebook = json.loads((BOOK / "01_m1_crawling.ipynb").read_text(encoding="utf-8"))
        code = "\n".join("".join(c["source"]) if isinstance(c["source"], list) else c["source"] for c in notebook["cells"] if c["cell_type"] == "code")
        self.assertNotIn("eda.get('papers_total', 24999)", code)
        self.assertNotIn('"verified_success": "success"', code)
        self.assertNotIn("Tổng hợp từ 11 batch", code)
        self.assertIn("load_verified_summary(summary_path)", code)


if __name__ == "__main__":
    unittest.main()
