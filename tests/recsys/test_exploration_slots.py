"""탐색 슬롯 메커니즘과 로그 propensity의 정확성 (app/recsys/exploration.py, ADR 0025).

세 층으로 확인한다.
1. 닫힌 식 자체: 위치별 합, 아이템별 합이 확률의 성질을 만족한다.
2. 식 = 구현: 작은 경우의 가능한 뽑기를 전부 열거해, 조립 함수가 칸마다 붙인 propensity가
   그 (아이템, 위치)의 실제 포함 확률과 같은지 본다(난수 없음).
3. 뽑기가 균등하다: 시드 고정 난수로 많은 요청을 뽑아 빈도를 식과 비교한다.
"""
import itertools
from collections import Counter
from fractions import Fraction
from math import comb, perm, sqrt

import numpy as np
import pytest

from app.recsys.exploration import (
    POLICY_DETERMINISTIC,
    POLICY_EPS_UNIFORM,
    ExplorationDraw,
    assemble_slate,
    det_propensity,
    draw_exploration,
    explore_pool,
    explore_propensity,
    plan_slate,
    slate_shape,
)

SHAPES = [(s, m) for s in (1, 2, 3, 5, 8, 20) for m in (0, 1, 2, 4) if m <= s]


@pytest.mark.parametrize("slate,m", SHAPES)
def test_a_deterministic_item_lands_somewhere_with_probability_one(slate, m):
    for rank in range(slate - m):
        total = sum(det_propensity(rank, pos, slate, m) for pos in range(slate))
        assert total == pytest.approx(1.0, abs=1e-12)
        # 앞에 끼어들 수 있는 탐색 칸은 최대 m개다: 제자리보다 앞이나 m칸 넘게 뒤로는 가지 않는다
        assert det_propensity(rank, rank - 1, slate, m) == 0.0
        assert det_propensity(rank, rank + m + 1, slate, m) == 0.0


# 탐색 풀은 항상 m개 이상이다(slate_shape).
@pytest.mark.parametrize(
    "slate,m,pool_size", [(s, m, n) for s, m in SHAPES for n in (1, 3, 27) if n >= m]
)
def test_every_position_is_filled_by_exactly_one_item_in_expectation(slate, m, pool_size):
    for pos in range(slate):
        from_det = sum(det_propensity(rank, pos, slate, m) for rank in range(slate - m))
        from_pool = pool_size * explore_propensity(slate, m, pool_size)
        assert from_det + from_pool == pytest.approx(1.0, abs=1e-12)
        # 위치 p가 탐색 칸일 확률은 m/S다
        assert from_pool == pytest.approx(m / slate if m else 0.0, abs=1e-12)


def test_without_exploration_the_propensity_is_one_at_the_own_rank():
    assert det_propensity(3, 3, 20, 0) == 1.0
    assert det_propensity(3, 4, 20, 0) == 0.0
    assert explore_propensity(20, 0, 30) == 0.0


def _all_draws(slate, m, pool_size):
    for positions in itertools.combinations(range(slate), m):
        for picks in itertools.permutations(range(pool_size), m):
            yield ExplorationDraw(positions, picks)


@pytest.mark.parametrize(
    "n_eligible,top_k,n_explore",
    [
        (6, 4, 1),
        (6, 4, 2),
        (7, 5, 2),
        (9, 5, 3),
        (5, 5, 2),  # 후보가 화면 칸 수와 같다: 탐색 풀 = 밀려난 결정론 아이템
        (3, 5, 2),  # 후보가 화면보다 적다: 화면이 3칸으로 줄어든다
        (2, 5, 4),  # 탐색 칸이 화면 전체
        (6, 4, 0),
    ],
)
def test_the_logged_propensity_is_the_exact_inclusion_probability(n_eligible, top_k, n_explore):
    """가능한 뽑기를 전부 열거한다. 뽑기마다 확률이 같으므로(균등성은 아래 테스트가 본다),
    (아이템, 위치)의 실제 포함 확률 = 그 조합이 나온 뽑기 수 / 전체 뽑기 수다."""
    eligible = list(range(100, 100 + n_eligible))
    det_ranked = eligible[: min(top_k, n_eligible)]
    n_det, m = slate_shape(len(det_ranked), n_eligible, top_k, n_explore)
    pool = explore_pool(det_ranked, eligible, n_det)
    slate = n_det + m

    plans = [assemble_slate(det_ranked, pool, d, n_det) for d in _all_draws(slate, m, len(pool))]
    assert len(plans) == comb(slate, m) * perm(len(pool), m)
    occurrences = Counter((s.news_letter_id, s.position) for plan in plans for s in plan.slots)

    for plan in plans:
        assert plan.slate_size == slate
        assert len(set(plan.ids)) == slate and set(plan.ids) <= set(eligible)
        for slot in plan.slots:
            true_probability = Fraction(occurrences[(slot.news_letter_id, slot.position)], len(plans))
            assert slot.propensity == pytest.approx(float(true_probability), abs=1e-12)

    # 주변 확률: 위치마다 합 1, 결정론 아이템은 항상 보이고, 탐색 풀의 아이템은 m/|E'|로 보인다.
    for pos in range(slate):
        assert sum(n for (_, p), n in occurrences.items() if p == pos) == len(plans)
    shown = Counter()
    for (item, _), n in occurrences.items():
        shown[item] += n
    for item in det_ranked[:n_det]:
        assert shown[item] == len(plans)
    for item in pool:
        assert Fraction(shown[item], len(plans)) == Fraction(m, len(pool))


def test_deterministic_items_keep_their_order_and_fill_the_non_explore_positions():
    eligible = list(range(30))
    det_ranked = list(range(20))
    plan = assemble_slate(det_ranked, explore_pool(det_ranked, eligible, 18),
                          ExplorationDraw((3, 11), (5, 0)), n_det=18)

    assert plan.policy == POLICY_EPS_UNIFORM
    assert plan.explore_positions == (3, 11)
    assert plan.pool_size == 12  # 30 - 18: 밀려난 결정론 18·19위도 탐색 풀에 있다
    assert [s.news_letter_id for s in plan.slots if s.explored] == [23, 18]
    det = [s for s in plan.slots if not s.explored]
    assert [s.news_letter_id for s in det] == list(range(18))
    assert [s.det_rank for s in det] == list(range(18))
    assert all(s.det_rank is None for s in plan.slots if s.explored)
    assert plan.slots[4].news_letter_id == 3 and plan.slots[4].det_rank == 3  # 한 칸 밀렸다


def test_zero_slots_or_no_rng_returns_the_deterministic_list_with_propensity_one():
    eligible = list(range(30))
    det_ranked = list(range(20))
    for plan in (
        plan_slate(det_ranked, eligible, 20, 0, np.random.default_rng(0)),
        plan_slate(det_ranked, eligible, 20, 2, None),
    ):
        assert plan.ids == det_ranked
        assert plan.policy == POLICY_DETERMINISTIC
        assert plan.explore_positions == ()
        assert {s.propensity for s in plan.slots} == {1.0}
        assert [s.det_rank for s in plan.slots] == list(range(20))


def test_the_draw_is_uniform_over_position_sets_and_ordered_picks():
    """S=5, m=2, |E'|=4: 가능한 뽑기는 C(5,2) x 4P2 = 120가지다. 시드 고정 6만 번의 빈도를
    카이제곱으로 본다(자유도 119의 평균 119, 표준편차 15.4; 임계값 200은 약 5 표준편차)."""
    rng = np.random.default_rng(20261006)
    n = 60_000
    counts = Counter()
    for _ in range(n):
        d = draw_exploration(rng, 5, 2, 4)
        counts[(d.positions, d.picks)] += 1

    assert len(counts) == 120
    expected = n / 120
    chi2 = sum((c - expected) ** 2 / expected for c in counts.values())
    assert chi2 < 200


def test_empirical_frequencies_match_the_logged_propensities_over_many_requests():
    """운영 모양(K=20, 탐색 2칸, 후보 45개)에서 시드 고정 6만 요청. 칸마다 로그에 남는 propensity로
    기대 횟수를 만들고, 모든 (아이템, 위치)의 실제 횟수가 6 표준편차 안에 있는지 본다."""
    rng = np.random.default_rng(7)
    eligible = list(range(1000, 1045))
    det_ranked = eligible[:20]
    n = 60_000
    seen = Counter()
    logged = {}
    explore_slots = 0
    for _ in range(n):
        plan = plan_slate(det_ranked, eligible, 20, 2, rng)
        for s in plan.slots:
            key = (s.news_letter_id, s.position)
            seen[key] += 1
            logged.setdefault(key, s.propensity)
            assert logged[key] == s.propensity  # 같은 (아이템, 위치)는 언제나 같은 값으로 기록된다
        explore_slots += plan.n_explore

    assert explore_slots == 2 * n
    for key, p in logged.items():
        sd = sqrt(n * p * (1 - p))
        assert abs(seen[key] - n * p) <= 6 * sd + 1, (key, seen[key], n * p)
    # IPS 항등식: 1/propensity로 가중한 횟수는 요청 수와 같아야 한다(지지되는 조합마다).
    pool_item, det_item = 1030, 1005
    for pos in (0, 9, 19):
        assert seen[(pool_item, pos)] / logged[(pool_item, pos)] == pytest.approx(n, rel=0.25)
    assert seen[(det_item, 5)] / logged[(det_item, 5)] == pytest.approx(n, rel=0.02)
    # 위치별 탐색 비율은 m/S = 0.1
    at_zero = sum(c for (item, pos), c in seen.items() if pos == 0 and item not in det_ranked[:18])
    assert at_zero / n == pytest.approx(0.1, abs=0.006)


def test_cold_shape_uses_four_slots_and_small_pools_shrink_the_slate():
    assert slate_shape(20, 45, 20, 4) == (16, 4)
    assert slate_shape(20, 45, 20, 2) == (18, 2)
    assert slate_shape(3, 3, 20, 4) == (0, 3)  # 후보 3개: 화면 3칸이 전부 위치만 섞인다
    assert slate_shape(0, 0, 20, 2) == (0, 0)

    plan = plan_slate([1, 2, 3], [1, 2, 3], 20, 4, np.random.default_rng(1))
    assert sorted(plan.ids) == [1, 2, 3]
    assert [s.propensity for s in plan.slots] == pytest.approx([1 / 3] * 3)
