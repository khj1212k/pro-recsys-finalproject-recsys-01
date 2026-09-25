"""Clustering-quality evaluation metrics for HDBSCAN news clustering.

Pure numpy/sklearn/hdbscan implementations, meant to be logged per run
alongside (or in place of) the LLM-judge quality signal used by
`ai_workspace/core/clustering/hdbscan_clusterer.py` and `split_v2.py`.

Every function returns plain Python types (float/int/str/None/dict/list)
so results can go straight through `json.dumps`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
from sklearn.metrics import adjusted_rand_score, silhouette_score

__all__ = [
    "dbcv",
    "basic_stats",
    "cosine_silhouette",
    "bcubed",
    "stability_ari",
    "evaluate_run",
]


def _non_noise_cluster_ids(labels: np.ndarray) -> List[Any]:
    return sorted({label for label in labels.tolist() if label != -1})


def dbcv(X: np.ndarray, labels: np.ndarray) -> Tuple[Optional[float], Optional[str]]:
    """Density-Based Clustering Validation via hdbscan.validity.validity_index.

    `X` is assumed to already be L2-normalized (unit-norm rows), as produced
    by the BGE-M3 embedding step. For unit vectors,
    ||a - b||^2 == 2 - 2*cos(a, b), so euclidean distance is a strictly
    monotonic function of cosine distance on the sphere: nearest-neighbour
    and MST structure -- which is all DBCV depends on -- is identical
    whether computed with metric='euclidean' on normalized vectors or with
    a genuine cosine metric. We therefore pass metric='euclidean', since
    hdbscan's Cython validity implementation only special-cases a handful
    of metrics for its internal core-distance computation and does not
    include 'cosine'.

    hdbscan's validity_index requires float64 input (its Cython code is
    compiled against `double_t`; float32 raises a buffer dtype mismatch).

    Returns (score, None) on success, or (None, reason) for degenerate
    inputs (empty input, all points labeled noise, only a single cluster,
    or any other configuration -- e.g. a singleton cluster -- that makes
    hdbscan's own validity_index computation ill-defined).
    """
    X = np.asarray(X, dtype=np.float64)
    labels = np.asarray(labels)

    if X.shape[0] == 0 or labels.shape[0] == 0:
        return None, "empty_input"

    cluster_ids = _non_noise_cluster_ids(labels)
    if len(cluster_ids) == 0:
        return None, "all_noise"
    if len(cluster_ids) < 2:
        return None, "single_cluster"

    import hdbscan.validity as hdbscan_validity

    try:
        value = hdbscan_validity.validity_index(X, labels, metric="euclidean")
    except Exception as exc:  # pragma: no cover - defensive, exact type varies
        return None, f"computation_error: {exc}"

    if value is None or (isinstance(value, float) and np.isnan(value)):
        return None, "degenerate_result"

    return float(value), None


def basic_stats(labels: np.ndarray) -> Dict[str, Any]:
    """Cluster-count, noise-ratio and size-distribution summary.

    Size histogram buckets are 3-4, 5-9 and 10+ (the sizes HDBSCAN's
    `min_cluster_size` is normally tuned to produce for this pipeline);
    an extra "<3" bucket catches any smaller cluster so bucket counts
    always sum to n_clusters.
    """
    labels = np.asarray(labels)
    n_total = int(labels.shape[0])

    cluster_ids = _non_noise_cluster_ids(labels)
    n_clusters = len(cluster_ids)
    n_noise = int(np.sum(labels == -1))
    noise_ratio = (n_noise / n_total) if n_total > 0 else 0.0

    sizes = [int(np.sum(labels == cid)) for cid in cluster_ids]

    if sizes:
        size_stats = {
            "min": int(np.min(sizes)),
            "median": float(np.median(sizes)),
            "max": int(np.max(sizes)),
        }
    else:
        size_stats = {"min": None, "median": None, "max": None}

    histogram = {"<3": 0, "3-4": 0, "5-9": 0, "10+": 0}
    for size in sizes:
        if size < 3:
            histogram["<3"] += 1
        elif size <= 4:
            histogram["3-4"] += 1
        elif size <= 9:
            histogram["5-9"] += 1
        else:
            histogram["10+"] += 1

    return {
        "n_total": n_total,
        "n_clusters": n_clusters,
        "n_noise": n_noise,
        "noise_ratio": float(noise_ratio),
        "cluster_size": size_stats,
        "size_histogram": histogram,
    }


def cosine_silhouette(X: np.ndarray, labels: np.ndarray) -> Optional[float]:
    """Mean silhouette score (cosine distance), excluding noise points.

    Returns None if fewer than 2 non-noise clusters remain, or if there
    are too few non-noise points to form a valid silhouette (sklearn
    requires 2 <= n_labels <= n_samples - 1 among the scored points).
    """
    X = np.asarray(X, dtype=np.float64)
    labels = np.asarray(labels)

    mask = labels != -1
    n_points = int(np.sum(mask))
    if n_points < 2:
        return None

    kept_labels = labels[mask]
    n_clusters = len(set(kept_labels.tolist()))
    if n_clusters < 2 or n_clusters >= n_points:
        return None

    try:
        score = silhouette_score(X[mask], kept_labels, metric="cosine")
    except ValueError:
        return None

    return float(score)


def _cluster_membership(labels: np.ndarray) -> List[set]:
    """Index -> its cluster's index set, treating each noise point (-1) as
    its own singleton cluster (documented choice for `bcubed`, matching
    common practice for evaluating clustering with an explicit outlier
    label: an unassigned point should only be compared against itself,
    never rewarded or penalized for accidentally sharing "-1" with other
    unrelated outliers)."""
    groups: Dict[Any, List[int]] = {}
    for i, label in enumerate(labels.tolist()):
        key = ("__noise__", i) if label == -1 else label
        groups.setdefault(key, []).append(i)

    membership: List[Optional[set]] = [None] * len(labels)
    for members in groups.values():
        member_set = set(members)
        for i in members:
            membership[i] = member_set
    return membership


def bcubed(labels_pred: np.ndarray, labels_true: np.ndarray) -> Dict[str, Optional[float]]:
    """B-cubed precision/recall/F1 between predicted and true labels.

    Noise (-1) in either array is treated as a set of singleton clusters,
    one per noise point -- see `_cluster_membership`. This choice applies
    symmetrically to `labels_pred` and `labels_true`, so a ground-truth
    array that also marks some items as unclustered outliers is handled
    the same way.
    """
    labels_pred = np.asarray(labels_pred)
    labels_true = np.asarray(labels_true)

    if labels_pred.shape[0] != labels_true.shape[0]:
        raise ValueError("labels_pred and labels_true must be the same length")

    n = labels_pred.shape[0]
    if n == 0:
        return {"precision": None, "recall": None, "f1": None}

    pred_membership = _cluster_membership(labels_pred)
    true_membership = _cluster_membership(labels_true)

    precisions = np.empty(n, dtype=np.float64)
    recalls = np.empty(n, dtype=np.float64)
    for i in range(n):
        overlap = len(pred_membership[i] & true_membership[i])
        precisions[i] = overlap / len(pred_membership[i])
        recalls[i] = overlap / len(true_membership[i])

    precision = float(np.mean(precisions))
    recall = float(np.mean(recalls))
    f1 = 0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall)

    return {"precision": precision, "recall": recall, "f1": float(f1)}


def stability_ari(
    X: np.ndarray,
    cluster_fn: Callable[[np.ndarray], np.ndarray],
    n_runs: int = 20,
    frac: float = 0.8,
    seed: Optional[int] = None,
) -> Dict[str, Any]:
    """Bootstrap-subsample stability of `cluster_fn` on `X`.

    Clusters the full `X` once, then repeatedly draws a `frac`-sized
    subsample (without replacement) of the points, re-clusters just that
    subsample, and compares the subsample's labels against the full
    clustering's labels restricted to those same (overlapping) points via
    adjusted_rand_score. High mean ARI / low std means `cluster_fn`'s
    output doesn't depend heavily on which points happen to be present --
    i.e. the clustering is stable.
    """
    X = np.asarray(X)
    n = X.shape[0]
    full_labels = np.asarray(cluster_fn(X))

    if n_runs <= 0:
        return {"mean_ari": None, "std_ari": None, "n_runs": 0, "aris": []}

    rng = np.random.default_rng(seed)
    sample_size = max(1, int(round(frac * n)))
    sample_size = min(sample_size, n)

    aris: List[float] = []
    for _ in range(n_runs):
        idx = np.sort(rng.choice(n, size=sample_size, replace=False))
        sub_labels = np.asarray(cluster_fn(X[idx]))
        ari = adjusted_rand_score(full_labels[idx], sub_labels)
        aris.append(float(ari))

    return {
        "mean_ari": float(np.mean(aris)),
        "std_ari": float(np.std(aris)),
        "n_runs": n_runs,
        "aris": aris,
    }


def evaluate_run(
    X: np.ndarray,
    labels: np.ndarray,
    labels_true: Optional[np.ndarray] = None,
    cluster_fn: Optional[Callable[[np.ndarray], np.ndarray]] = None,
    stability_n_runs: int = 20,
    stability_frac: float = 0.8,
    seed: Optional[int] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Bundle every metric above into one JSON-serializable report.

    `params` is passed through verbatim (e.g. the min_cluster_size /
    min_samples used to produce `labels`) so a report is self-describing
    when logged. `cluster_fn` is optional: stability_ari is only computed
    -- and takes O(n_runs) extra clustering calls -- when it's given,
    since it needs a way to re-cluster subsamples.
    """
    X = np.asarray(X, dtype=np.float64)
    labels = np.asarray(labels)

    input_hash = hashlib.sha256(np.ascontiguousarray(X).tobytes()).hexdigest()

    dbcv_score, dbcv_reason = dbcv(X, labels)

    report: Dict[str, Any] = {
        "n": int(X.shape[0]),
        "input_hash": input_hash,
        "params": dict(params) if params else {},
        "basic_stats": basic_stats(labels),
        "dbcv": dbcv_score,
        "dbcv_reason": dbcv_reason,
        "cosine_silhouette": cosine_silhouette(X, labels),
        "bcubed": bcubed(labels, labels_true) if labels_true is not None else None,
        "stability": (
            stability_ari(
                X,
                cluster_fn,
                n_runs=stability_n_runs,
                frac=stability_frac,
                seed=seed,
            )
            if cluster_fn is not None
            else None
        ),
    }
    return report


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m evaluation.clustering.metrics",
        description="Compute clustering-quality metrics for a saved run.",
    )
    parser.add_argument("--embeddings", required=True, help="path to a .npy file of embeddings (n, d)")
    parser.add_argument("--labels", required=True, help="path to a .npy file of predicted cluster labels (n,)")
    parser.add_argument("--truth", default=None, help="optional path to a .npy file of ground-truth labels (n,)")
    parser.add_argument("--out", required=True, help="path to write the JSON report to")
    return parser


def main(argv: Optional[List[str]] = None) -> None:
    args = _build_arg_parser().parse_args(argv)

    X = np.load(args.embeddings)
    labels = np.load(args.labels)
    labels_true = np.load(args.truth) if args.truth else None

    report = evaluate_run(X, labels, labels_true=labels_true)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
