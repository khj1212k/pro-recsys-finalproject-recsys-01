"""오프폴리시 추정기(evaluation/recsys/ope.py)를 정답을 아는 합성 로그로 검증한다 (ADR 0025).

로그는 서빙과 같은 탐색 메커니즘(app.recsys.exploration)으로 만들고, 클릭은 위치 기반 모델
P(클릭 | 아이템 i가 위치 p) = θ_p · r_i 로 낸다. θ와 r를 알고 있으므로 어떤 타깃 정책이든
참값(칸당 클릭률)을 식으로 계산할 수 있다.

두 종류의 검사:
- 열거: 작은 화면에서 가능한 뽑기를 전부 로그로 만들고 클릭 대신 클릭 확률을 넣으면, 추정값은
  추정기의 기대값 그 자체다. 난수 없이 "불편인가, 무엇에 대해 불편인가"를 1e-12로 확인한다.
- 표본: 운영 모양(K=20, 탐색 2칸, 후보 45개)에서 시드 고정 로그로 수렴과 오차 크기를 본다.
  이 합성 세계의 수치는 추정기가 식대로 동작하는지를 보는 것이지 실제 서비스의 정확도가 아니다.
"""
import itertools

import numpy as np
import pytest

from app.recsys.exploration import (
    ExplorationDraw,
    assemble_slate,
    explore_pool,
    plan_slate,
    slate_shape,
)
from evaluation.recsys.ope import (
    PositionCtr,
    SlotLog,
    effective_sample_size,
    fit_position_bias_eta,
    ips_slate,
    position_based_slate,
    position_ctr,
    power_law_theta,
    replay_exploration,
    replay_position_based,
    snips_slate,
    target_rank,
)

# ----------------------------------------------------------------------------- 열거(기대값)

ATTRACTION = {10: 0.30, 11: 0.25, 12: 0.20, 13: 0.15, 14: 0.10, 15: 0.08, 16: 0.05}
ELIGIBLE = list(ATTRACTION)
TOP_K = 4
THETA = power_law_theta(TOP_K, 1.0)


def _enumerated_log(n_explore, target_slate):
    """가능한 뽑기마다 요청 하나. 보상은 클릭 확률(기대값)이다."""
    det_ranked = ELIGIBLE[:TOP_K]
    n_det, m = slate_shape(len(det_ranked), len(ELIGIBLE), TOP_K, n_explore)
    pool = explore_pool(det_ranked, ELIGIBLE, n_det)
    rows, target, support = [], {}, set()
    draws = [
        ExplorationDraw(positions, picks)
        for positions in itertools.combinations(range(n_det + m), m)
        for picks in itertools.permutations(range(len(pool)), m)
    ]
    for req, draw in enumerate(draws):
        target[req] = target_slate
        for s in assemble_slate(det_ranked, pool, draw, n_det).slots:
            support.add((s.news_letter_id, s.position))
            rows.append((req, s.position, s.news_letter_id, s.explored, s.propensity,
                         THETA[s.position] * ATTRACTION[s.news_letter_id]))
    return SlotLog.from_columns(*zip(*rows)), target, support, set(pool)


def _slot_value(slate, positions=None):
    positions = range(len(slate)) if positions is None else positions
    return sum(THETA[p] * ATTRACTION[slate[p]] for p in positions)


@pytest.mark.parametrize("n_explore", [1, 2])
def test_full_slate_ips_is_unbiased_for_the_logging_policy_with_exploration_switched_off(n_explore):
    """타깃 = 결정론 목록 그대로. 앞쪽 칸은 결정론 칸이, 밀려난 뒤쪽 순위는 탐색 칸이 지지한다."""
    slate = ELIGIBLE[:TOP_K]
    log, target, _, _ = _enumerated_log(n_explore, slate)
    truth = _slot_value(slate) / TOP_K

    ips, snips = ips_slate(log, target), snips_slate(log, target)

    assert ips.coverage == pytest.approx(1.0, abs=1e-12)
    assert ips.value == pytest.approx(truth, abs=1e-12)
    assert snips.value == pytest.approx(truth, abs=1e-12)


@pytest.mark.parametrize("n_explore", [1, 2])
@pytest.mark.parametrize("slate", [[13, 12, 11, 10], [16, 10, 15, 11], [14, 15, 16, 13], [10, 12, 14, 16]])
def test_ips_recovers_exactly_the_supported_part_and_coverage_reports_the_gap(n_explore, slate):
    """임의의 타깃. 로그 정책이 확률 0을 준 (아이템, 위치)는 복원되지 않는다: IPS는 지지되는 칸의
    값만 세고, coverage가 그 비율을 정확히 알려 주며, SNIPS는 지지되는 칸의 평균 클릭률이다."""
    log, target, support, _ = _enumerated_log(n_explore, slate)
    supported = [p for p in range(TOP_K) if (slate[p], p) in support]

    ips, snips = ips_slate(log, target), snips_slate(log, target)

    assert ips.coverage == pytest.approx(len(supported) / TOP_K, abs=1e-12)
    assert ips.value == pytest.approx(_slot_value(slate, supported) / TOP_K, abs=1e-12)
    if supported:
        assert snips.value == pytest.approx(_slot_value(slate, supported) / len(supported), abs=1e-12)
    else:
        assert np.isnan(snips.value)
    if len(supported) < TOP_K:
        assert ips.value < _slot_value(slate) / TOP_K  # 지지가 모자라면 참값보다 작다


@pytest.mark.parametrize("n_explore", [1, 2])
@pytest.mark.parametrize("slate", [[16, 15, 14, 13], [14, 16, 13, 15], [10, 16, 11, 15]])
def test_exploration_replay_scores_only_target_slots_whose_item_is_in_the_exploration_pool(n_explore, slate):
    log, target, _, pool = _enumerated_log(n_explore, slate)
    in_pool = [p for p in range(TOP_K) if slate[p] in pool]

    est = replay_exploration(log, target)

    assert est.coverage == pytest.approx(len(in_pool) / TOP_K, abs=1e-12)
    assert est.value == pytest.approx(_slot_value(slate, in_pool) / len(in_pool), abs=1e-12)
    assert est.n_slots == int(log.explored.sum())
    # 한 요청 안에서 탐색 칸의 propensity는 상수다: 채점된 칸의 가중치가 모두 같아 ESS = 채점 칸 수
    assert est.ess == pytest.approx(est.n_matched)


@pytest.mark.parametrize("n_explore", [1, 2])
@pytest.mark.parametrize("slate", [[16, 15, 14, 13], [10, 16, 11, 15], [13, 10, 11, 12]])
def test_position_based_replay_is_unbiased_under_the_position_based_model(n_explore, slate):
    """아이템만 맞추고 위치는 θ 비로 옮긴다. 기대값은 타깃 화면 중 탐색 풀에 있는 아이템들의
    (타깃 위치에서의) 평균 클릭률이다."""
    log, target, _, pool = _enumerated_log(n_explore, slate)
    in_pool = [p for p in range(TOP_K) if slate[p] in pool]

    est = replay_position_based(log, target, THETA)

    assert est.coverage == pytest.approx(len(in_pool) / TOP_K, abs=1e-12)
    assert est.value == pytest.approx(_slot_value(slate, in_pool) / len(in_pool), abs=1e-12)
    # 위치까지 맞추는 replay보다 채점되는 칸이 화면 칸 수 배만큼 많다
    assert est.n_matched == TOP_K * replay_exploration(log, target).n_matched


@pytest.mark.parametrize("n_explore", [1, 2])
@pytest.mark.parametrize("slate", [[10, 11, 12, 13], [13, 12, 11, 10], [16, 10, 15, 11], [14, 15, 16, 13]])
def test_the_full_slate_position_based_estimator_supports_any_target_inside_the_candidate_set(n_explore, slate):
    """결정론 칸의 아이템(항상 보임)과 탐색 풀의 아이템을 함께 쓰면 후보 집합 안의 어떤 화면이든 지지된다.
    위치 기반 모델이 참이고 θ가 맞을 때 기대값은 타깃 화면 전체의 칸당 클릭률이다."""
    log, target, _, _ = _enumerated_log(n_explore, slate)

    est = position_based_slate(log, target, THETA)

    assert est.coverage == pytest.approx(1.0, abs=1e-12)
    assert est.value == pytest.approx(_slot_value(slate) / TOP_K, abs=1e-12)
    # 결정론 칸의 기여는 θ를 믿는다: θ가 틀리면 지지가 완전해도 값이 틀린다
    wrong = position_based_slate(log, target, np.ones(TOP_K)).value
    if slate != ELIGIBLE[:TOP_K]:
        assert wrong != pytest.approx(_slot_value(slate) / TOP_K, rel=1e-3)


def test_a_wrong_position_bias_makes_position_based_replay_wrong():
    """보정은 θ를 믿는다. θ를 평평하게 주면(위치 편향 무시) 아래쪽에 좋은 아이템을 둔 타깃이 과대평가된다."""
    slate = [16, 15, 14, 13]  # 탐색 풀 안에서 매력이 낮은 것부터
    log, target, _, _ = _enumerated_log(2, slate)
    truth = _slot_value(slate) / TOP_K

    assert replay_position_based(log, target, THETA).value == pytest.approx(truth, abs=1e-12)
    assert replay_position_based(log, target, np.ones(TOP_K)).value > 1.2 * truth


# ----------------------------------------------------------------------------- 표본(운영 모양)

K, N_ITEMS, ETA = 20, 45, 1.0


def _pool_only(attraction, det_ranked, n_explore):
    """탐색 풀(결정론 앞 K-m개 밖)에서 매력 순으로 K개: 탐색 칸만으로 완전히 지지되는 타깃."""
    taken = set(det_ranked[: K - n_explore])
    rest = [i for i in np.argsort(-attraction) if i not in taken]
    return [int(i) for i in rest[:K]]


def _simulate(seed, n_requests, n_explore=2, target="pool_only", bernoulli=True):
    """bernoulli=False면 보상에 클릭 확률을 그대로 넣는다(클릭 잡음 없이 매칭 잡음만 남는다)."""
    rng = np.random.default_rng(seed)
    theta = power_law_theta(K, ETA)
    eligible = list(range(N_ITEMS))
    rows, slates = [], {}
    truth_clicks = 0.0
    for req in range(n_requests):
        attraction = rng.uniform(0.02, 0.4, N_ITEMS)
        det_ranked = [int(i) for i in np.argsort(-(attraction + rng.normal(0, 0.1, N_ITEMS)))[:K]]
        slate = _pool_only(attraction, det_ranked, n_explore) if target == "pool_only" else det_ranked
        slates[req] = slate
        truth_clicks += float(theta @ attraction[slate])
        uniform = rng.random(K)
        for s in plan_slate(det_ranked, eligible, K, n_explore, rng).slots:
            p_click = theta[s.position] * attraction[s.news_letter_id]
            reward = float(uniform[s.position] < p_click) if bernoulli else p_click
            rows.append((req, s.position, s.news_letter_id, s.explored, s.propensity, reward))
    return SlotLog.from_columns(*zip(*rows)), slates, truth_clicks / (n_requests * K)


@pytest.fixture(scope="module")
def click_world():
    return _simulate(seed=11, n_requests=20_000)


def test_position_bias_is_recovered_from_the_exploration_slots(click_world):
    log, _, _ = click_world

    counts = position_ctr(log, K)
    eta = fit_position_bias_eta(counts)

    assert counts.slots.sum() == 2 * 20_000
    assert counts.slots.min() > 1_500  # 위치가 균등하게 뽑혔다(기대 2,000)
    assert eta == pytest.approx(ETA, abs=0.1)
    # 비모수 추정도 앞쪽 위치에서는 식과 맞는다: CTR(1)/CTR(0) ~ 1/2
    assert counts.ctr[1] / counts.ctr[0] == pytest.approx(0.5, abs=0.1)


def test_in_the_serving_shape_both_replays_converge_to_the_true_value_of_a_supported_target():
    """클릭 잡음을 뺀 로그(보상 = 클릭 확률) 8천 요청. 남는 오차는 "어느 칸이 채점됐는가"뿐이다."""
    log, slates, truth = _simulate(seed=3, n_requests=8_000, bernoulli=False)

    exact = replay_exploration(log, slates)
    position_based = replay_position_based(log, slates, power_law_theta(K, ETA))

    assert position_based.coverage == pytest.approx(1.0, abs=0.03)
    assert position_based.value == pytest.approx(truth, rel=0.02)
    assert exact.value == pytest.approx(truth, rel=0.10)
    # 채점되는 탐색 칸의 비율: 위치까지 맞추면 1/|E'| = 1/27, 아이템만 맞추면 K/|E'| = 20/27
    assert exact.ess_ratio == pytest.approx(1 / 27, rel=0.15)
    assert position_based.ess_ratio == pytest.approx(20 / 27, rel=0.05)


def test_over_seeded_worlds_the_replays_are_unbiased_and_their_intervals_cover_the_truth():
    """베르누이 클릭, 요청 2천 개짜리 세계 24개. 한 세계의 추정값은 잡음이 크므로(아래 폭) 한 시드의
    값이 아니라 시드 평균이 참값에서 3 표준오차 안인지, 부트스트랩 구간이 참값을 덮는 비율을 본다."""
    ratios, covered, widths = [], [], []
    for seed in range(24):
        log, slates, truth = _simulate(seed=500 + seed, n_requests=2_000)
        row_ratio, row_cover, row_width = [], [], []
        for est in (replay_exploration(log, slates), replay_position_based(log, slates, power_law_theta(K, ETA))):
            lo, hi = est.bootstrap_ci(n_boot=300, seed=0)
            row_ratio.append(est.value / truth)
            row_cover.append(lo < truth < hi)
            row_width.append((hi - lo) / truth)
        ratios.append(row_ratio)
        covered.append(row_cover)
        widths.append(row_width)
    ratios, covered, widths = np.array(ratios), np.array(covered), np.array(widths)

    standard_error = ratios.std(axis=0, ddof=1) / np.sqrt(len(ratios))
    assert np.all(np.abs(ratios.mean(axis=0) - 1.0) < 3 * standard_error)
    assert np.all(covered.mean(axis=0) >= 0.85)
    # 같은 로그에서 아이템 단위 매칭의 구간이 위치까지 맞추는 replay보다 뚜렷이 좁다
    assert widths[:, 1].mean() < 0.7 * widths[:, 0].mean()
    # 요청 2천 개로는 큰 차이만 가른다: 가장 좁은 추정기도 95% 구간 폭이 참값의 절반을 넘는다
    assert widths[:, 1].mean() > 0.5


def test_full_slate_snips_estimates_the_deterministic_list_and_exploration_replay_alone_cannot():
    """타깃 = 탐색을 끈 결정론 목록. 화면 전체를 쓰면 지지가 완전하고, 탐색 칸만 쓰면 밀려난 뒤쪽
    두 순위(가장 덜 보이는 두 칸)만 지지된다."""
    log, slates, truth = _simulate(seed=5, n_requests=6_000, target="deterministic", bernoulli=False)

    snips = snips_slate(log, slates)
    explore_only = replay_exploration(log, slates)

    assert snips.coverage == pytest.approx(1.0, abs=0.05)
    assert snips.value == pytest.approx(truth, rel=0.02)
    assert explore_only.coverage == pytest.approx(2 / K, abs=0.03)
    assert explore_only.value < 0.5 * truth  # 화면 전체의 값이 아니라 맨 아래 두 칸의 값이다


# ----------------------------------------------------------------------------- 부품


def test_effective_sample_size_is_kish():
    assert effective_sample_size([1, 1, 1, 1]) == pytest.approx(4.0)
    assert effective_sample_size([5, 5, 0, 0]) == pytest.approx(2.0)
    assert effective_sample_size([100, 1, 1, 1]) == pytest.approx(103**2 / 10003)
    assert effective_sample_size([]) == 0.0
    assert effective_sample_size([0, 0]) == 0.0


def test_target_rank_is_minus_one_outside_the_target_slate_and_for_unknown_requests():
    log = SlotLog.from_columns([1, 1, 2], [0, 1, 0], [7, 8, 7], [False, True, True], [1.0, 0.1, 0.1], [0, 1, 0])

    assert target_rank(log, {1: [8, 7]}).tolist() == [1, 0, -1]


def test_rows_without_a_valid_propensity_are_rejected_instead_of_silently_weighted():
    for bad in (0.0, float("nan"), 1.5, -0.1):
        with pytest.raises(ValueError, match="propensity"):
            SlotLog.from_columns([1], [0], [7], [True], [bad], [1])
    with pytest.raises(ValueError, match="rows"):
        SlotLog.from_columns([1, 2], [0], [7], [True], [0.5], [1])


def test_bootstrap_resamples_clusters_and_is_reproducible(click_world):
    log, slates, _ = click_world
    est = replay_position_based(log, slates, power_law_theta(K, ETA))

    assert est.bootstrap_ci(n_boot=200, seed=3) == est.bootstrap_ci(n_boot=200, seed=3)
    lo, hi = est.bootstrap_ci(n_boot=200, seed=3)
    assert lo < est.value < hi
    # 모든 요청이 한 사용자의 것이면 재표집해도 같은 덩어리 하나뿐이다
    one_user = np.zeros(len(est.num_by_request), dtype=int)
    assert est.bootstrap_ci(n_boot=50, seed=3, cluster_of_request=one_user) == pytest.approx(
        (est.value, est.value)
    )
    # 사용자 100명으로 묶으면 덩어리 수가 줄어 구간이 요청 단위보다 좁아지지 않는다(여기서는 독립이라 비슷하다)
    users = np.arange(len(est.num_by_request)) % 100
    u_lo, u_hi = est.bootstrap_ci(n_boot=200, seed=3, cluster_of_request=users)
    assert u_lo < est.value < u_hi


def test_eta_fit_needs_clicks_and_handles_positions_without_slots():
    assert np.isnan(fit_position_bias_eta(PositionCtr(clicks=np.zeros(5), slots=np.full(5, 10.0))))
    theta = power_law_theta(6, 0.7)
    slots = np.array([4000.0, 4000.0, 0.0, 4000.0, 4000.0, 4000.0])
    assert fit_position_bias_eta(PositionCtr(clicks=slots * 0.2 * theta, slots=slots)) == pytest.approx(0.7, abs=0.002)
