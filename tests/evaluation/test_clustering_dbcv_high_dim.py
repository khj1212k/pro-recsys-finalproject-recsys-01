"""DBCV의 all-points core distance는 (Σ (1/d_ij)^D / (n-1))^(-1/D)이고 D는 데이터 차원이다.
hdbscan.validity는 이것을 그대로 거듭제곱으로 계산해서 BGE-M3(D=1024)처럼 거리가 1보다 작은
쌍이 있으면 (1/d)^1024가 inf로 넘치고, core distance가 전부 0이 된다 - 그러면 상호 도달 거리가
원 거리로 퇴화해 논문 정의와 다른 값이 조용히 나온다(2026-09-26 첫 클러스터링 실행에서
overflow 경고와 함께 확인). metrics.dbcv는 같은 식을 로그 공간(logsumexp)에서 계산한다.
"""
import warnings

import numpy as np
import pytest

from evaluation.clustering import metrics
from evaluation.clustering.metrics import dbcv, stable_all_points_core_distance


def _pairwise(X):
    return np.linalg.norm(X[:, None, :] - X[None, :, :], axis=-1)


def _sphere_blobs(dim, n=25, k=3, spread=0.35, seed=0):
    rng = np.random.default_rng(seed)
    centers = rng.normal(size=(k, dim))
    centers /= np.linalg.norm(centers, axis=1, keepdims=True)
    X = np.vstack([c + spread * rng.normal(size=(n, dim)) / np.sqrt(dim) for c in centers])
    X /= np.linalg.norm(X, axis=1, keepdims=True)
    return X, np.repeat(np.arange(k), n)


def test_stable_core_distance_equals_library_formula_in_low_dimension():
    import hdbscan.validity as validity

    X, _ = _sphere_blobs(dim=8)
    D = _pairwise(X[:25])
    np.testing.assert_allclose(
        stable_all_points_core_distance(D, d=8), validity.all_points_core_distance(D.copy(), d=8), rtol=1e-9
    )


def test_library_core_distance_collapses_to_zero_in_1024d_but_stable_one_is_bounded():
    import hdbscan.validity as validity

    X, _ = _sphere_blobs(dim=1024)
    D = _pairwise(X[:25])
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        lib = validity.all_points_core_distance(D.copy(), d=1024)
    assert np.all(lib == 0.0)  # 넘침으로 퇴화한 현재 라이브러리 동작

    core = stable_all_points_core_distance(D, d=1024)
    off = np.where(np.eye(len(D), dtype=bool), np.inf, D)
    nn = off.min(axis=1)
    # 정의상 nn ≤ core ≤ nn·(n-1)^(1/D)
    assert np.all(core >= nn * (1 - 1e-12))
    assert np.all(core <= nn * (len(D) - 1) ** (1 / 1024) * (1 + 1e-12))


def test_dbcv_matches_library_where_it_does_not_overflow():
    import hdbscan.validity as validity

    X, y = _sphere_blobs(dim=8)
    ours, reason = dbcv(X, y)
    assert reason is None
    assert ours == pytest.approx(validity.validity_index(X.astype(np.float64), y), rel=1e-9)


def test_dbcv_1024d_uses_stable_core_distance_and_restores_library_function():
    import hdbscan.validity as validity

    original = validity.all_points_core_distance
    X, y = _sphere_blobs(dim=1024)
    score, reason = dbcv(X, y)
    raw, _ = dbcv(X, y, stable=False)

    assert reason is None and np.isfinite(score)
    # 라이브러리 값은 core distance가 0으로 퇴화한 값이라 안정화한 값과 달라야 한다
    assert score != pytest.approx(raw, abs=1e-6)
    assert validity.all_points_core_distance is original


def test_evaluate_run_reports_library_value_separately_for_transparency():
    X, y = _sphere_blobs(dim=1024)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        report = metrics.evaluate_run(X, y)
    assert report["dbcv"] is not None
    assert "dbcv_hdbscan_unstabilized" in report
