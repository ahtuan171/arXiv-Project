"""Tests for src/download_module."""

from dataclasses import asdict
from pathlib import Path
import pytest

from src.download_module.downloader import DownloadStats


def test_download_stats_schema():
    stats = DownloadStats(
        arxiv_id="2504.12523",
        download_status="success",
        has_tex=True,
        raw_size_bytes=10240,
        cleaned_size_bytes=4096,
        download_time_sec=1.234,
    )
    d = stats.to_dict()
    assert d == {
        "arxiv_id": "2504.12523",
        "download_status": "success",
        "has_tex": True,
        "raw_size_bytes": 10240,
        "cleaned_size_bytes": 4096,
        "download_time_sec": 1.234,
    }
    # Verify exact column names matching Table 2 in table_description.md
    expected_cols = [
        "arxiv_id",
        "download_status",
        "has_tex",
        "raw_size_bytes",
        "cleaned_size_bytes",
        "download_time_sec",
    ]
    assert list(d.keys()) == expected_cols


def test_prune_figures(tmp_path: Path):
    from src.download_module.downloader import prune_figures

    # Setup files in tmp_path
    (tmp_path / "main.tex").write_text("\\documentclass{article}\n\\begin{document}\nHello\n\\end{document}")
    (tmp_path / "refs.bib").write_text("@article{key, title={Test}}")
    (tmp_path / "custom.sty").write_text("% style file")
    (tmp_path / "figure1.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"dummy png data" * 20)
    (tmp_path / "chart.pdf").write_bytes(b"%PDF-1.4" + b"dummy pdf figure" * 20)
    (tmp_path / "diagram.EPS").write_bytes(b"%!PS-Adobe" + b"dummy eps" * 10)
    
    sub = tmp_path / "figures"
    sub.mkdir()
    (sub / "plot.jpg").write_bytes(b"\xff\xd8\xff" + b"dummy jpg" * 15)

    num_deleted, bytes_deleted = prune_figures(tmp_path)

    assert num_deleted == 4
    assert bytes_deleted > 0

    # Ensure source files remain
    assert (tmp_path / "main.tex").exists()
    assert (tmp_path / "refs.bib").exists()
    assert (tmp_path / "custom.sty").exists()

    # Ensure image files were removed
    assert not (tmp_path / "figure1.png").exists()
    assert not (tmp_path / "chart.pdf").exists()
    assert not (tmp_path / "diagram.EPS").exists()
    assert not (sub / "plot.jpg").exists()


def test_extract_source_archive_tar_gz(tmp_path: Path):
    import io
    import tarfile
    from src.download_module.downloader import extract_source_archive

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tex_data = b"\\documentclass{article}\n\\begin{document}Content\\end{document}"
        ti = tarfile.TarInfo(name="paper.tex")
        ti.size = len(tex_data)
        tar.addfile(ti, io.BytesIO(tex_data))

        fig_data = b"fake png image"
        ti_fig = tarfile.TarInfo(name="figs/fig1.png")
        ti_fig.size = len(fig_data)
        tar.addfile(ti_fig, io.BytesIO(fig_data))

    archive_bytes = buf.getvalue()
    target_dir = tmp_path / "extracted_tar"
    extracted, has_tex = extract_source_archive(archive_bytes, target_dir)

    assert extracted is True
    assert has_tex is True
    assert (target_dir / "paper.tex").exists()
    assert (target_dir / "figs" / "fig1.png").exists()


def test_extract_source_archive_single_gz(tmp_path: Path):
    import gzip
    from src.download_module.downloader import extract_source_archive

    tex_content = b"\\documentclass{article}\n\\begin{document}Single file\\end{document}"
    gz_bytes = gzip.compress(tex_content)

    target_dir = tmp_path / "extracted_gz"
    extracted, has_tex = extract_source_archive(gz_bytes, target_dir)

    assert extracted is True
    assert has_tex is True
    assert (target_dir / "main.tex").exists()
    assert (target_dir / "main.tex").read_bytes() == tex_content


class MockResponse:
    def __init__(self, content: bytes, status_code: int = 200, headers: dict = None):
        self.content = content
        self.status_code = status_code
        self.headers = headers or {}


def test_download_single_paper_success(tmp_path: Path, monkeypatch):
    import io
    import tarfile
    from src.download_module.downloader import download_single_paper

    # Prepare mock tar.gz
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tex_data = b"\\documentclass{article}\n\\begin{document}Hello\\end{document}"
        ti = tarfile.TarInfo(name="paper.tex")
        ti.size = len(tex_data)
        tar.addfile(ti, io.BytesIO(tex_data))

        fig_data = b"fake image" * 100
        ti_fig = tarfile.TarInfo(name="fig.png")
        ti_fig.size = len(fig_data)
        tar.addfile(ti_fig, io.BytesIO(fig_data))

    tar_bytes = buf.getvalue()

    def mock_get(*args, **kwargs):
        return MockResponse(tar_bytes, 200, {"Content-Type": "application/x-eprint-tar"})

    monkeypatch.setattr("requests.get", mock_get)

    stats = download_single_paper("2504.12523", raw_dir=tmp_path, rate_limiter=None)

    assert stats.arxiv_id == "2504.12523"
    assert stats.download_status == "success"
    assert stats.has_tex is True
    assert stats.raw_size_bytes == len(tar_bytes)
    assert stats.cleaned_size_bytes < stats.raw_size_bytes
    assert stats.download_time_sec >= 0.0

    paper_dir = tmp_path / "2504_12523"
    assert paper_dir.exists()
    assert (paper_dir / "paper.tex").exists()
    assert not (paper_dir / "fig.png").exists()


def test_download_single_paper_pdf_only(tmp_path: Path, monkeypatch):
    from src.download_module.downloader import download_single_paper

    pdf_bytes = b"%PDF-1.4\nfake pdf content"

    def mock_get(*args, **kwargs):
        return MockResponse(pdf_bytes, 200, {"Content-Type": "application/pdf"})

    monkeypatch.setattr("requests.get", mock_get)

    stats = download_single_paper("2504.99999", raw_dir=tmp_path, rate_limiter=None)

    assert stats.arxiv_id == "2504.99999"
    assert stats.download_status == "pdf_only"
    assert stats.has_tex is False
    assert stats.raw_size_bytes == len(pdf_bytes)
    assert stats.cleaned_size_bytes == 0


def test_download_single_paper_not_found(tmp_path: Path, monkeypatch):
    from src.download_module.downloader import download_single_paper

    def mock_get(*args, **kwargs):
        return MockResponse(b"Not Found", 404)

    monkeypatch.setattr("requests.get", mock_get)

    stats = download_single_paper("2504.00000", raw_dir=tmp_path, rate_limiter=None)

    assert stats.arxiv_id == "2504.00000"
    assert stats.download_status == "failed"
    assert stats.has_tex is False
    assert stats.raw_size_bytes == 0
    assert stats.cleaned_size_bytes == 0


def test_parse_arxiv_range():
    from src.download_module.downloader import parse_arxiv_range

    ids = parse_arxiv_range("2504.00001", "2504.00003")
    assert ids == ["2504.00001", "2504.00002", "2504.00003"]

    ids_with_month = parse_arxiv_range(1, 3, month="2504")
    assert ids_with_month == ["2504.00001", "2504.00002", "2504.00003"]

    with pytest.raises(ValueError):
        parse_arxiv_range(1, 3)


def test_download_papers_range(tmp_path: Path, monkeypatch):
    import io
    import tarfile
    from src.download_module.downloader import download_papers_range

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tex_data = b"\\documentclass{article}\n\\begin{document}Hi\\end{document}"
        ti = tarfile.TarInfo(name="main.tex")
        ti.size = len(tex_data)
        tar.addfile(ti, io.BytesIO(tex_data))
    tar_bytes = buf.getvalue()

    def mock_get(self, url, *args, **kwargs):
        if "00002" in url:
            return MockResponse(b"%PDF-1.4\nfake", 200, {"Content-Type": "application/pdf"})
        return MockResponse(tar_bytes, 200, {"Content-Type": "application/x-eprint-tar"})

    monkeypatch.setattr("requests.Session.get", mock_get)
    monkeypatch.setattr("requests.get", lambda url, *a, **kw: mock_get(None, url, *a, **kw))

    raw_dir = tmp_path / "raw"
    csv_dir = tmp_path / "csv"

    df = download_papers_range(
        start_id="2504.00001",
        end_id="2504.00002",
        raw_dir=raw_dir,
        csv_dir=csv_dir,
        delay=0.0,
        resume=True,
    )

    csv_file = csv_dir / "m1_download_stats.csv"
    assert csv_file.exists()
    assert len(df) == 2
    assert list(df.columns) == [
        "arxiv_id",
        "download_status",
        "has_tex",
        "raw_size_bytes",
        "cleaned_size_bytes",
        "download_time_sec",
    ]
    assert df.loc[df["arxiv_id"] == "2504.00001", "download_status"].values[0] == "success"
    assert df.loc[df["arxiv_id"] == "2504.00002", "download_status"].values[0] == "pdf_only"

    # Test resume: run again, it should skip already processed
    df_resumed = download_papers_range(
        start_id="2504.00001",
        end_id="2504.00002",
        raw_dir=raw_dir,
        csv_dir=csv_dir,
        delay=0.0,
        resume=True,
    )
    assert len(df_resumed) == 2



