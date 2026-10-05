"""ε-균등 탐색 슬롯과 그 정확한 propensity (ADR 0025).

한 요청의 화면은 S칸이다(S = min(top_k, 후보 수)). 그중 m칸을 탐색에 쓴다.

메커니즘(이 모듈이 구현하는 것 전부):
1. 탐색 위치 집합 P를 {0..S-1}의 크기 m 부분집합 중에서 균등하게 뽑는다.
2. 탐색 풀 E' = E \\ (결정론 목록의 앞 S-m개)에서 m개를 순서 있게, 비복원으로, 균등하게 뽑아
   P의 작은 위치부터 차례로 넣는다.
3. 결정론 목록의 앞 S-m개는 남은 위치를 결정론 순위(det_rank) 순서대로 채운다.

이 메커니즘에서 "아이템 i가 위치 p에 놓일 확률"은 닫힌 식으로 나온다.
- 탐색 풀의 아이템: (m/S) * (1/|E'|). 위치 p가 탐색 위치일 확률 곱하기 그 칸에 i가 뽑힐 확률.
- 결정론 순위 r인 아이템: p = r + j(앞쪽에 탐색 위치가 j개)일 확률
  C(p, j) * C(S-p-1, m-j) / C(S, m). 앞 p칸에 탐색 위치가 정확히 j개 있고, p는 탐색 위치가 아니고,
  나머지 m-j개가 뒤쪽 S-p-1칸에 있는 경우의 수다(음의 초기하 분포).

그래서 로그의 propensity는 추정값이 아니라 이 식의 값이다. 테스트는 작은 경우를 전부 열거해
식과 구현이 같은지 확인한다(tests/recsys/test_exploration_slots.py).

뽑기(draw_exploration)와 조립(assemble_slate)을 나눈 이유: 조립은 순수 함수라 가능한 뽑기를 전부
넣어 볼 수 있고, 뽑기는 난수만 다루므로 균등성을 따로 검사할 수 있다.
"""
from dataclasses import dataclass
from math import comb
from typing import List, Optional, Sequence, Tuple

import numpy as np

POLICY_EPS_UNIFORM = "eps-uniform-v1"
POLICY_DETERMINISTIC = "deterministic"


def explore_propensity(slate_size: int, n_explore: int, pool_size: int) -> float:
    """탐색 풀의 한 아이템이 한 위치에 놓일 확률. 풀 안의 모든 (아이템, 위치)에서 같다."""
    if n_explore <= 0 or pool_size <= 0 or slate_size <= 0:
        return 0.0
    return (n_explore / slate_size) / pool_size


def det_propensity(det_rank: int, position: int, slate_size: int, n_explore: int) -> float:
    """결정론 순위 det_rank인 아이템이 position에 놓일 확률. 탐색이 없으면(m=0) 제자리에서 1이다."""
    shift = position - det_rank
    n_det = slate_size - n_explore
    if det_rank < 0 or det_rank >= n_det or shift < 0 or shift > n_explore or position >= slate_size:
        return 0.0
    return (
        comb(position, shift)
        * comb(slate_size - position - 1, n_explore - shift)
        / comb(slate_size, n_explore)
    )


@dataclass(frozen=True)
class ExplorationDraw:
    positions: Tuple[int, ...]  # 탐색 위치(오름차순)
    picks: Tuple[int, ...]  # 탐색 풀 안의 인덱스. picks[k]가 positions[k]에 들어간다


@dataclass(frozen=True)
class PlannedSlot:
    news_letter_id: int
    position: int
    explored: bool
    propensity: float
    det_rank: Optional[int]  # 결정론으로 채운 칸만 값이 있다


@dataclass(frozen=True)
class SlatePlan:
    slots: Tuple[PlannedSlot, ...]
    explore_positions: Tuple[int, ...]
    pool_size: int  # |E'|
    policy: str

    @property
    def slate_size(self) -> int:
        return len(self.slots)

    @property
    def n_explore(self) -> int:
        return len(self.explore_positions)

    @property
    def ids(self) -> List[int]:
        return [s.news_letter_id for s in self.slots]


def slate_shape(n_det_ranked: int, n_eligible: int, top_k: int, n_explore: int) -> Tuple[int, int]:
    """(결정론 칸 수, 탐색 칸 수). 후보가 top_k보다 적으면 화면도 그만큼 줄고, 탐색 칸은 화면 칸 수를
    넘지 않는다. 결정론 목록이 모자라도(없어야 하는 경우다) 탐색 풀은 항상 m개 이상 남는다:
    |E'| = |E| - n_det >= S - n_det >= m."""
    slate = max(0, min(top_k, n_eligible))
    m = max(0, min(n_explore, slate))
    n_det = min(n_det_ranked, slate - m)
    return n_det, m


def explore_pool(det_ranked: Sequence[int], eligible: Sequence[int], n_det: int) -> List[int]:
    """E' = E에서 결정론 칸에 들어갈 앞 n_det개를 뺀 것. 순서는 eligible의 순서다."""
    taken = set(det_ranked[:n_det])
    return [i for i in eligible if i not in taken]


def draw_exploration(
    rng: np.random.Generator, slate_size: int, n_explore: int, pool_size: int
) -> ExplorationDraw:
    """위치 부분집합과 순서 있는 아이템 표본을 각각 균등하게 뽑는다."""
    if n_explore <= 0:
        return ExplorationDraw((), ())
    positions = np.sort(rng.choice(slate_size, size=n_explore, replace=False))
    # Generator.choice(replace=False)는 기본값 shuffle=True라 순서까지 균등한 표본을 준다.
    picks = rng.choice(pool_size, size=n_explore, replace=False)
    return ExplorationDraw(tuple(int(p) for p in positions), tuple(int(i) for i in picks))


def assemble_slate(
    det_ranked: Sequence[int], pool: Sequence[int], draw: ExplorationDraw, n_det: int
) -> SlatePlan:
    """뽑힌 결과로 화면을 조립하고 칸마다 propensity를 붙인다(순수 함수)."""
    m = len(draw.positions)
    slate_size = n_det + m
    if m == 0:
        slots = tuple(
            PlannedSlot(int(nid), pos, False, 1.0, pos) for pos, nid in enumerate(det_ranked[:n_det])
        )
        return SlatePlan(slots, (), len(pool), POLICY_DETERMINISTIC)

    explore_at = dict(zip(draw.positions, draw.picks))
    p_explore = explore_propensity(slate_size, m, len(pool))
    slots: List[PlannedSlot] = []
    det_rank = 0
    for pos in range(slate_size):
        if pos in explore_at:
            slots.append(PlannedSlot(int(pool[explore_at[pos]]), pos, True, p_explore, None))
        else:
            slots.append(
                PlannedSlot(
                    int(det_ranked[det_rank]),
                    pos,
                    False,
                    det_propensity(det_rank, pos, slate_size, m),
                    det_rank,
                )
            )
            det_rank += 1
    return SlatePlan(tuple(slots), tuple(draw.positions), len(pool), POLICY_EPS_UNIFORM)


def plan_slate(
    det_ranked: Sequence[int],
    eligible: Sequence[int],
    top_k: int,
    n_explore: int,
    rng: Optional[np.random.Generator],
) -> SlatePlan:
    """결정론 목록(det_ranked, 순위순)과 후보 전체(eligible ⊇ det_ranked)로 한 요청의 화면을 만든다.

    n_explore가 0이거나 rng가 없으면 결정론 목록을 그대로 내고 propensity는 1이다."""
    n_det, m = slate_shape(len(det_ranked), len(eligible), top_k, n_explore if rng is not None else 0)
    pool = explore_pool(det_ranked, eligible, n_det)
    if m == 0:
        return assemble_slate(det_ranked, pool, ExplorationDraw((), ()), n_det)
    return assemble_slate(det_ranked, pool, draw_exploration(rng, n_det + m, m, len(pool)), n_det)
