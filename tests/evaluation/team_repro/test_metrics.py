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


# --- v2 REQUIRED CHANGE #8: nested bootstrap (시드 x 유저) -----------------------


def test_nested_bootstrap_ci_mean_matches_point_estimate_and_reports_n_seeds():
    # 3개 시드, 각 시드마다 동일한 20명 유저 (시드 간 값은 살짝 다름 - 학습 무작위성 모사)
    per_user_by_seed = [
        {i: {"m": float(i % 3) + offset} for i in range(20)} for offset in (0.0, 0.1, -0.1)
    ]
    result = M.nested_bootstrap_ci(per_user_by_seed, "m", n_boot=300, seed=1)
    true_mean = np.mean([m["m"] for pu in per_user_by_seed for m in pu.values()])
    assert result["mean"] == pytest.approx(true_mean)
    assert result["ci_lo"] <= result["mean"] <= result["ci_hi"]
    assert result["n_seeds"] == 3
    assert result["n_users"] == 20


def test_nested_bootstrap_ci_empty_seeds_returns_nan():
    result = M.nested_bootstrap_ci([], "m", n_boot=10)
    assert result["n_seeds"] == 0
    assert np.isnan(result["mean"])


def test_nested_bootstrap_ci_only_uses_users_common_to_all_seeds():
    per_user_by_seed = [
        {1: {"m": 1.0}, 2: {"m": 2.0}},
        {1: {"m": 1.0}, 3: {"m": 3.0}},  # user2/3는 시드 한쪽에만 있음
    ]
    result = M.nested_bootstrap_ci(per_user_by_seed, "m", n_boot=50, seed=0)
    assert result["n_users"] == 1


def test_nested_bootstrap_paired_diff_zero_when_arms_identical():
    per_user_by_seed = [{i: {"m": float(i)} for i in range(15)} for _ in range(3)]
    result = M.nested_bootstrap_paired_diff(per_user_by_seed, per_user_by_seed, "m", n_boot=300, seed=2)
    assert result["effect"] == pytest.approx(0.0)
    assert result["ci_lo"] <= 0.0 <= result["ci_hi"]


def test_nested_bootstrap_paired_diff_detects_constant_shift():
    a = [{i: {"m": float(i)} for i in range(15)} for _ in range(3)]
    b = [{i: {"m": float(i) + 2.0} for i in range(15)} for _ in range(3)]
    result = M.nested_bootstrap_paired_diff(a, b, "m", n_boot=300, seed=3)
    assert result["effect"] == pytest.approx(2.0)
    assert result["n_seeds_a"] == 3 and result["n_seeds_b"] == 3


def test_nested_bootstrap_paired_diff_wider_than_plain_bootstrap_when_seeds_vary():
    """시드마다 값이 크게 요동치면, 유저만 재표본하는 bootstrap_paired_diff보다
    시드까지 재표본하는 nested 쪽 CI가 더 넓어야 한다(v1이 놓친 변동성을 잡아낸다는
    것의 직접적인 회귀 테스트)."""
    rng = np.random.default_rng(0)
    n_users = 25
    per_user_by_seed = []
    for _ in range(6):
        seed_shift = rng.normal(scale=2.0)  # 시드마다 큰 흔들림
        per_user_by_seed.append({i: {"m": float(i % 3) + seed_shift} for i in range(n_users)})

    nested = M.nested_bootstrap_ci(per_user_by_seed, "m", n_boot=500, seed=5)

    pooled = {i: {"m": float(np.mean([pu[i]["m"] for pu in per_user_by_seed]))} for i in range(n_users)}
    plain = M.bootstrap_ci(pooled, "m", n_boot=500, seed=5)

    nested_width = nested["ci_hi"] - nested["ci_lo"]
    plain_width = plain["ci_hi"] - plain["ci_lo"]
    assert nested_width > plain_width


def test_nested_bootstrap_paired_diff_paired_seeds_narrower_when_model_is_shared():
    """두 조건이 같은 시드의 같은 모델을 공유하면(A의 시드 변동이 B에도 그대로 실림)
    paired_seeds=True가 공유 변동을 상쇄해 독립 재표본보다 구간이 좁아야 한다."""
    rng = np.random.default_rng(1)
    n_users = 20
    a, b = [], []
    for _ in range(5):
        shift = rng.normal(scale=1.0)  # 시드(=모델)마다 큰 공통 흔들림
        a.append({i: {"m": float(i % 4) + shift} for i in range(n_users)})
        b.append({i: {"m": float(i % 4) + shift + 0.3} for i in range(n_users)})
    indep = M.nested_bootstrap_paired_diff(a, b, "m", n_boot=500, seed=0)
    paired = M.nested_bootstrap_paired_diff(a, b, "m", n_boot=500, seed=0, paired_seeds=True)
    assert paired["effect"] == pytest.approx(0.3)
    assert paired["paired_seeds"] is True and indep["paired_seeds"] is False
    assert (paired["ci_hi"] - paired["ci_lo"]) < (indep["ci_hi"] - indep["ci_lo"])
    assert paired["ci_lo"] > 0  # 공통 변동을 상쇄하면 일정한 +0.3 이동이 검출된다


def test_nested_bootstrap_paired_diff_paired_seeds_requires_equal_seed_counts():
    a = [{i: {"m": 1.0} for i in range(5)} for _ in range(3)]
    b = [{i: {"m": 1.0} for i in range(5)} for _ in range(2)]
    with pytest.raises(ValueError):
        M.nested_bootstrap_paired_diff(a, b, "m", n_boot=10, paired_seeds=True)


def test_ci_verdict_classifies_by_ci_sign_only():
    assert M.ci_verdict({"effect": 0.1, "ci_lo": 0.01, "ci_hi": 0.2}) == "positive"
    assert M.ci_verdict({"effect": -0.1, "ci_lo": -0.2, "ci_hi": -0.01}) == "negative"
    assert M.ci_verdict({"effect": -0.03, "ci_lo": -0.15, "ci_hi": 0.07}) == "inconclusive"
    assert M.ci_verdict({"effect": float("nan"), "ci_lo": float("nan"), "ci_hi": float("nan")}) == "nan"
    assert M.ci_verdict({}) == "nan"
