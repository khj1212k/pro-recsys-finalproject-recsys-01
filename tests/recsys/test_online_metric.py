"""E13의 1차 지표(evaluation/recsys/online.py)를 정답을 아는 합성 칸 로그로 검증한다 (ADR 0025 사전 등록 1).

칸 로그는 서빙과 같은 탐색 메커니즘으로 만들고, 클릭은 위치 편향 θ_p x 매력으로 낸다. 보상에 클릭 확률을
그대로 넣으면(잡음 없음) 지표의 값을 식으로 맞출 수 있다.
"""
import numpy as np
import pytest

from app.recsys.exploration import plan_slate
from evaluation.recsys.online import (
    VERDICT_ACTIVE_BETTER,
    VERDICT_ACTIVE_WORSE,
    VERDICT_UNDECIDED,
    stratified_ctr_difference,
)
from evaluation.recsys.ope import power_law_theta

K = 20
THETA = power_law_theta(K, 1.0)


def _slots(seed, groups, bernoulli=False, n_eligible=45, top_k=K):
    """groups: (사용자 수, 사용자당 요청 수, 탐색 칸 수, 결정론 칸의 매력, 탐색 칸의 매력)의 목록."""
    rng = np.random.default_rng(seed)
    eligible = list(range(n_eligible))
    det_ranked = eligible[:top_k]
    cols = {k: [] for k in ("request", "user", "position", "explored", "clicked", "n_explore", "slate_size")}
    request = user = 0
    for n_users, per_user, m, r_det, r_exp in groups:
        for _ in range(n_users):
            for _ in range(per_user):
                plan = plan_slate(det_ranked, eligible, top_k, m, rng)
                for s in plan.slots:
                    p_click = THETA[s.position] * (r_exp if s.explored else r_det)
                    cols["request"].append(request)
                    cols["user"].append(user)
                    cols["position"].append(s.position)
                    cols["explored"].append(s.explored)
                    cols["clicked"].append(float(rng.random() < p_click) if bernoulli else p_click)
                    cols["n_explore"].append(plan.n_explore)
                    cols["slate_size"].append(plan.slate_size)
                request += 1
            user += 1
    return {k: np.asarray(v) for k, v in cols.items()}


def _expected_delta(cols, gap_by_m):
    """층 가중치는 실현된 탐색 칸 수다. 층 (m, p)의 참 차이는 θ_p x (그 m의 매력 차이)."""
    explore = cols["explored"]
    total = explore.sum()
    return sum(
        ((explore & (cols["n_explore"] == m) & (cols["position"] == p)).sum() / total) * THETA[p] * gap
        for m, gap in gap_by_m.items()
        for p in range(K)
    )


def test_the_metric_recovers_the_true_stratified_difference():
    cols = _slots(seed=1, groups=[(30, 40, 2, 0.30, 0.10)])

    result = stratified_ctr_difference(**cols, n_boot=200)

    assert result.value == pytest.approx(_expected_delta(cols, {2: 0.20}), abs=1e-12)
    assert result.verdict == VERDICT_ACTIVE_BETTER and result.ci[0] > 0
    assert (result.n_users, result.n_requests) == (30, 1200)
    assert result.n_explore_slots == 2 * 1200 and result.n_det_slots == 18 * 1200
    assert (result.strata_used, result.strata_dropped) == (20, 0)


def test_mixing_users_with_different_exploration_rates_does_not_fake_a_difference():
    """warm 사용자(탐색 2칸)는 클릭률이 높고 신호 없는 사용자(4칸)는 낮다. 두 집단 모두에서 결정론 칸과 탐색 칸의
    매력이 같다(참 차이 0). 칸을 전부 합쳐 비율을 내면 탐색 쪽에 클릭률 낮은 집단이 더 많이 들어가 차이가
    생긴 것처럼 보이지만, 층을 나눈 지표는 0이다."""
    cols = _slots(seed=2, groups=[(20, 30, 2, 0.30, 0.30), (20, 30, 4, 0.05, 0.05)])

    result = stratified_ctr_difference(**cols, n_boot=200)
    pooled = cols["clicked"][~cols["explored"]].mean() - cols["clicked"][cols["explored"]].mean()

    assert pooled > 0.003  # 층을 나누지 않으면 없는 차이가 보인다
    assert result.value == pytest.approx(0.0, abs=1e-12)
    # 잡음이 없는 로그라 재표집한 값도 전부 0이다(부동소수점 오차만 남는다). 구간이 0으로 줄어든 이 경우의
    # 판정은 그 오차의 부호가 정하므로 보지 않는다 - 판정 규칙은 잡음 있는 로그로 아래에서 본다.
    assert result.ci == pytest.approx((0.0, 0.0), abs=1e-12)
    assert result.strata_used == 40  # (2칸, 위치 20개) + (4칸, 위치 20개)


def test_each_user_group_contributes_in_proportion_to_its_exploration_slots():
    cols = _slots(seed=3, groups=[(15, 20, 2, 0.30, 0.10), (15, 20, 4, 0.10, 0.20)])

    result = stratified_ctr_difference(**cols, n_boot=100)

    assert result.value == pytest.approx(_expected_delta(cols, {2: 0.20, 4: -0.10}), abs=1e-12)


def test_requests_whose_slate_is_not_the_reference_size_are_left_out_and_counted():
    """후보가 12개뿐인 요청은 화면이 12칸이다. 한 위치가 탐색 칸이 될 확률이 2/12로 달라 같은 층에 넣지 않는다."""
    full = _slots(seed=4, groups=[(10, 30, 2, 0.30, 0.10)])
    small = _slots(seed=5, groups=[(10, 30, 2, 0.90, 0.01)], n_eligible=12)
    small["request"] += 10_000
    small["user"] += 10_000
    cols = {k: np.concatenate([full[k], small[k]]) for k in full}

    result = stratified_ctr_difference(**cols, n_boot=100)

    assert set(small["slate_size"]) == {12}
    assert result.requests_dropped_by_slate_size == 300
    assert result.value == pytest.approx(stratified_ctr_difference(**full, n_boot=100).value, abs=1e-12)
    assert result.n_requests == 300


def test_strata_missing_one_arm_are_dropped_and_reported():
    cols = _slots(seed=6, groups=[(5, 20, 2, 0.30, 0.10)])
    # 위치 0의 탐색 칸을 지운다: 그 층에는 결정론 칸만 남는다
    keep = ~(cols["explored"] & (cols["position"] == 0))
    cols = {k: v[keep] for k, v in cols.items()}

    result = stratified_ctr_difference(**cols, n_boot=50)

    assert (result.strata_used, result.strata_dropped) == (19, 1)
    assert result.n_explore_slots == int(cols["explored"].sum())


def test_the_interval_resamples_users_and_the_verdict_follows_the_preregistered_rule():
    better = _slots(seed=7, groups=[(40, 30, 2, 0.30, 0.10)], bernoulli=True)
    worse = _slots(seed=8, groups=[(40, 30, 2, 0.10, 0.30)], bernoulli=True)
    same = _slots(seed=9, groups=[(40, 30, 2, 0.20, 0.20)], bernoulli=True)

    r_better, r_worse, r_same = (stratified_ctr_difference(**c) for c in (better, worse, same))

    assert r_better.verdict == VERDICT_ACTIVE_BETTER and r_better.ci[0] > 0
    assert r_worse.verdict == VERDICT_ACTIVE_WORSE and r_worse.ci[1] < 0
    assert r_same.verdict == VERDICT_UNDECIDED and r_same.ci[0] < 0 < r_same.ci[1]
    for r in (r_better, r_worse, r_same):
        assert r.ci[0] < r.value < r.ci[1]
    assert stratified_ctr_difference(**better) == r_better  # 시드 고정: 같은 입력이면 같은 구간
    # 모든 칸이 한 사용자의 것이면 재표집할 덩어리가 하나뿐이다: 구간이 점으로 줄어든다
    one_user = dict(better, user=np.zeros_like(better["user"]))
    single = stratified_ctr_difference(**one_user, n_boot=50)
    assert single.ci == pytest.approx((single.value, single.value))


def test_no_usable_stratum_gives_nan_and_an_undecided_verdict():
    cols = _slots(seed=10, groups=[(3, 5, 2, 0.3, 0.1)])
    only_det = {k: v[~cols["explored"]] for k, v in cols.items()}

    result = stratified_ctr_difference(**only_det, n_boot=20)

    assert np.isnan(result.value) and result.verdict == VERDICT_UNDECIDED
    assert result.strata_used == 0 and result.n_explore_slots == 0
