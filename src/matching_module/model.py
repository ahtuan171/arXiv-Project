"""Splitting, training (4 models), top-5 ranking, and MRR@5 benchmarking for Milestone 3."""
from __future__ import annotations

import time
from typing import Any

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier
from sklearn.inspection import permutation_importance
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from src.benchmark import current_process_ram_mb
from src.matching_module.features import FEATURE_COLUMNS


def split_publications(labels_df: pd.DataFrame, *, seed: int = 42) -> dict[str, str]:
    """Split labeled publications into train, valid, and test per Milestone 3 spec.

    - test : 1 manual publication + 1 auto publication
    - valid: 1 manual publication + 1 auto publication
    - train: all remaining labeled publications
    """
    manual_papers = sorted(labels_df[labels_df["pool"] == "manual"]["arxiv_id"].astype(str).unique())
    auto_papers = sorted(labels_df[labels_df["pool"] == "auto"]["arxiv_id"].astype(str).unique())

    if len(manual_papers) < 3:
        raise ValueError(f"Need >=3 manual papers for train/valid/test split, got {len(manual_papers)}")
    if len(auto_papers) < 3:
        raise ValueError(f"Need >=3 auto papers for train/valid/test split, got {len(auto_papers)}")

    rng = np.random.default_rng(seed)
    m_perm = [manual_papers[i] for i in rng.permutation(len(manual_papers))]
    a_perm = [auto_papers[i] for i in rng.permutation(len(auto_papers))]

    partition_map: dict[str, str] = {}
    partition_map[m_perm[0]] = "test"
    partition_map[a_perm[0]] = "test"
    partition_map[m_perm[1]] = "valid"
    partition_map[a_perm[1]] = "valid"

    for aid in m_perm[2:] + a_perm[2:]:
        partition_map[aid] = "train"

    return partition_map


def compute_mrr_at_5(predictions_df: pd.DataFrame) -> dict[str, Any]:
    """Compute MRR@5, Hit@1 (Recall@1), and Hit@5 (Recall@5) on a predictions DataFrame."""
    if predictions_df.empty:
        return {
            "total_queries": 0,
            "positive_queries": 0,
            "mrr_at_5": 0.0,
            "mrr_at_5_positive_only": 0.0,
            "recall_at_1": 0.0,
            "recall_at_5": 0.0,
        }

    rr_all: list[float] = []
    rr_pos: list[float] = []
    hits_1 = 0
    hits_5 = 0

    cand_cols = ["cand_1", "cand_2", "cand_3", "cand_4", "cand_5"]
    for row in predictions_df.to_dict(orient="records"):
        gt = row.get("groundtruth_arxiv_id")
        gt_str = str(gt).strip() if gt is not None and str(gt).strip() not in ("", "None", "nan") else None
        rr = 0.0
        if gt_str is not None:
            for rank, col in enumerate(cand_cols, start=1):
                cval = row.get(col)
                if cval is not None and str(cval).strip() == gt_str:
                    rr = 1.0 / rank
                    if rank == 1:
                        hits_1 += 1
                    hits_5 += 1
                    break
            rr_pos.append(rr)
        rr_all.append(rr)

    n_all = len(rr_all)
    n_pos = len(rr_pos)
    return {
        "total_queries": n_all,
        "positive_queries": n_pos,
        "mrr_at_5": round(float(sum(rr_all) / max(1, n_all)), 4),
        "mrr_at_5_positive_only": round(float(sum(rr_pos) / max(1, n_pos)), 4),
        "recall_at_1": round(float(hits_1 / max(1, n_pos)), 4),
        "recall_at_5": round(float(hits_5 / max(1, n_pos)), 4),
    }


def evaluate_predictions(predictions_df: pd.DataFrame) -> dict[str, dict[str, Any]]:
    """Compute MRR@5 metrics separately for train, valid, and test partitions."""
    report: dict[str, dict[str, Any]] = {}
    for part in ("train", "valid", "test"):
        sub = predictions_df[predictions_df["partition"] == part]
        report[part] = compute_mrr_at_5(sub)
    return report


def compute_candidate_recall_at_k(pairs_df: pd.DataFrame, k: int = 20) -> float:
    """Measure Recall@K of pure TF-IDF Cosine candidate ranking (upper bound)."""
    if pairs_df.empty:
        return 0.0
    pos_queries = pairs_df[pairs_df["groundtruth_arxiv_id"].notna()][["arxiv_id", "citation_key", "groundtruth_arxiv_id"]].drop_duplicates()
    if pos_queries.empty:
        return 0.0

    work = pairs_df[["arxiv_id", "citation_key", "ref_arxiv_id", "groundtruth_arxiv_id", "title_char_ngram_cosine"]].copy()
    work = work.sort_values(["arxiv_id", "citation_key", "title_char_ngram_cosine", "ref_arxiv_id"], ascending=[True, True, False, True])

    hits = 0
    total = 0
    for (_, _), grp in work.groupby(["arxiv_id", "citation_key"], sort=False):
        gt = grp["groundtruth_arxiv_id"].iloc[0]
        if gt is None or (isinstance(gt, float) and np.isnan(gt)) or str(gt).strip() == "":
            continue
        total += 1
        topk = set(grp["ref_arxiv_id"].astype(str).head(k))
        if str(gt) in topk:
            hits += 1
    return round(float(hits / max(1, total)), 4)


def _rank_top_5(
    pairs_df: pd.DataFrame,
    scores: np.ndarray,
    partition_map: dict[str, str],
) -> pd.DataFrame:
    """Group scored candidate pairs by (arxiv_id, citation_key) and emit top-5 candidates."""
    work = pairs_df[["arxiv_id", "citation_key", "ref_arxiv_id", "groundtruth_arxiv_id"]].copy()
    work["score"] = scores
    work = work.sort_values(
        ["arxiv_id", "citation_key", "score", "ref_arxiv_id"],
        ascending=[True, True, False, True],
    )

    rows: list[dict[str, Any]] = []
    for (aid, ckey), grp in work.groupby(["arxiv_id", "citation_key"], sort=False):
        gt = grp["groundtruth_arxiv_id"].iloc[0]
        gt_val = None if (gt is None or (isinstance(gt, float) and np.isnan(gt)) or str(gt).strip() == "") else str(gt)
        top5 = grp["ref_arxiv_id"].astype(str).head(5).tolist()
        while len(top5) < 5:
            top5.append(None)
        rows.append({
            "arxiv_id": str(aid),
            "partition": partition_map[str(aid)],
            "citation_key": str(ckey),
            "groundtruth_arxiv_id": gt_val,
            "cand_1": top5[0],
            "cand_2": top5[1],
            "cand_3": top5[2],
            "cand_4": top5[3],
            "cand_5": top5[4],
        })

    return pd.DataFrame(rows)


def _build_model_registry(seed: int) -> dict[str, Any]:
    """Return the 3 supervised ML models to compare against TF-IDF Cosine baseline."""
    try:
        from lightgbm import LGBMClassifier
        lgbm_model = LGBMClassifier(
            n_estimators=200,
            max_depth=5,
            learning_rate=0.05,
            random_state=seed,
            n_jobs=-1,
            verbose=-1,
        )
        lgbm_label = "LightGBM"
    except ImportError:
        lgbm_model = HistGradientBoostingClassifier(
            max_iter=200,
            max_depth=5,
            learning_rate=0.05,
            random_state=seed,
        )
        lgbm_label = "LightGBM (HistGB fallback)"

    return {
        "Logistic Regression": make_pipeline(
            StandardScaler(),
            LogisticRegression(max_iter=1000, class_weight="balanced", random_state=seed),
        ),
        "Random Forest": RandomForestClassifier(
            n_estimators=150,
            max_depth=10,
            class_weight="balanced",
            random_state=seed,
            n_jobs=-1,
        ),
        lgbm_label: lgbm_model,
    }


def train_and_rank(
    pairs_df: pd.DataFrame,
    partition_map: dict[str, str],
    *,
    seed: int = 42,
) -> tuple[pd.DataFrame, dict[str, Any], pd.DataFrame]:
    """Evaluate TF-IDF Cosine + 3 ML models (4 total), select best on valid, and return benchmarks."""
    work = pairs_df.copy()
    work["partition"] = work["arxiv_id"].astype(str).map(partition_map)
    work = work[work["partition"].notna()].reset_index(drop=True)

    train_mask = work["partition"] == "train"
    valid_mask = work["partition"] == "valid"

    X_train = work.loc[train_mask, FEATURE_COLUMNS].to_numpy(dtype=float)
    y_train = work.loc[train_mask, "label"].to_numpy(dtype=int)
    X_valid = work.loc[valid_mask, FEATURE_COLUMNS].to_numpy(dtype=float)
    y_valid = work.loc[valid_mask, "label"].to_numpy(dtype=int)
    X_all = work[FEATURE_COLUMNS].to_numpy(dtype=float)

    cand_recall_20 = compute_candidate_recall_at_k(work, k=20)

    best_name = ""
    best_model = None
    best_valid_mrr = -1.0
    best_preds_df = pd.DataFrame()
    model_comparison: dict[str, Any] = {}
    benchmark_rows: list[dict[str, Any]] = []
    total_train_time = 0.0
    best_inference_time = 0.0

    # 1. Baseline 1: Pure TF-IDF Cosine (unsupervised)
    t_inf0 = time.perf_counter()
    tfidf_scores = (0.6 * work["title_char_ngram_cosine"] + 0.4 * work["title_word_tfidf_cosine"]).to_numpy(dtype=float)
    tfidf_preds = _rank_top_5(work, tfidf_scores, partition_map)
    tfidf_inf_sec = time.perf_counter() - t_inf0
    tfidf_eval = evaluate_predictions(tfidf_preds)
    model_comparison["TF-IDF Cosine"] = tfidf_eval
    benchmark_rows.append({
        "model": "Baseline 1: TF-IDF Cosine",
        "candidate_recall_at_20": cand_recall_20,
        "train_mrr_at_5": tfidf_eval["train"]["mrr_at_5"],
        "valid_mrr_at_5": tfidf_eval["valid"]["mrr_at_5"],
        "test_mrr_at_5": tfidf_eval["test"]["mrr_at_5"],
        "valid_mrr_at_5_pos": tfidf_eval["valid"]["mrr_at_5_positive_only"],
        "test_mrr_at_5_pos": tfidf_eval["test"]["mrr_at_5_positive_only"],
        "test_hit_at_1": tfidf_eval["test"]["recall_at_1"],
        "test_hit_at_5": tfidf_eval["test"]["recall_at_5"],
        "train_time_sec": 0.0,
        "inference_latency_ms": round(tfidf_inf_sec * 1000 / max(1, len(tfidf_preds)), 4),
        "peak_ram_mb": round(current_process_ram_mb(), 2),
    })

    # 2. Supervised ML Models: Logistic Regression, Random Forest, LightGBM
    for name, clf in _build_model_registry(seed).items():
        t_tr0 = time.perf_counter()
        clf.fit(X_train, y_train)
        train_sec = time.perf_counter() - t_tr0
        total_train_time += train_sec

        t_in0 = time.perf_counter()
        all_scores = clf.predict_proba(X_all)[:, 1]
        preds_df = _rank_top_5(work, all_scores, partition_map)
        inf_sec = time.perf_counter() - t_in0

        eval_metrics = evaluate_predictions(preds_df)
        model_comparison[name] = eval_metrics
        ram_now = round(current_process_ram_mb(), 2)

        benchmark_rows.append({
            "model": name,
            "candidate_recall_at_20": cand_recall_20,
            "train_mrr_at_5": eval_metrics["train"]["mrr_at_5"],
            "valid_mrr_at_5": eval_metrics["valid"]["mrr_at_5"],
            "test_mrr_at_5": eval_metrics["test"]["mrr_at_5"],
            "valid_mrr_at_5_pos": eval_metrics["valid"]["mrr_at_5_positive_only"],
            "test_mrr_at_5_pos": eval_metrics["test"]["mrr_at_5_positive_only"],
            "test_hit_at_1": eval_metrics["test"]["recall_at_1"],
            "test_hit_at_5": eval_metrics["test"]["recall_at_5"],
            "train_time_sec": round(train_sec, 4),
            "inference_latency_ms": round(inf_sec * 1000 / max(1, len(preds_df)), 4),
            "peak_ram_mb": ram_now,
        })

        # Select best supervised model by validation MRR@5 (tie-break by positive MRR@5)
        valid_score = (eval_metrics["valid"]["mrr_at_5"], eval_metrics["valid"]["mrr_at_5_positive_only"])
        best_score = (best_valid_mrr, model_comparison[best_name]["valid"]["mrr_at_5_positive_only"] if best_name else -1.0)
        if valid_score > best_score:
            best_valid_mrr = eval_metrics["valid"]["mrr_at_5"]
            best_name = name
            best_model = clf
            best_preds_df = preds_df
            best_inference_time = inf_sec

    # Compute permutation importance on validation set for the winning model
    perm_res = permutation_importance(
        best_model, X_valid, y_valid, n_repeats=5, random_state=seed, scoring="roc_auc"
    )
    feat_imp_df = pd.DataFrame({
        "feature": FEATURE_COLUMNS,
        "importance_mean": perm_res.importances_mean,
        "importance_std": perm_res.importances_std,
    }).sort_values("importance_mean", ascending=False).reset_index(drop=True)

    model_bench_df = pd.DataFrame(benchmark_rows)

    summary = {
        "selected_model": best_name,
        "candidate_recall_at_20": cand_recall_20,
        "model_comparison": model_comparison,
        "final_metrics": model_comparison[best_name],
        "model_benchmark_rows": benchmark_rows,
        "total_train_time_sec": round(total_train_time, 4),
        "best_inference_time_sec": round(best_inference_time, 4),
        "num_candidate_pairs": {
            part: int((work["partition"] == part).sum())
            for part in ("train", "valid", "test")
        },
    }
    return best_preds_df, summary, feat_imp_df, model_bench_df, best_model
