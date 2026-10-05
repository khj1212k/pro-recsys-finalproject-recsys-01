"""첫 온라인 지표(E13)의 계산 - ADR 0025 "사전 등록 1"과 "추가 기록"의 정의를 코드로 고정한다. numpy 전용.

지표: 활성 스코어러가 채운 칸(결정론 칸)과 균등 무작위로 채운 칸(탐색 칸)의 클릭률 차이를
층 s = (그 요청의 탐색 칸 수 m, 위치 p)마다 내고, 층의 탐색 칸 수로 가중 평균한다.

    Δ = Σ_s w_s · [CTR_결정론(s) − CTR_탐색(s)],  w_s = 층 s의 탐색 칸 수 / 전체 탐색 칸 수

층을 나누는 이유: 한 층 안에서는 어느 칸이 탐색 칸이 되는지가 사용자와 무관하게 무작위다. 층을 나누지 않고
칸을 전부 합치면 탐색 칸 수가 많은 요청(개인 신호가 없는 사용자, 4칸)이 탐색 쪽에 더 많이 들어가, 두 클릭률의
차이에 사용자 구성의 차이가 섞인다. 같은 이유로 화면 칸 수가 기준(20)과 다른 요청은 뺀다(한 위치가 탐색 칸이 될
확률 m/S가 달라진다).

입력은 칸 하나가 한 행이다. 분석 대상(정책 버전, 화면이 계획대로 나간 요청, 제외할 계정)을 고르는 일과 클릭을
칸에 붙이는 조인은 호출하는 쪽이 한다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

import numpy as np

VERDICT_ACTIVE_BETTER = "active_slots_get_more_clicks"
VERDICT_UNDECIDED = "undecided"
VERDICT_ACTIVE_WORSE = "active_slots_get_fewer_clicks"


@dataclass(frozen=True)
class StratifiedDifference:
    value: float  # Δ
    ci: Tuple[float, float]  # 사용자 단위 부트스트랩 백분위 구간
    verdict: str
    n_users: int
    n_requests: int
    n_explore_slots: int  # 판정에 들어간 탐색 칸 수
    n_det_slots: int
    strata_used: int
    strata_dropped: int  # 한쪽 칸이 하나도 없어 뺀 층의 수
    requests_dropped_by_slate_size: int


def _delta(counts: np.ndarray) -> float:
    """counts: (층, 4) = [결정론 칸 수, 결정론 클릭, 탐색 칸 수, 탐색 클릭]. 한쪽이 빈 층은 뺀다."""
    n_det, c_det, n_exp, c_exp = counts[:, 0], counts[:, 1], counts[:, 2], counts[:, 3]
    ok = (n_det > 0) & (n_exp > 0)
    if not ok.any():
        return float("nan")
    diff = c_det[ok] / n_det[ok] - c_exp[ok] / n_exp[ok]
    return float(np.sum(diff * n_exp[ok]) / n_exp[ok].sum())


def stratified_ctr_difference(
    request,
    user,
    position,
    explored,
    clicked,
    n_explore,
    slate_size,
    required_slate_size: int = 20,
    n_boot: int = 1000,
    seed: int = 0,
    alpha: float = 0.05,
) -> StratifiedDifference:
    """칸마다 한 행인 배열들. n_explore·slate_size는 그 칸이 속한 요청의 값(요청 로그에서 붙인 것)이다."""
    request = np.asarray(request)
    user = np.asarray(user)
    position = np.asarray(position, dtype=np.int64)
    explored = np.asarray(explored, dtype=bool)
    clicked = np.asarray(clicked, dtype=np.float64)
    n_explore = np.asarray(n_explore, dtype=np.int64)
    slate_size = np.asarray(slate_size, dtype=np.int64)

    keep = (slate_size == required_slate_size) & (n_explore > 0)
    dropped_requests = len(np.unique(request[slate_size != required_slate_size]))
    request, user, position = request[keep], user[keep], position[keep]
    explored, clicked, n_explore = explored[keep], clicked[keep], n_explore[keep]

    # 층 = (탐색 칸 수, 위치)
    stratum_key = n_explore * (required_slate_size + 1) + position
    strata, stratum = np.unique(stratum_key, return_inverse=True)
    users, user_index = np.unique(user, return_inverse=True)
    n_strata, n_users = len(strata), len(users)

    # (사용자, 층, 4): 결정론 칸 수·클릭, 탐색 칸 수·클릭
    counts = np.zeros((n_users, n_strata, 4), dtype=np.float64)
    arm = np.where(explored, 2, 0)
    np.add.at(counts, (user_index, stratum, arm), 1.0)
    np.add.at(counts, (user_index, stratum, arm + 1), clicked)

    total = counts.sum(axis=0)
    value = _delta(total)
    used = (total[:, 0] > 0) & (total[:, 2] > 0)

    lo = hi = float("nan")
    if used.any():
        rng = np.random.default_rng(seed)
        boots = np.empty(n_boot)
        for b in range(n_boot):
            multiplicity = np.bincount(rng.integers(0, n_users, size=n_users), minlength=n_users)
            boots[b] = _delta(np.tensordot(multiplicity, counts, axes=1))
        lo, hi = (float(x) for x in np.nanquantile(boots, [alpha / 2, 1 - alpha / 2]))

    if lo > 0:
        verdict = VERDICT_ACTIVE_BETTER
    elif hi < 0:
        verdict = VERDICT_ACTIVE_WORSE
    else:
        verdict = VERDICT_UNDECIDED
    return StratifiedDifference(
        value=value,
        ci=(lo, hi),
        verdict=verdict,
        n_users=n_users,
        n_requests=len(np.unique(request)),
        n_explore_slots=int(total[used, 2].sum()),
        n_det_slots=int(total[used, 0].sum()),
        strata_used=int(used.sum()),
        strata_dropped=int((~used).sum()),
        requests_dropped_by_slate_size=dropped_requests,
    )
