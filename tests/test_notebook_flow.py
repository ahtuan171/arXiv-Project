"""The Colab notebook can start from an empty data directory and follows M1 flow."""
import ast
import json
from pathlib import Path

NOTEBOOK = Path(__file__).resolve().parents[1] / 'src/milestone1_pipeline.ipynb'


def test_setup_and_collection_steps_are_separate_and_ordered():
    notebook = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
    codes = [''.join(cell['source']) for cell in notebook['cells'] if cell['cell_type'] == 'code']
    assert len(codes) == 12
    anchors = [
        'drive.mount(', 'requirements.txt', 'userdata.get(', 'start_id =',
        'SystemBenchmarkTracker(', 'crawl_papers(', 'audit_missing_versions(',
        'run_version_downloads(', 'collect_raw_references(',
        'audit_m1_integrity(', 'tracker.stop(', 'build_m1_report(',
    ]
    for code, anchor in zip(codes, anchors):
        ast.parse(code)
        assert anchor in code
    assert 'Missing original metadata' not in codes[0]
    assert 'papers.jsonl' in codes[5]
    assert 'S2_API_KEY' in codes[2]
