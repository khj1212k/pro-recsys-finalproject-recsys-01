import hashlib
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from evaluation.clustering.metrics import (
    basic_stats,
    bcubed,
    cosine_silhouette,
    dbcv,
    evaluate_run,
    stability_ari,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# synthetic data helpers
# ---------------------------------------------------------------------------

def make_sphere_blobs(n_per_cluster=30, n_clusters=3, dim=8, spread=0.05, seed=0):
    """L2-normalized Gaussian blobs on the unit sphere (BGE-M3-style embeddings).

    `spread` controls how tight each blob is around its center; small spread
    -> well-separated clusters, large spread -> overlapping clusters.
    """
    rng = np.random.default_rng(seed)
    centers = rng.normal(size=(n_clusters, dim))
    centers /= np.linalg.norm(centers, axis=1, keepdims=True)
    xs, ys = [], []
    for c_idx, center in enumerate(centers):
        pts = center + rng.normal(scale=spread, size=(n_per_cluster, dim))
        pts /= np.linalg.norm(pts, axis=1, keepdims=True)
        xs.append(pts)
        ys.append(np.full(n_per_cluster, c_idx))
    return np.vstack(xs).astype(np.float64), np.concatenate(ys)


WELL_SEPARATED_KWARGS = dict(n_per_cluster=30, n_clusters=3, dim=8, spread=0.03, seed=42)
OVERLAPPING_KWARGS = dict(n_per_cluster=30, n_clusters=3, dim=8, spread=0.6, seed=42)


def hdbscan_cluster(X, min_cluster_size=5, min_samples=3):
    import hdbscan

    clusterer = hdbscan.HDBSCAN(
        min_cluster_size=min_cluster_size, min_samples=min_samples, metric="euclidean"
    )
    return clusterer.fit_predict(X)


# ---------------------------------------------------------------------------
# dbcv
# ---------------------------------------------------------------------------

class TestDbcv:
    def test_well_separated_scores_higher_than_overlapping(self):
        X_well, y_well = make_sphere_blobs(**WELL_SEPARATED_KWARGS)
        X_overlap, y_overlap = make_sphere_blobs(**OVERLAPPING_KWARGS)

        score_well, reason_well = dbcv(X_well, y_well)
        score_overlap, reason_overlap = dbcv(X_overlap, y_overlap)

        assert reason_well is None
        assert reason_overlap is None
        assert score_well > score_overlap

    def test_all_noise_returns_none_with_reason(self):
        X = np.random.default_rng(0).normal(size=(10, 4))
        labels = np.full(10, -1)
        score, reason = dbcv(X, labels)
        assert score is None
        assert reason == "all_noise"

    def test_single_cluster_returns_none_with_reason(self):
        X = np.random.default_rng(0).normal(size=(10, 4))
        labels = np.zeros(10, dtype=int)
        score, reason = dbcv(X, labels)
        assert score is None
        assert reason == "single_cluster"

    def test_empty_input_returns_none_with_reason(self):
        X = np.zeros((0, 4))
        labels = np.array([], dtype=int)
        score, reason = dbcv(X, labels)
        assert score is None
        assert reason == "empty_input"

    def test_singleton_cluster_does_not_raise(self):
        # a cluster of size 1 makes hdbscan's own validity_index raise
        # internally; dbcv() must degrade gracefully instead of propagating.
        rng = np.random.default_rng(0)
        X = rng.normal(size=(21, 4))
        labels = np.array([0] * 10 + [1] * 9 + [2] + [-1])
        score, reason = dbcv(X, labels)
        assert score is None
        assert reason is not None

    def test_uses_float64_internally_regardless_of_input_dtype(self):
        X_well, y_well = make_sphere_blobs(**WELL_SEPARATED_KWARGS)
        score_f64, _ = dbcv(X_well.astype(np.float64), y_well)
        score_f32, _ = dbcv(X_well.astype(np.float32), y_well)
        assert score_f32 == pytest.approx(score_f64, abs=1e-6)

    def test_deterministic_for_same_inputs(self):
        X_well, y_well = make_sphere_blobs(**WELL_SEPARATED_KWARGS)
        score1, _ = dbcv(X_well, y_well)
        score2, _ = dbcv(X_well, y_well)
        assert score1 == score2

    def test_metric_defaults_to_euclidean(self):
        X_well, y_well = make_sphere_blobs(**WELL_SEPARATED_KWARGS)
        score_default, reason_default = dbcv(X_well, y_well)
        score_explicit, reason_explicit = dbcv(X_well, y_well, metric="euclidean")
        assert reason_default is None
        assert score_default == score_explicit

    def test_cosine_metric_is_accepted_and_still_separates_well_from_overlapping(self):
        # regression for the false claim that hdbscan's validity_index
        # doesn't support metric='cosine' -- it does, it just isn't the
        # default here (see dbcv's docstring).
        X_well, y_well = make_sphere_blobs(**WELL_SEPARATED_KWARGS)
        X_overlap, y_overlap = make_sphere_blobs(**OVERLAPPING_KWARGS)

        score_well, reason_well = dbcv(X_well, y_well, metric="cosine")
        score_overlap, reason_overlap = dbcv(X_overlap, y_overlap, metric="cosine")

        assert reason_well is None
        assert reason_overlap is None
        assert score_well > score_overlap

    def test_mismatched_lengths_raises_value_error(self):
        X_well, y_well = make_sphere_blobs(**WELL_SEPARATED_KWARGS)
        with pytest.raises(ValueError):
            dbcv(X_well, y_well[:-1])


# ---------------------------------------------------------------------------
# basic_stats
# ---------------------------------------------------------------------------

class TestBasicStats:
    def test_counts_and_noise_ratio(self):
        labels = np.array([0, 0, 0, 1, 1, 1, 1, -1, -1])
        stats = basic_stats(labels)
        assert stats["n_total"] == 9
        assert stats["n_clusters"] == 2
        assert stats["n_noise"] == 2
        assert stats["noise_ratio"] == pytest.approx(2 / 9)

    def test_size_distribution(self):
        # cluster sizes: 3, 4, 12
        labels = np.array([0] * 3 + [1] * 4 + [2] * 12)
        stats = basic_stats(labels)
        assert stats["cluster_size"]["min"] == 3
        assert stats["cluster_size"]["median"] == 4
        assert stats["cluster_size"]["max"] == 12

    def test_size_histogram_buckets(self):
        # sizes: 3 (bucket 3-4), 5 (bucket 5-9), 9 (bucket 5-9), 10 (bucket 10+)
        labels = np.concatenate(
            [
                np.full(3, 0),
                np.full(5, 1),
                np.full(9, 2),
                np.full(10, 3),
            ]
        )
        stats = basic_stats(labels)
        hist = stats["size_histogram"]
        assert hist["3-4"] == 1
        assert hist["5-9"] == 2
        assert hist["10+"] == 1
        assert sum(hist.values()) == stats["n_clusters"]

    def test_all_noise(self):
        labels = np.full(5, -1)
        stats = basic_stats(labels)
        assert stats["n_clusters"] == 0
        assert stats["noise_ratio"] == 1.0
        assert stats["cluster_size"]["min"] is None
        assert stats["cluster_size"]["median"] is None
        assert stats["cluster_size"]["max"] is None

    def test_empty_labels(self):
        labels = np.array([], dtype=int)
        stats = basic_stats(labels)
        assert stats["n_total"] == 0
        assert stats["n_clusters"] == 0
        assert stats["noise_ratio"] == 0.0

    def test_output_is_json_serializable(self):
        labels = np.array([0, 0, 0, 1, 1, 1, -1])
        json.dumps(basic_stats(labels))


# ---------------------------------------------------------------------------
# cosine_silhouette
# ---------------------------------------------------------------------------

class TestCosineSilhouette:
    def test_well_separated_scores_higher_than_overlapping(self):
        X_well, y_well = make_sphere_blobs(**WELL_SEPARATED_KWARGS)
        X_overlap, y_overlap = make_sphere_blobs(**OVERLAPPING_KWARGS)

        sil_well = cosine_silhouette(X_well, y_well)
        sil_overlap = cosine_silhouette(X_overlap, y_overlap)

        assert sil_well is not None
        assert sil_overlap is not None
        assert sil_well > sil_overlap

    def test_excludes_noise_points(self):
        X_well, y_well = make_sphere_blobs(**WELL_SEPARATED_KWARGS)
        labels_with_noise = y_well.copy()
        rng = np.random.default_rng(1)
        noisy_idx = rng.choice(len(labels_with_noise), size=5, replace=False)
        labels_with_noise[noisy_idx] = -1

        # should not raise, and should still be a valid silhouette in [-1, 1]
        sil = cosine_silhouette(X_well, labels_with_noise)
        assert sil is not None
        assert -1.0 <= sil <= 1.0

    def test_fewer_than_two_clusters_returns_none(self):
        X = np.random.default_rng(0).normal(size=(10, 4))
        labels_single = np.zeros(10, dtype=int)
        assert cosine_silhouette(X, labels_single) is None

        labels_all_noise = np.full(10, -1)
        assert cosine_silhouette(X, labels_all_noise) is None

    def test_one_cluster_after_excluding_noise_returns_none(self):
        X = np.random.default_rng(0).normal(size=(10, 4))
        labels = np.array([0] * 9 + [-1])
        assert cosine_silhouette(X, labels) is None


# ---------------------------------------------------------------------------
# bcubed
# ---------------------------------------------------------------------------

class TestBcubed:
    def test_perfect_clustering(self):
        labels_true = np.array([0, 0, 0, 1, 1, 1])
        labels_pred = np.array([0, 0, 0, 1, 1, 1])
        result = bcubed(labels_pred, labels_true)
        assert result["precision"] == pytest.approx(1.0)
        assert result["recall"] == pytest.approx(1.0)
        assert result["f1"] == pytest.approx(1.0)

    def test_hand_computed_example(self):
        # pred groups: {0,1}, {2,3}; true groups: {0,1,2}, {3}
        labels_pred = np.array([0, 0, 1, 1])
        labels_true = np.array([0, 0, 0, 1])
        result = bcubed(labels_pred, labels_true)
        # item0: prec=2/2=1,   rec=2/3
        # item1: prec=2/2=1,   rec=2/3
        # item2: prec=1/2=0.5, rec=1/3
        # item3: prec=1/2=0.5, rec=1/1=1
        expected_precision = (1 + 1 + 0.5 + 0.5) / 4
        expected_recall = (2 / 3 + 2 / 3 + 1 / 3 + 1) / 4
        expected_f1 = (
            2
            * expected_precision
            * expected_recall
            / (expected_precision + expected_recall)
        )
        assert result["precision"] == pytest.approx(expected_precision)
        assert result["recall"] == pytest.approx(expected_recall)
        assert result["f1"] == pytest.approx(expected_f1)

    def test_noise_treated_as_singletons(self):
        # pred: {0,1}, noise point 2 is its own singleton
        labels_pred = np.array([0, 0, -1])
        labels_true = np.array([0, 0, 0])
        result = bcubed(labels_pred, labels_true)
        # item0: prec=2/2=1, rec=2/3
        # item1: prec=2/2=1, rec=2/3
        # item2 (noise, singleton): prec=1/1=1, rec=1/3
        expected_precision = (1 + 1 + 1) / 3
        expected_recall = (2 / 3 + 2 / 3 + 1 / 3) / 3
        assert result["precision"] == pytest.approx(expected_precision)
        assert result["recall"] == pytest.approx(expected_recall)

    def test_two_noise_points_are_not_grouped_together(self):
        # two -1 points must NOT be treated as sharing a cluster with each
        # other -- each is its own singleton.
        labels_pred = np.array([-1, -1])
        labels_true = np.array([0, 0])
        result = bcubed(labels_pred, labels_true)
        # each item: pred_of={self} true_of={0,1}; overlap=1
        # precision = 1/1 = 1 for each -> avg 1.0
        # recall = 1/2 for each -> avg 0.5
        assert result["precision"] == pytest.approx(1.0)
        assert result["recall"] == pytest.approx(0.5)

    def test_well_separated_ranks_higher_than_overlapping(self):
        X_well, y_well = make_sphere_blobs(**WELL_SEPARATED_KWARGS)
        X_overlap, y_overlap = make_sphere_blobs(**OVERLAPPING_KWARGS)

        pred_well = hdbscan_cluster(X_well)
        pred_overlap = hdbscan_cluster(X_overlap)

        f1_well = bcubed(pred_well, y_well)["f1"]
        f1_overlap = bcubed(pred_overlap, y_overlap)["f1"]
        assert f1_well > f1_overlap

    def test_mismatched_lengths_raises(self):
        with pytest.raises(ValueError):
            bcubed(np.array([0, 1]), np.array([0, 1, 1]))

    def test_empty_input(self):
        result = bcubed(np.array([], dtype=int), np.array([], dtype=int))
        assert result["precision"] is None
        assert result["recall"] is None
        assert result["f1"] is None


# ---------------------------------------------------------------------------
# stability_ari
# ---------------------------------------------------------------------------

class TestStabilityAri:
    def test_identical_clustering_gives_high_mean_ari(self):
        X_well, y_well = make_sphere_blobs(**WELL_SEPARATED_KWARGS)

        def cluster_fn(X):
            return hdbscan_cluster(X)

        result = stability_ari(X_well, cluster_fn, n_runs=5, frac=0.8, seed=0)
        assert result["mean_ari"] > 0.8
        assert result["n_runs"] == 5
        assert len(result["aris"]) == 5

    def test_deterministic_with_seed(self):
        X_well, y_well = make_sphere_blobs(**WELL_SEPARATED_KWARGS)

        def cluster_fn(X):
            return hdbscan_cluster(X)

        result1 = stability_ari(X_well, cluster_fn, n_runs=5, frac=0.8, seed=7)
        result2 = stability_ari(X_well, cluster_fn, n_runs=5, frac=0.8, seed=7)
        assert result1["aris"] == result2["aris"]
        assert result1["mean_ari"] == result2["mean_ari"]

    def test_random_relabeling_gives_low_mean_ari(self):
        X_well, y_well = make_sphere_blobs(**WELL_SEPARATED_KWARGS)
        rng = np.random.default_rng(123)

        def random_cluster_fn(X):
            return rng.integers(0, 3, size=X.shape[0])

        result = stability_ari(X_well, random_cluster_fn, n_runs=5, frac=0.8, seed=1)
        assert result["mean_ari"] < 0.3

    def test_zero_runs_returns_none(self):
        X_well, y_well = make_sphere_blobs(**WELL_SEPARATED_KWARGS)

        def cluster_fn(X):
            return hdbscan_cluster(X)

        result = stability_ari(X_well, cluster_fn, n_runs=0, frac=0.8, seed=0)
        assert result["mean_ari"] is None
        assert result["std_ari"] is None
        assert result["aris"] == []


# ---------------------------------------------------------------------------
# evaluate_run
# ---------------------------------------------------------------------------

class TestEvaluateRun:
    def test_bundles_all_metrics_and_is_json_serializable(self):
        X_well, y_well = make_sphere_blobs(**WELL_SEPARATED_KWARGS)
        pred = hdbscan_cluster(X_well)

        report = evaluate_run(
            X_well,
            pred,
            labels_true=y_well,
            params={"min_cluster_size": 5, "min_samples": 3},
        )

        json.dumps(report)  # must not raise

        assert report["n"] == X_well.shape[0]
        assert report["params"] == {"min_cluster_size": 5, "min_samples": 3}
        assert "basic_stats" in report
        assert "dbcv" in report
        assert "dbcv_reason" in report
        assert "cosine_silhouette" in report
        assert report["bcubed"]["f1"] is not None

    def test_input_hash_is_sha256_of_x_bytes(self):
        X_well, y_well = make_sphere_blobs(**WELL_SEPARATED_KWARGS)
        pred = hdbscan_cluster(X_well)
        report = evaluate_run(X_well, pred)

        expected = hashlib.sha256(
            np.ascontiguousarray(X_well, dtype=np.float64).tobytes()
        ).hexdigest()
        assert report["input_hash"] == expected

    def test_without_labels_true_bcubed_is_none(self):
        X_well, y_well = make_sphere_blobs(**WELL_SEPARATED_KWARGS)
        pred = hdbscan_cluster(X_well)
        report = evaluate_run(X_well, pred)
        assert report["bcubed"] is None

    def test_stability_included_when_cluster_fn_given(self):
        X_well, y_well = make_sphere_blobs(**WELL_SEPARATED_KWARGS)
        pred = hdbscan_cluster(X_well)

        def cluster_fn(X):
            return hdbscan_cluster(X)

        report = evaluate_run(
            X_well, pred, cluster_fn=cluster_fn, stability_n_runs=3, seed=0
        )
        assert report["stability"] is not None
        assert report["stability"]["n_runs"] == 3

    def test_stability_none_when_no_cluster_fn(self):
        X_well, y_well = make_sphere_blobs(**WELL_SEPARATED_KWARGS)
        pred = hdbscan_cluster(X_well)
        report = evaluate_run(X_well, pred)
        assert report["stability"] is None

    def test_default_params_is_empty_dict(self):
        X_well, y_well = make_sphere_blobs(**WELL_SEPARATED_KWARGS)
        pred = hdbscan_cluster(X_well)
        report = evaluate_run(X_well, pred)
        assert report["params"] == {}


# ---------------------------------------------------------------------------
# CLI (`python -m evaluation.clustering.metrics`)
# ---------------------------------------------------------------------------

class TestCli:
    def _run_cli(self, args):
        return subprocess.run(
            [sys.executable, "-m", "evaluation.clustering.metrics", *args],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )

    def test_writes_json_report_from_npy_files(self, tmp_path):
        X_well, y_well = make_sphere_blobs(**WELL_SEPARATED_KWARGS)
        embeddings_path = tmp_path / "embeddings.npy"
        labels_path = tmp_path / "labels.npy"
        out_path = tmp_path / "report.json"
        np.save(embeddings_path, X_well)
        np.save(labels_path, hdbscan_cluster(X_well))

        result = self._run_cli(
            [
                "--embeddings", str(embeddings_path),
                "--labels", str(labels_path),
                "--out", str(out_path),
            ]
        )

        assert result.returncode == 0, result.stderr
        report = json.loads(out_path.read_text(encoding="utf-8"))
        assert report["n"] == X_well.shape[0]
        assert "basic_stats" in report
        assert "dbcv" in report
        assert "dbcv_reason" in report
        assert "cosine_silhouette" in report
        assert report["bcubed"] is None
        assert report["stability"] is None

    def test_truth_argument_populates_bcubed(self, tmp_path):
        X_well, y_well = make_sphere_blobs(**WELL_SEPARATED_KWARGS)
        embeddings_path = tmp_path / "embeddings.npy"
        labels_path = tmp_path / "labels.npy"
        truth_path = tmp_path / "truth.npy"
        out_path = tmp_path / "report.json"
        np.save(embeddings_path, X_well)
        np.save(labels_path, y_well)
        np.save(truth_path, y_well)

        result = self._run_cli(
            [
                "--embeddings", str(embeddings_path),
                "--labels", str(labels_path),
                "--truth", str(truth_path),
                "--out", str(out_path),
            ]
        )

        assert result.returncode == 0, result.stderr
        report = json.loads(out_path.read_text(encoding="utf-8"))
        assert report["bcubed"]["f1"] == pytest.approx(1.0)
