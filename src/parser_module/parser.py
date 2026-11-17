"""Main LaTeX to hierarchy tree parsing interface.

Provides functions to parse a LaTeX source file, string, or entire version directory
into a structured LaTeXHierarchyTree, as well as parse_tex_versions() for all versions.
"""
from __future__ import annotations

import csv
import logging
import re
import time
from pathlib import Path
from typing import Any

from src.parser_module.cleaner import clean_latex_markup, normalize_math, strip_latex_comments
from src.parser_module.hierarchy_builder import (
    HierarchyNode,
    LaTeXHierarchyTree,
    parse_latex_to_hierarchy,
)
from src.parser_module.multi_file import find_root_tex_file, merge_tex_sources

log = logging.getLogger(__name__)

TREE_STATS_COLUMNS = [
    "arxiv_id",
    "parse_status",
    "total_tex_versions",
    "parsed_versions",
    "version_coverage",
    "raw_elements_count",
    "max_tree_depth",
    "parse_time_sec",
]


def parse_tex_string(
    tex_source: str,
    arxiv_id: str = "",
    version: int = 1,
) -> LaTeXHierarchyTree:
    """Parse raw LaTeX source text into a LaTeXHierarchyTree."""
    return parse_latex_to_hierarchy(tex_source, arxiv_id=arxiv_id, version=version)


def parse_tex_file(
    tex_file: Path,
    arxiv_id: str = "",
    version: int = 1,
) -> LaTeXHierarchyTree:
    """Parse a single .tex file, resolving transitive \\input and \\include statements."""
    if not tex_file.exists():
        raise FileNotFoundError(f"LaTeX file not found: {tex_file}")

    merged_source = merge_tex_sources(tex_file)
    return parse_latex_to_hierarchy(merged_source, arxiv_id=arxiv_id, version=version)


def parse_tex_directory(
    source_dir: Path,
    arxiv_id: str = "",
    version: int = 1,
) -> LaTeXHierarchyTree | None:
    """Identify the compilation root in source_dir, gather all included files, and build the tree.
    
    If source_dir does not exist or has no root .tex file, returns None.
    """
    if not source_dir.exists() or not source_dir.is_dir():
        log.warning("Source directory does not exist or is not a directory: %s", source_dir)
        return None

    root_tex = find_root_tex_file(source_dir)
    if not root_tex:
        log.warning("No root .tex file (with \\documentclass) found in: %s", source_dir)
        return None

    return parse_tex_file(root_tex, arxiv_id=arxiv_id, version=version)


def _resolve_paper_path(paper_input: str | Path, raw_dir: str | Path | None = None) -> tuple[str, Path | None]:
    """Resolve arxiv_id and the paper directory from diverse input formats."""
    # Case 1: paper_input is already an existing directory Path
    p = Path(paper_input)
    if p.is_dir():
        arxiv_id = p.name.replace("_", ".")
        return arxiv_id, p

    # Case 2: paper_input is an arXiv ID string (e.g. "2501.07642" or "2501_07642")
    arxiv_id = str(paper_input).strip().replace("_", ".")
    raw_dirs = []
    if raw_dir is not None:
        raw_dirs.append(Path(raw_dir))
    else:
        # Default fallback candidates
        raw_dirs.extend([
            Path("/content/local_raw"),
            Path("data/raw"),
            Path("data/raw/sources"),
        ])

    for r in raw_dirs:
        for candidate_name in [arxiv_id, arxiv_id.replace(".", "_")]:
            target = r / candidate_name
            if target.is_dir():
                return arxiv_id, target

    return arxiv_id, None


def _find_version_directories(paper_dir: Path) -> dict[int, Path]:
    """Find version subdirectories (v1, v2, ...) or treat root as v1."""
    versions: dict[int, Path] = {}
    v_pattern = re.compile(r"^v(\d+)$", re.IGNORECASE)

    for sub in paper_dir.iterdir():
        if sub.is_dir():
            m = v_pattern.match(sub.name)
            if m:
                versions[int(m.group(1))] = sub

    if not versions:
        # Check if .tex files exist directly in paper_dir
        if list(paper_dir.glob("*.tex")):
            versions[1] = paper_dir

    return dict(sorted(versions.items()))


def parse_tex_versions(
    paper_input: str | Path,
    raw_dir: str | Path | None = None,
) -> tuple[dict[int, LaTeXHierarchyTree], dict[str, Any]]:
    """Parse all versions of a paper into raw AST hierarchy trees in RAM.
    
    Args:
        paper_input: arXiv ID string (e.g. "2501.07642") or Path to paper directory.
        raw_dir: Optional root raw directory (e.g. "/content/local_raw" or "data/raw").
        
    Returns:
        tuple (trees, stats_record):
          - trees: dict[int, LaTeXHierarchyTree] mapping version number to in-memory tree.
          - stats_record: dict adhering to Table 1 (m2_tree_stats.csv) schema:
              * arxiv_id: string
              * parse_status: "success" | "syntax_error" | "empty"
              * total_tex_versions: int
              * parsed_versions: int
              * version_coverage: float
              * raw_elements_count: int
              * max_tree_depth: int
              * parse_time_sec: float
    """
    t0 = time.perf_counter()
    arxiv_id, paper_dir = _resolve_paper_path(paper_input, raw_dir)

    if paper_dir is None or not paper_dir.exists():
        parse_time = round(time.perf_counter() - t0, 4)
        stats = {
            "arxiv_id": arxiv_id,
            "parse_status": "empty",
            "total_tex_versions": 0,
            "parsed_versions": 0,
            "version_coverage": 0.0,
            "raw_elements_count": 0,
            "max_tree_depth": 0,
            "parse_time_sec": parse_time,
        }
        return {}, stats

    version_dirs = _find_version_directories(paper_dir)
    total_tex_versions = 0
    parsed_versions = 0
    trees: dict[int, LaTeXHierarchyTree] = {}

    for v, vdir in version_dirs.items():
        tex_files = list(vdir.glob("**/*.tex"))
        if not tex_files:
            continue

        total_tex_versions += 1
        try:
            root_tex = find_root_tex_file(vdir)
            if not root_tex:
                log.warning("No root .tex found for %s v%d in %s", arxiv_id, v, vdir)
                continue

            merged_source = merge_tex_sources(root_tex)
            if not merged_source.strip():
                continue

            tree = parse_latex_to_hierarchy(merged_source, arxiv_id=arxiv_id, version=v)
            if tree and tree.root and tree.root.children:
                trees[v] = tree
                parsed_versions += 1
        except Exception as exc:
            log.warning("Error parsing %s v%d: %s", arxiv_id, v, exc)

    if parsed_versions > 0:
        parse_status = "success"
    elif total_tex_versions > 0:
        parse_status = "syntax_error"
    else:
        parse_status = "empty"

    version_coverage = (
        round(parsed_versions / total_tex_versions, 4) if total_tex_versions > 0 else 0.0
    )
    raw_elements_count = sum(t.raw_elements_count() for t in trees.values())
    max_tree_depth = max((t.get_max_depth() for t in trees.values()), default=0)
    parse_time_sec = round(time.perf_counter() - t0, 4)

    stats = {
        "arxiv_id": arxiv_id,
        "parse_status": parse_status,
        "total_tex_versions": total_tex_versions,
        "parsed_versions": parsed_versions,
        "version_coverage": version_coverage,
        "raw_elements_count": raw_elements_count,
        "max_tree_depth": max_tree_depth,
        "parse_time_sec": parse_time_sec,
    }

    return trees, stats


def export_tree_stats(
    stats_records: list[dict[str, Any]],
    output_path: str | Path,
) -> Path:
    """Export a list of tree stats records to Bảng 1: m2_tree_stats.csv."""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=TREE_STATS_COLUMNS)
        writer.writeheader()
        for r in stats_records:
            filtered = {col: r.get(col, "") for col in TREE_STATS_COLUMNS}
            writer.writerow(filtered)

    return path


def append_tree_stats(
    record: dict[str, Any],
    output_path: str | Path,
) -> None:
    """Append a single paper's stats to m2_tree_stats.csv (writes header if missing)."""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    file_exists = path.exists() and path.stat().st_size > 0

    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=TREE_STATS_COLUMNS)
        if not file_exists:
            writer.writeheader()
        filtered = {col: record.get(col, "") for col in TREE_STATS_COLUMNS}
        writer.writerow(filtered)
