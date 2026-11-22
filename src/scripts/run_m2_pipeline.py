"""Milestone 2 In-Memory Execution Pipeline with Multiprocessing, Batch Flushing, and Benchmarking.

Orchestrates:
  1. LaTeX AST parsing (src.parser_module.parse_tex_versions)
  2. BibTeX reference extraction & normalization (src.parser_module.bib_parser)
  3. In-memory deduplication across versions (src.deduplicator.deduplicate_paper)
  4. Batch flushing to Parquet files & CSV stats
  5. System resource tracking (src.benchmark.SystemBenchmarkTracker)
"""
from __future__ import annotations

import csv
import gc
import logging
import os
import re
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Sequence

import pandas as pd

# Ensure project root is available
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src import benchmark, utils
from src.deduplicator import (
    BIBTEX_COLUMNS,
    ELEMENT_COLUMNS,
    HIERARCHY_COLUMNS,
    STAT_COLUMNS as DEDUP_STAT_COLUMNS,
    deduplicate_paper,
)
from src.parser_module import TREE_STATS_COLUMNS, parse_tex_versions
from src.parser_module.bib_parser import (
    BIB_STATS_COLUMNS,
    process_publication_references,
)

log = logging.getLogger(__name__)

BENCHMARK_COLUMNS = [
    "total_papers",
    "total_wall_time_sec",
    "avg_time_per_paper_sec",
    "peak_ram_mb",
    "average_ram_mb",
    "peak_disk_mb",
    "final_storage_mb",
]


def process_single_paper(paper_path_str: str, raw_dir_str: str = "") -> dict[str, Any]:
    """Worker function: process one paper completely in RAM.
    
    Safe for top-level multiprocessing execution.
    """
    paper_path = Path(paper_path_str)
    raw_dir = Path(raw_dir_str) if raw_dir_str else paper_path.parent
    arxiv_id = paper_path.name.replace("_", ".")

    # 1. LaTeX AST Parsing
    trees, tree_stats = parse_tex_versions(paper_path, raw_dir=raw_dir)
    raw_elements: list[dict[str, Any]] = []
    raw_hierarchy: list[dict[str, Any]] = []

    for v, tree in trees.items():
        for elem in tree.to_elements_table():
            elem["version"] = f"v{v}"
            raw_elements.append(elem)
        raw_hierarchy.extend(tree.to_hierarchy_table())

    # 2. BibTeX Reference Extraction
    bib_df, rename_map, bib_stats = process_publication_references(
        paper_path, arxiv_id=arxiv_id, return_stats=True
    )

    # 3. In-Memory Deduplication
    elem_df, hier_df, bib_df_clean, dedup_stats = deduplicate_paper(
        arxiv_id=arxiv_id,
        raw_elements=raw_elements,
        raw_hierarchy=raw_hierarchy,
        raw_bibtex=bib_df,
    )

    return {
        "arxiv_id": arxiv_id,
        "elements_df": elem_df,
        "hierarchy_df": hier_df,
        "bibtex_df": bib_df_clean,
        "tree_stats": tree_stats,
        "bib_stats": bib_stats.to_dict(),
        "dedup_stats": dedup_stats,
    }


def _append_or_write_csv(rows: list[dict[str, Any]], csv_path: Path, columns: list[str]) -> None:
    """Append a batch of dictionary rows to a CSV file."""
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    file_exists = csv_path.exists() and csv_path.stat().st_size > 0
    with csv_path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        if not file_exists:
            writer.writeheader()
        for r in rows:
            filtered = {c: r.get(c, "") for c in columns}
            writer.writerow(filtered)


def find_paper_folders(raw_path: Path) -> list[Path]:
    """Find all paper folders under raw_path, auto-resolving nested subdirectories.
    
    Handles cases like:
    - raw_path / <arxiv_id> / v1 / ...
    - raw_path / raw / <arxiv_id> / v1 / ...
    - raw_path / data / raw / <arxiv_id> / v1 / ...
    """
    if not raw_path.exists():
        return []

    ignored = {"__MACOSX", ".ipynb_checkpoints", ".git"}
    subdirs = sorted([d for d in raw_path.iterdir() if d.is_dir() and not d.name.startswith(".") and d.name not in ignored])

    # If raw_path contains only a single container folder like 'raw' or 'data', unwrap it
    if len(subdirs) == 1 and subdirs[0].name.lower() in ("raw", "data"):
        return find_paper_folders(subdirs[0])

    # If raw_path has a 'raw' folder containing subdirectories
    if (raw_path / "raw").is_dir():
        raw_sub = raw_path / "raw"
        nested_papers = [d for d in raw_sub.iterdir() if d.is_dir() and not d.name.startswith(".") and d.name not in ignored]
        if nested_papers:
            return find_paper_folders(raw_sub)

    # Check if subdirs look like paper folders (contain version dirs like v1, .tex files, or arXiv ID naming)
    paper_folders = []
    for d in subdirs:
        if d.name.lower() in ("raw", "data"):
            continue
        has_versions = any(v.is_dir() and v.name.startswith("v") for v in d.iterdir())
        has_tex = any(f.suffix == ".tex" for f in d.iterdir())
        has_id_pattern = (
            bool(re.search(r"\d{4}[._]\d{4,5}", d.name))
            or bool(re.match(r"^[a-zA-Z\-]+(\.[a-zA-Z]{2})?\d{7}$", d.name))
            or any(char.isdigit() for char in d.name)
        )
        if has_versions or has_tex or has_id_pattern:
            paper_folders.append(d)

    return paper_folders or subdirs


def run_m2_pipeline(
    raw_dir: Path | str,
    processed_dir: Path | str,
    member_name: str = "",
    paper_ids: Sequence[str] | None = None,
    batch_size: int = 50,
    max_workers: int | None = None,
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> dict[str, Any]:
    """Execute the full Milestone 2 pipeline with batch flushing and resource tracking.
    
    Args:
        raw_dir: Directory containing extracted raw LaTeX sources (e.g. /content/local_raw).
        processed_dir: Destination for final Parquet and CSV files (e.g. /content/local_processed).
        member_name: Optional team member suffix for output CSV files.
        paper_ids: Optional list of specific arXiv IDs to process.
        batch_size: Number of papers processed before flushing Parquet & CSV to disk and clearing RAM.
        max_workers: CPU worker count for ProcessPoolExecutor.
        progress_callback: Optional progress callback.
        
    Returns:
        Summary dict containing totals, benchmark results, and output file paths.
    """
    raw_path = Path(raw_dir)
    proc_path = Path(processed_dir)
    proc_path.mkdir(parents=True, exist_ok=True)

    # 1. Discover paper folders with nested container auto-resolution
    if not raw_path.exists():
        raise FileNotFoundError(f"Raw sources directory does not exist: {raw_path}")

    all_folders = find_paper_folders(raw_path)
    if paper_ids:
        filter_set = {str(pid).replace(".", "_") for pid in paper_ids} | {str(pid).replace("_", ".") for pid in paper_ids}
        target_folders = [d for d in all_folders if d.name in filter_set or d.name.replace("_", ".") in filter_set]
    else:
        target_folders = all_folders

    total_papers = len(target_folders)
    if total_papers == 0:
        log.warning("No papers found to process in %s", raw_path)
        return {"total_papers": 0, "status": "empty"}

    log.info("Starting Milestone 2 pipeline for %d papers with batch_size=%d", total_papers, batch_size)

    # File paths for 4 Tables
    suffix = f"_{member_name.strip()}" if member_name.strip() else ""
    tree_csv = proc_path / f"m2_tree_stats{suffix}.csv"
    bib_csv = proc_path / f"m2_bib_stats{suffix}.csv"
    dedup_csv = proc_path / f"m2_dedup_stats{suffix}.csv"
    bench_csv = proc_path / f"m2_system_benchmark{suffix}.csv"

    # Clean existing CSVs if fresh run
    for p in [tree_csv, bib_csv, dedup_csv, bench_csv]:
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass

    # 2. Start System Benchmark Tracker
    # Spec requirement: Only point tracker to processed_dir to avoid double counting raw data.
    tracker = benchmark.SystemBenchmarkTracker(
        storage_dirs=[proc_path],
        sample_interval_sec=0.5,
    )
    tracker.start()

    default_workers = 2 if sys.platform.startswith("linux") else 4
    workers = max(1, min(os.cpu_count() or 1, max_workers or default_workers))
    log.info("Running on %d CPU worker processes...", workers)

    batch_idx = 0
    completed_count = 0
    parquet_parts: list[tuple[Path, Path, Path]] = []
    PAPER_TIMEOUT_SEC = 45.0

    # Batch accumulator
    batch_elements: list[pd.DataFrame] = []
    batch_hierarchy: list[pd.DataFrame] = []
    batch_bibtex: list[pd.DataFrame] = []
    batch_tree_stats: list[dict[str, Any]] = []
    batch_bib_stats: list[dict[str, Any]] = []
    batch_dedup_stats: list[dict[str, Any]] = []

    def _flush_batch():
        nonlocal batch_idx
        if not batch_tree_stats:
            return
        batch_idx += 1
        t_flush_start = time.perf_counter()

        # 1. Flush Parquet chunk (filter empty frames to prevent pandas FutureWarning)
        valid_elems = [df for df in batch_elements if not df.empty]
        valid_hiers = [df for df in batch_hierarchy if not df.empty]
        valid_bibs = [df for df in batch_bibtex if not df.empty]

        chunk_elem_df = pd.concat(valid_elems, ignore_index=True) if valid_elems else pd.DataFrame(columns=ELEMENT_COLUMNS)
        chunk_hier_df = pd.concat(valid_hiers, ignore_index=True) if valid_hiers else pd.DataFrame(columns=HIERARCHY_COLUMNS)
        chunk_bib_df = pd.concat(valid_bibs, ignore_index=True) if valid_bibs else pd.DataFrame(columns=BIBTEX_COLUMNS)

        elem_part = proc_path / f"elements_part_{batch_idx:04d}.parquet"
        hier_part = proc_path / f"hierarchy_part_{batch_idx:04d}.parquet"
        bib_part = proc_path / f"bibtex_part_{batch_idx:04d}.parquet"

        chunk_elem_df.to_parquet(elem_part, index=False, engine="pyarrow", compression="snappy")
        chunk_hier_df.to_parquet(hier_part, index=False, engine="pyarrow", compression="snappy")
        chunk_bib_df.to_parquet(bib_part, index=False, engine="pyarrow", compression="snappy")

        parquet_parts.append((elem_part, hier_part, bib_part))

        # 2. Append to 3 stats CSVs
        _append_or_write_csv(batch_tree_stats, tree_csv, TREE_STATS_COLUMNS)
        _append_or_write_csv(batch_bib_stats, bib_csv, BIB_STATS_COLUMNS)
        _append_or_write_csv(batch_dedup_stats, dedup_csv, DEDUP_STAT_COLUMNS)

        log.info(
            "Batch %d flushed (%d papers, %d elements, %d hier, %d bibtex) in %.2fs. Freeing RAM...",
            batch_idx,
            len(batch_tree_stats),
            len(chunk_elem_df),
            len(chunk_hier_df),
            len(chunk_bib_df),
            time.perf_counter() - t_flush_start,
        )

        # Clear batch and trigger garbage collection
        batch_elements.clear()
        batch_hierarchy.clear()
        batch_bibtex.clear()
        batch_tree_stats.clear()
        batch_bib_stats.clear()
        batch_dedup_stats.clear()
        gc.collect()

    try:
        if workers == 1:
            for fpath in target_folders:
                res = process_single_paper(str(fpath), str(fpath.parent))
                batch_elements.append(res["elements_df"])
                batch_hierarchy.append(res["hierarchy_df"])
                batch_bibtex.append(res["bibtex_df"])
                batch_tree_stats.append(res["tree_stats"])
                batch_bib_stats.append(res["bib_stats"])
                batch_dedup_stats.append(res["dedup_stats"])
                completed_count += 1
                if progress_callback:
                    progress_callback(completed_count, total_papers, res["arxiv_id"])
                if len(batch_tree_stats) >= batch_size:
                    _flush_batch()
        else:
            with ProcessPoolExecutor(max_workers=workers) as executor:
                future_to_id = {
                    executor.submit(process_single_paper, str(fpath), str(fpath.parent)): fpath.name
                    for fpath in target_folders
                }
                for future in as_completed(future_to_id):
                    paper_name = future_to_id[future]
                    arxiv_id = paper_name.replace("_", ".")
                    try:
                        res = future.result(timeout=PAPER_TIMEOUT_SEC)
                    except TimeoutError:
                        log.warning("Paper %s TIMED OUT (>%.1fs). Skipping to prevent pipeline hang...", arxiv_id, PAPER_TIMEOUT_SEC)
                        res = {
                            "arxiv_id": arxiv_id,
                            "elements_df": pd.DataFrame(columns=ELEMENT_COLUMNS),
                            "hierarchy_df": pd.DataFrame(columns=HIERARCHY_COLUMNS),
                            "bibtex_df": pd.DataFrame(columns=BIBTEX_COLUMNS),
                            "tree_stats": {
                                "arxiv_id": arxiv_id, "parse_status": "timeout",
                                "total_tex_versions": 0, "parsed_versions": 0, "version_coverage": 0.0,
                                "raw_elements_count": 0, "max_tree_depth": 0, "parse_time_sec": PAPER_TIMEOUT_SEC,
                            },
                            "bib_stats": {
                                "arxiv_id": arxiv_id, "bib_parse_status": "timeout", "bib_source_type": "none",
                                "raw_bib_count": 0, "missing_fields_count": 0, "bib_parse_time_sec": PAPER_TIMEOUT_SEC,
                            },
                            "dedup_stats": {
                                "arxiv_id": arxiv_id, "dedup_status": "timeout", "dedup_elements_count": 0,
                                "element_dedup_ratio": 0.0, "dedup_bib_count": 0, "output_size_bytes": 0,
                                "dedup_time_sec": PAPER_TIMEOUT_SEC,
                            },
                        }
                    except Exception as exc:
                        log.error("Fatal error processing %s: %s", paper_name, exc)
                        res = {
                            "arxiv_id": arxiv_id,
                            "elements_df": pd.DataFrame(columns=ELEMENT_COLUMNS),
                            "hierarchy_df": pd.DataFrame(columns=HIERARCHY_COLUMNS),
                            "bibtex_df": pd.DataFrame(columns=BIBTEX_COLUMNS),
                            "tree_stats": {
                                "arxiv_id": arxiv_id, "parse_status": "syntax_error",
                                "total_tex_versions": 0, "parsed_versions": 0, "version_coverage": 0.0,
                                "raw_elements_count": 0, "max_tree_depth": 0, "parse_time_sec": 0.0,
                            },
                            "bib_stats": {
                                "arxiv_id": arxiv_id, "bib_parse_status": "failed", "bib_source_type": "none",
                                "raw_bib_count": 0, "missing_fields_count": 0, "bib_parse_time_sec": 0.0,
                            },
                            "dedup_stats": {
                                "arxiv_id": arxiv_id, "dedup_status": "failed", "dedup_elements_count": 0,
                                "element_dedup_ratio": 0.0, "dedup_bib_count": 0, "output_size_bytes": 0,
                                "dedup_time_sec": 0.0,
                            },
                        }

                    batch_elements.append(res["elements_df"])
                    batch_hierarchy.append(res["hierarchy_df"])
                    batch_bibtex.append(res["bibtex_df"])
                    batch_tree_stats.append(res["tree_stats"])
                    batch_bib_stats.append(res["bib_stats"])
                    batch_dedup_stats.append(res["dedup_stats"])
                    completed_count += 1

                    if progress_callback:
                        progress_callback(completed_count, total_papers, res["arxiv_id"])

                    if len(batch_tree_stats) >= batch_size:
                        _flush_batch()

        # Final batch flush
        _flush_batch()

    finally:
        # 3. Consolidate Parquet parts into master tables
        final_elements_p = proc_path / "elements.parquet"
        final_hierarchy_p = proc_path / "hierarchy.parquet"
        final_bibtex_p = proc_path / "bibtex.parquet"

        if parquet_parts:
            log.info("Consolidating %d batch Parquet files into master tables...", len(parquet_parts))
            elem_dfs = [pd.read_parquet(p[0]) for p in parquet_parts if p[0].exists() and p[0].stat().st_size > 0]
            hier_dfs = [pd.read_parquet(p[1]) for p in parquet_parts if p[1].exists() and p[1].stat().st_size > 0]
            bib_dfs = [pd.read_parquet(p[2]) for p in parquet_parts if p[2].exists() and p[2].stat().st_size > 0]

            valid_elem = [df for df in elem_dfs if not df.empty]
            valid_hier = [df for df in hier_dfs if not df.empty]
            valid_bib = [df for df in bib_dfs if not df.empty]

            (pd.concat(valid_elem, ignore_index=True) if valid_elem else pd.DataFrame(columns=ELEMENT_COLUMNS)).to_parquet(
                final_elements_p, index=False, engine="pyarrow", compression="snappy"
            )
            (pd.concat(valid_hier, ignore_index=True) if valid_hier else pd.DataFrame(columns=HIERARCHY_COLUMNS)).to_parquet(
                final_hierarchy_p, index=False, engine="pyarrow", compression="snappy"
            )
            (pd.concat(valid_bib, ignore_index=True) if valid_bib else pd.DataFrame(columns=BIBTEX_COLUMNS)).to_parquet(
                final_bibtex_p, index=False, engine="pyarrow", compression="snappy"
            )

            log.info("Master Parquet tables created at %s", proc_path)

        # 4. Calculate exact storage of processed directory & Stop Benchmark Tracker
        final_storage_bytes = sum(f.stat().st_size for f in proc_path.glob("*.*") if f.is_file())
        final_storage_mb = round(final_storage_bytes / (1024 * 1024), 4)

        bench_df = tracker.stop(total_papers=completed_count, final_storage_mb=final_storage_mb)
        # Add avg_time_per_paper_sec
        wall_time = float(bench_df["total_wall_time_sec"].iloc[0])
        avg_time = round(wall_time / max(1, completed_count), 4)
        bench_df["avg_time_per_paper_sec"] = avg_time

        # Reorder columns to match Milestone 2 Table 4 exactly
        bench_df = bench_df[BENCHMARK_COLUMNS]
        bench_df.to_csv(bench_csv, index=False)
        log.info("Saved Table 4 benchmark to %s (storage=%.2f MB)", bench_csv, final_storage_mb)

    return {
        "total_papers": completed_count,
        "tree_stats_csv": str(tree_csv),
        "bib_stats_csv": str(bib_csv),
        "dedup_stats_csv": str(dedup_csv),
        "benchmark_csv": str(bench_csv),
        "elements_parquet": str(final_elements_p),
        "hierarchy_parquet": str(final_hierarchy_p),
        "bibtex_parquet": str(final_bibtex_p),
        "benchmark_df": bench_df,
    }
