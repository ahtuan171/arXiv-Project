"""End-to-end Milestone 3 Reference Matching Pipeline runner with full benchmarking.

Implements all Milestone 3 requirements:
  - Data Cleaning & Manual/Auto Labelling
  - Pairwise Feature Engineering (including Milestone 2 hierarchy citation context)
  - 4-Model Benchmarking (TF-IDF Cosine, Logistic Regression, Random Forest, LightGBM)
  - System Resource Benchmarking (SystemBenchmarkTracker -> m3_system_benchmark.csv)
  - Exporting predictions.parquet & model artifacts

Usage:
    python src/scripts/run_m3_pipeline.py
    python src/scripts/run_m3_pipeline.py --parquet-dir /content/local_parquet --out-dir /content/local_out
"""
from __future__ import annotations

import argparse
import json
import pickle
import sys
import time
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.benchmark import SystemBenchmarkTracker
from src.matching_module.exporter import export_predictions_parquet, prepare_hf_dataset_dir
from src.matching_module.features import build_candidate_pairs, extract_hierarchy_citation_features
from src.matching_module.labelling import (
    auto_label_publications,
    build_manual_seed_labels,
    load_manual_labels,
    validate_labelling_requirements,
)
from src.matching_module.model import split_publications, train_and_rank


def run_m3_pipeline(
    parquet_dir: Path = Path("data/parquet"),
    csv_dir: Path = Path("data/csv"),
    out_parquet_dir: Path | None = None,
    manual_labels_path: Path | None = None,
    auto_fraction: float = 0.12,
    seed: int = 42,
    hf_dir: Path | None = None,
) -> dict[str, Any]:
    """Execute full Milestone 3 pipeline with system & model benchmarking."""
    out_pq_dir = out_parquet_dir or parquet_dir
    out_pq_dir.mkdir(parents=True, exist_ok=True)
    csv_dir.mkdir(parents=True, exist_ok=True)

    # Track RAM, disk, and wall time on M3 output directories only
    tracker = SystemBenchmarkTracker(
        storage_dirs=[out_pq_dir, csv_dir],
        sample_interval_sec=0.5,
    )
    tracker.start()
    t0 = time.perf_counter()

    bib_path = parquet_dir / "bibtex.parquet"
    ref_path = parquet_dir / "references.parquet"
    elem_path = parquet_dir / "elements.parquet"
    hier_path = parquet_dir / "hierarchy.parquet"

    if not bib_path.is_file() or not ref_path.is_file():
        tracker.stop(total_papers=0)
        raise FileNotFoundError(f"Missing required Parquet files in {parquet_dir}")

    print("[M3] Loading bibtex.parquet and references.parquet...", flush=True)
    bibtex_df = pd.read_parquet(bib_path)
    references_df = pd.read_parquet(ref_path)

    # 1. Manual & Automatic Labelling (Candidate Generation / Ground Truth setup)
    t_cand0 = time.perf_counter()
    manual_csv = manual_labels_path or (csv_dir / "m3_manual_labels.csv")
    if manual_csv.is_file():
        print(f"[M3] Loading manual labels from {manual_csv}...", flush=True)
        manual_df = load_manual_labels(manual_csv)
    else:
        print("[M3] Generating seed manual ground-truth pool (>=5 papers, >=20 entries each)...", flush=True)
        manual_df = build_manual_seed_labels(bibtex_df, references_df, seed=seed)
        manual_df.to_csv(manual_csv, index=False)

    manual_papers = set(manual_df["arxiv_id"].astype(str))
    bib_papers = set(bibtex_df["arxiv_id"].astype(str))
    ref_papers = set(references_df["arxiv_id"].astype(str))
    total_non_manual_eligible = len((bib_papers & ref_papers) - manual_papers)

    print(f"[M3] Auto-labelling {auto_fraction:.0%} of {total_non_manual_eligible} non-manual eligible papers...", flush=True)
    auto_df = auto_label_publications(
        bibtex_df,
        references_df,
        exclude_papers=manual_papers,
        fraction=auto_fraction,
        seed=seed,
    )

    labels_df = pd.concat([manual_df, auto_df], ignore_index=True)
    label_stats = validate_labelling_requirements(labels_df, total_non_manual_eligible)
    print(f"[M3] Labelling verified: {label_stats}", flush=True)

    # 2. Train / Valid / Test Split
    partition_map = split_publications(labels_df, seed=seed)
    target_papers = set(partition_map.keys())
    candidate_gen_time_sec = round(time.perf_counter() - t_cand0, 4)

    # 3. Feature Engineering (M2 Hierarchy Context + Pairwise Text/Author/Year/ID Features)
    t_feat0 = time.perf_counter()
    hierarchy_feats = {}
    if elem_path.is_file():
        print("[M3] Extracting Milestone 2 hierarchy citation context features...", flush=True)
        elements_df = pd.read_parquet(
            elem_path,
            columns=["element_id", "arxiv_id", "kind", "content"],
            filters=[("arxiv_id", "in", list(target_papers))],
        )
        hierarchy_df = (
            pd.read_parquet(
                hier_path,
                columns=["arxiv_id", "element_id", "parent_id"],
                filters=[("arxiv_id", "in", list(target_papers))],
            )
            if hier_path.is_file()
            else None
        )
        hierarchy_feats = extract_hierarchy_citation_features(elements_df, hierarchy_df, target_papers)
        print(f"[M3] Extracted hierarchy features for {len(hierarchy_feats):,} (paper, citation_key) pairs.", flush=True)

    print("[M3] Building m x n candidate pairs and computing 16 features...", flush=True)
    pairs_df = build_candidate_pairs(
        bibtex_df,
        references_df,
        labels_df=labels_df,
        hierarchy_feats=hierarchy_feats,
    )
    feature_eng_time_sec = round(time.perf_counter() - t_feat0, 4)
    print(f"[M3] Built {len(pairs_df):,} candidate pairs in {feature_eng_time_sec:.2f}s.", flush=True)

    # 4. Train & Benchmark 4 Models (TF-IDF Cosine, Logistic Regression, Random Forest, LightGBM)
    print("[M3] Training & evaluating 4 models...", flush=True)
    predictions_df, model_summary, feat_imp_df, model_bench_df, best_model = train_and_rank(
        pairs_df, partition_map, seed=seed
    )

    # 5. Export predictions.parquet, model weights, and CSV reports
    pred_path = export_predictions_parquet(predictions_df, out_pq_dir / "predictions.parquet")
    model_weights_path = csv_dir / "m3_best_model.pkl"
    with model_weights_path.open("wb") as fh:
        pickle.dump(best_model, fh)

    feat_imp_path = csv_dir / "m3_feature_importance.csv"
    feat_imp_df.to_csv(feat_imp_path, index=False)

    model_bench_path = csv_dir / "m3_model_benchmark.csv"
    model_bench_df.to_csv(model_bench_path, index=False)

    label_stats_path = csv_dir / "m3_labelling_stats.csv"
    pd.DataFrame([label_stats]).to_csv(label_stats_path, index=False)

    # 6. Stop SystemBenchmarkTracker & save m3_system_benchmark.csv
    final_storage_bytes = pred_path.stat().st_size + model_weights_path.stat().st_size
    final_storage_mb = round(final_storage_bytes / (1024 * 1024), 4)
    sys_bench_df = tracker.stop(total_papers=len(target_papers), final_storage_mb=final_storage_mb)

    sys_bench_df["candidate_gen_time_sec"] = candidate_gen_time_sec
    sys_bench_df["feature_eng_time_sec"] = feature_eng_time_sec
    sys_bench_df["train_time_sec"] = model_summary["total_train_time_sec"]
    sys_bench_df["inference_time_sec"] = model_summary["best_inference_time_sec"]

    sys_cols = [
        "total_papers",
        "candidate_gen_time_sec",
        "feature_eng_time_sec",
        "train_time_sec",
        "inference_time_sec",
        "total_wall_time_sec",
        "peak_ram_mb",
        "average_ram_mb",
        "peak_disk_mb",
        "final_storage_mb",
    ]
    sys_bench_df = sys_bench_df[sys_cols]
    sys_bench_path = csv_dir / "m3_system_benchmark.csv"
    sys_bench_df.to_csv(sys_bench_path, index=False)

    eval_summary = {
        "elapsed_sec": round(time.perf_counter() - t0, 2),
        "labelling": label_stats,
        "split_counts": {
            part: sum(1 for v in partition_map.values() if v == part)
            for part in ("train", "valid", "test")
        },
        "model": model_summary,
        "system_benchmark": sys_bench_df.iloc[0].to_dict(),
        "predictions_parquet": str(pred_path),
        "model_weights": str(model_weights_path),
        "model_benchmark_csv": str(model_bench_path),
        "system_benchmark_csv": str(sys_bench_path),
    }
    eval_path = csv_dir / "m3_evaluation_summary.json"
    eval_path.write_text(json.dumps(eval_summary, indent=2), encoding="utf-8")

    if hf_dir is not None:
        prepare_hf_dataset_dir(parquet_dir, hf_dir)
        eval_summary["hf_dataset_dir"] = str(hf_dir)

    print(f"[M3] Done in {eval_summary['elapsed_sec']}s! Selected model: {model_summary['selected_model']}")
    print(f"[M3] Test metrics: {model_summary['final_metrics']['test']}")
    return eval_summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Milestone 3 Reference Matching Pipeline.")
    parser.add_argument("--parquet-dir", type=Path, default=Path("data/parquet"))
    parser.add_argument("--out-parquet-dir", type=Path, default=None)
    parser.add_argument("--csv-dir", type=Path, default=Path("data/csv"))
    parser.add_argument("--manual-labels", type=Path, default=None)
    parser.add_argument("--auto-fraction", type=float, default=0.12)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--hf-dir", type=Path, default=None, help="Optional output directory for 6-table Hugging Face dataset")
    args = parser.parse_args()

    run_m3_pipeline(
        parquet_dir=args.parquet_dir,
        csv_dir=args.csv_dir,
        out_parquet_dir=args.out_parquet_dir,
        manual_labels_path=args.manual_labels,
        auto_fraction=args.auto_fraction,
        seed=args.seed,
        hf_dir=args.hf_dir,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
