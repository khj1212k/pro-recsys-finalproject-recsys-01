"""metrics.py 테스트: 손으로 계산한 예시로 per_user_metrics/aggregate/AUC/부트스트랩을
검증한다. Evaluator 자체(MRR/Precision/nDCG/Coverage 공식)는 recommend_engine
팀 코드를 그대로 쓰므로 여기서는 그 위에 얹은 유틸만 검증한다.
"""
import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "evaluation" / "recsys" / "team_repro"))

import metrics as M  # noqa: E402


class _FakeEvaluator:
    """Evaluator.evaluate_user와 같은 인터페이스의 손계산 가능한 가짜 구현."""

    def evaluate_user(self, recommended, relevant, item_categories=None):
        hit_at_1 = 1.0 if recommended and recommended[0] in relevant else 0.0
        precision_at_2 = sum(1 for x in recommended[:2] if x in relevant) / 2
        return {"hit@1": hit_at_1, "precision@2": precision_at_2}


def test_per_user_metrics_skips_users_without_ground_truth():
    ev = _FakeEvaluator()
    recs = {1: [10, 20], 2: [30, 40], 3: [50, 60]}
    gt = {1: {10}, 2: set()}  # user2: 빈 set -> 제외, user3: gt 자체가 없음 -> 제외
    out = M.per_user_metrics(ev, recs, gt, {})
    assert set(out.keys()) == {1}
    assert out[1]["hit@1"] == 1.0
    assert out[1]["precision@2"] == 0.5


def test_aggregate_averages_and_counts_users():
    per_user = {1: {"hit@1": 1.0, "precision@2": 0.5}, 2: {"hit@1": 0.0, "precision@2": 1.0}}
    agg = M.aggregate(per_user)
    assert agg["hit@1"] == pytest.approx(0.5)
    assert agg["precision@2"] == pytest.approx(0.75)
    assert agg["num_users"] == 2


def test_aggregate_empty_returns_empty_dict():
    assert M.aggregate({}) == {}


def test_compute_auc_perfect_separation_is_1():
    y_true = [0, 0, 1, 1]
    y_score = [0.1, 0.2, 0.8, 0.9]
    assert M.compute_auc(y_true, y_score) == pytest.approx(1.0)


def test_compute_auc_returns_none_for_single_class():
    assert M.compute_auc([1, 1, 1], [0.1, 0.5, 0.9]) is None


def test_bootstrap_ci_mean_matches_point_estimate_and_ci_contains_point():
    per_user = {i: {"m": float(i % 3)} for i in range(50)}
    result = M.bootstrap_ci(per_user, "m", n_boot=500, seed=1)
    true_mean = np.mean([v["m"] for v in per_user.values()])
    assert result["mean"] == pytest.approx(true_mean)
    assert result["ci_lo"] <= result["mean"] <= result["ci_hi"]
    assert result["n_users"] == 50


def test_bootstrap_ci_empty_users_returns_nan():
    result = M.bootstrap_ci({}, "m", n_boot=10)
    assert result["n_users"] == 0
    assert np.isnan(result["mean"])


def test_bootstrap_paired_diff_zero_when_arms_identical():
    per_user = {i: {"m": float(i)} for i in range(20)}
    result = M.bootstrap_paired_diff(per_user, per_user, "m", n_boot=500, seed=2)
    assert result["effect"] == pytest.approx(0.0)
    assert result["ci_lo"] <= 0.0 <= result["ci_hi"]


def test_bootstrap_paired_diff_detects_constant_positive_shift():
    a = {i: {"m": float(i)} for i in range(30)}
    b = {i: {"m": float(i) + 1.0} for i in range(30)}
    result = M.bootstrap_paired_diff(a, b, "m", n_boot=500, seed=3)
    assert result["effect"] == pytest.approx(1.0)
    # 상수 이동이라 분산이 0에 가까워 CI가 매우 좁아야 함
    assert result["ci_hi"] - result["ci_lo"] < 0.2


def test_bootstrap_paired_diff_only_uses_common_users():
    a = {1: {"m": 1.0}, 2: {"m": 2.0}}
    b = {2: {"m": 5.0}, 3: {"m": 9.0}}
    result = M.bootstrap_paired_diff(a, b, "m", n_boot=200, seed=4)
    assert result["n_users"] == 1
    assert result["effect"] == pytest.approx(3.0)
