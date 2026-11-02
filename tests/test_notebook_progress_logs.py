"""The Colab notebook reports durable work in text rather than progress widgets."""
import json
from pathlib import Path

NOTEBOOK = Path(__file__).resolve().parents[1] / 'src/milestone1_pipeline.ipynb'


def test_source_references_and_audit_use_text_progress():
    n = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
    codes = [''.join(c['source']) for c in n['cells'] if c['cell_type'] == 'code']
    for index in (7, 8, 9):
        code = codes[index]
        assert 'tqdm' not in code
        assert 'progress_callback=' in code
        assert 'pct' in code
        assert 'arxiv_id' in code
        assert 'status' in code
