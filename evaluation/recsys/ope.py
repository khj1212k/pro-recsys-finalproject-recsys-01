"""탐색 슬롯 로그로 다른 정책을 평가하는 오프폴리시 추정기 (ADR 0025) - numpy 전용, DB·서빙 코드 비의존.

입력은 "칸(slot) 로그"다: 요청마다 화면에 나간 칸 하나가 한 행이고, 행마다
(요청, 위치, 아이템, 탐색 여부, propensity, 보상)을 가진다. propensity는 로그 정책에서
"그 아이템이 그 위치에 놓일 확률"이고 서빙이 닫힌 식으로 기록한 값이다(app/recsys/exploration.py).
타깃 정책은 요청마다의 화면(위치순 아이템 목록)으로 준다.

가정과 추정 대상
- 보상은 칸 단위다: 한 칸의 클릭 확률은 그 칸의 (아이템, 위치)와 문맥에만 달려 있고 같은 화면의
  다른 칸에는 달려 있지 않다(item-position 모델). 화면 전체가 상호작용하는 효과는 다루지 않는다.
- 추정 대상은 타깃 정책의 **칸당 클릭률**이다. 로그 정책이 0의 확률을 준 (아이템, 위치)는 어떤
  가중치로도 복원되지 않는다. 그래서 모든 추정은 "로그가 지지하는 타깃 칸"에 대한 값이고,
  지지 비율의 추정치를 coverage로 함께 낸다. coverage가 1보다 뚜렷이 작으면 그 추정은 타깃 화면의
  일부만 본 것이다.

추정기
- replay_exploration (1차): 탐색 칸만 쓴다. 타깃이 같은 위치에 같은 아이템을 둔 칸만 채점한다
  (Li et al. 2011의 replay). 탐색 칸의 propensity는 한 요청 안에서 상수라 가중치가 고르다.
- snips_slate (2차): 화면 전체를 쓴다. 결정론 칸은 타깃과 (아이템, 위치)가 같을 때만 기여한다.
- ips_slate: 정규화하지 않은 값. 지지가 완전할 때만 불편이고, 아니면 지지되지 않는 칸만큼 작다.
- replay_position_based: 탐색 칸에서 위치를 맞추지 않고 "타깃 화면 안의 아이템인가"만 본 뒤,
  위치 편향 비 θ[타깃 위치]/θ[기록 위치]로 보정한다. 위치까지 맞추는 replay보다 표본이 화면 칸 수
  배만큼 많지만, 클릭 = 위치 검토 확률 x 아이템 매력(position-based model)이라는 가정이 더 든다.
- position_based_slate: 위 보정을 화면 전체에 쓴다. 후보 집합 안의 어떤 타깃이든 지지되지만, 결정론
  칸의 기여는 무작위화가 아니라 위치 편향 모델에 기댄다.
- position_ctr / fit_position_bias_eta: 탐색 칸은 위치가 균등 무작위이고 아이템이 위치와 독립이라,
  위치별 클릭률의 비가 곧 위치 편향의 비다.
- effective_sample_size: Kish의 (Σw)² / Σw².

신뢰구간은 요청(또는 사용자) 단위 재표집으로 낸다: 한 요청의 칸들은 독립이 아니다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Sequence, Tuple

import numpy as np


@dataclass(frozen=True)
class SlotLog:
    request: np.ndarray  # (n,) 같은 요청의 칸은 같은 정수
    position: np.ndarray  # (n,) 0부터
    item: np.ndarray  # (n,)
    explored: np.ndarray  # (n,) bool
    propensity: np.ndarray  # (n,) 로그 정책에서 이 아이템이 이 위치에 놓일 확률
    reward: np.ndarray  # (n,) 클릭 0/1(또는 기대값)

    def __post_init__(self):
        n = len(self.request)
        for name in ("position", "item", "explored", "propensity", "reward"):
            if len(getattr(self, name)) != n:
                raise ValueError(f"{name} has {len(getattr(self, name))} rows, request has {n}")
        p = np.asarray(self.propensity, dtype=np.float64)
        if n and (not np.all(np.isfinite(p)) or p.min() <= 0.0 or p.max() > 1.0):
            # propensity가 없는 행(폴백 응답, 화면이 계획과 달라진 요청)은 넣기 전에 걸러야 한다.
            raise ValueError("propensity must be in (0, 1] for every row; drop rows without one first")

    @classmethod
    def from_columns(cls, request, position, item, explored, propensity, reward) -> "SlotLog":
        return cls(
            np.asarray(request, dtype=np.int64),
            np.asarray(position, dtype=np.int64),
            np.asarray(item, dtype=np.int64),
            np.asarray(explored, dtype=bool),
            np.asarray(propensity, dtype=np.float64),
            np.asarray(reward, dtype=np.float64),
        )

    def __len__(self) -> int:
        return len(self.request)


Target = Mapping[int, Sequence[int]]


def target_rank(log: SlotLog, target: Target) -> np.ndarray:
    """칸마다 "기록된 아이템이 타깃 화면에서 몇 번째인가". 타깃 화면에 없으면 -1."""
    rank = np.full(len(log), -1, dtype=np.int64)
    cache: dict = {}
    for k in range(len(log)):
        req = int(log.request[k])
        index = cache.get(req)
        if index is None:
            index = {int(item): pos for pos, item in enumerate(target.get(req, ()))}
            cache[req] = index
        rank[k] = index.get(int(log.item[k]), -1)
    return rank


def effective_sample_size(weights) -> float:
    w = np.asarray(weights, dtype=np.float64)
    total = w.sum()
    if total <= 0.0:
        return 0.0
    return float(total * total / np.square(w).sum())


@dataclass(frozen=True)
class Estimate:
    """value = Σ num / Σ den. 요청 단위 합(num_by_request, den_by_request)을 들고 있어 재표집이 싸다."""

    value: float
    n_slots: int  # 이 추정기가 본 로그 칸 수(탐색 한정이면 탐색 칸 수)
    n_matched: int
    ess: float
    coverage: float  # 타깃 칸 중 로그가 지지하는 비율의 추정(Σw / 타깃 칸 수)
    num_by_request: np.ndarray
    den_by_request: np.ndarray

    @property
    def ess_ratio(self) -> float:
        return self.ess / self.n_slots if self.n_slots else 0.0

    def bootstrap_ci(
        self,
        n_boot: int = 1000,
        seed: int = 0,
        alpha: float = 0.05,
        cluster_of_request: Optional[np.ndarray] = None,
    ) -> Tuple[float, float]:
        """요청 단위(cluster_of_request를 주면 그 단위, 예: 사용자) 백분위 부트스트랩 구간."""
        num, den = self.num_by_request, self.den_by_request
        if cluster_of_request is not None:
            _, inv = np.unique(np.asarray(cluster_of_request), return_inverse=True)
            num = np.bincount(inv, weights=num)
            den = np.bincount(inv, weights=den)
        if len(num) == 0:
            return float("nan"), float("nan")
        rng = np.random.default_rng(seed)
        idx = rng.integers(0, len(num), size=(n_boot, len(num)))
        d = den[idx].sum(axis=1)
        with np.errstate(invalid="ignore", divide="ignore"):
            values = np.where(d > 0, num[idx].sum(axis=1) / d, np.nan)
        lo, hi = np.nanquantile(values, [alpha / 2, 1 - alpha / 2])
        return float(lo), float(hi)


def _requests(log: SlotLog) -> Tuple[np.ndarray, np.ndarray]:
    return np.unique(log.request, return_inverse=True)


def _target_slots(requests: np.ndarray, target: Target) -> np.ndarray:
    return np.asarray([len(target.get(int(r), ())) for r in requests], dtype=np.float64)


def _ratio(num: np.ndarray, den: np.ndarray) -> float:
    total = den.sum()
    return float(num.sum() / total) if total > 0 else float("nan")


def _weighted(
    log: SlotLog,
    target: Target,
    scope: np.ndarray,
    use: np.ndarray,
    weight: np.ndarray,
    gain: np.ndarray,
    normalize: bool,
) -> Estimate:
    """scope = 이 추정기가 보는 칸, use = 그중 채점되는 칸. 값은 Σ w·gain·보상 / (Σ w 또는 타깃 칸 수)."""
    requests, inv = _requests(log)
    w = np.where(use, weight, 0.0)
    num = np.bincount(inv, weights=w * gain * log.reward, minlength=len(requests))
    weight_sum = np.bincount(inv, weights=w, minlength=len(requests))
    slots = _target_slots(requests, target)
    den = weight_sum if normalize else slots
    total_slots = slots.sum()
    return Estimate(
        value=_ratio(num, den),
        n_slots=int(scope.sum()),
        n_matched=int(use.sum()),
        ess=effective_sample_size(w),
        coverage=float(weight_sum.sum() / total_slots) if total_slots > 0 else float("nan"),
        num_by_request=num,
        den_by_request=den,
    )


def _exact(log: SlotLog, target: Target, scope: np.ndarray, normalize: bool) -> Estimate:
    matched = scope & (target_rank(log, target) == log.position)
    return _weighted(log, target, scope, matched, 1.0 / log.propensity, np.ones(len(log)), normalize)


def replay_exploration(log: SlotLog, target: Target) -> Estimate:
    """1차 추정기. 탐색 칸 중 타깃이 같은 위치에 같은 아이템을 둔 칸의 (가중) 평균 클릭률.

    탐색 칸의 propensity가 모든 요청에서 같으면(후보 수가 같으면) 가중치가 상수라 단순 평균과 같다."""
    return _exact(log, target, np.asarray(log.explored, dtype=bool), normalize=True)


def snips_slate(log: SlotLog, target: Target) -> Estimate:
    """2차 추정기. 화면 전체의 칸에서 타깃과 (아이템, 위치)가 같은 칸을 1/propensity로 가중해 자기정규화한다."""
    return _exact(log, target, np.ones(len(log), dtype=bool), normalize=True)


def ips_slate(log: SlotLog, target: Target) -> Estimate:
    """정규화하지 않은 IPS: Σ w·클릭 / 타깃 칸 수. 지지가 완전하면 불편이고, 아니면 (1 - coverage)만큼의
    타깃 칸을 0으로 센 값이다."""
    return _exact(log, target, np.ones(len(log), dtype=bool), normalize=False)


def _position_based(
    log: SlotLog, target: Target, theta, scope: np.ndarray, weight: np.ndarray
) -> Estimate:
    theta = np.asarray(theta, dtype=np.float64)
    rank = target_rank(log, target)
    in_range = (rank >= 0) & (rank < len(theta)) & (log.position < len(theta))
    last = max(len(theta) - 1, 0)
    theta_logged = np.where(in_range, theta[np.clip(log.position, 0, last)], 0.0)
    theta_target = np.where(in_range, theta[np.clip(rank, 0, last)], 0.0)
    use = scope & in_range & (theta_logged > 0)
    gain = np.where(use, theta_target / np.where(theta_logged > 0, theta_logged, 1.0), 0.0)
    return _weighted(log, target, scope, use, weight, gain, normalize=True)


def _slate_size(log: SlotLog, slate_size: Optional[np.ndarray]) -> np.ndarray:
    if slate_size is None:
        requests, inv = _requests(log)
        slate_size = np.bincount(inv, minlength=len(requests))[inv]
    return np.asarray(slate_size, dtype=np.float64)


def replay_position_based(
    log: SlotLog, target: Target, theta, slate_size: Optional[np.ndarray] = None
) -> Estimate:
    """탐색 칸 + 아이템 단위 매칭 + 위치 편향 보정.

    기록된 아이템이 타깃 화면의 q번째에 있으면, 기록 위치 p에서 본 클릭을 θ[q]/θ[p]로 옮겨 센다.
    가중치는 1/(그 아이템이 탐색 칸 어디에든 놓일 확률) = 1/(화면 칸 수 x propensity)다.
    theta는 위치별 검토 확률의 상대값(척도 무관)이고 position_ctr나 power_law_theta로 만든다.
    θ[p]가 0인 위치에서 기록된 칸은 버린다(그 위치에서는 클릭이 관측되지 않았다).
    slate_size(칸마다 그 요청의 화면 칸 수)를 주지 않으면 로그에 요청의 모든 칸이 들어 있다고 보고 센다.

    지지: 타깃 화면의 아이템 중 탐색 풀에 있는 것만. 로그 정책의 결정론 칸에 들어간 아이템은 탐색 칸에
    나오지 않으므로, 로그 정책과 많이 겹치는 타깃에서는 coverage가 낮다."""
    explored = np.asarray(log.explored, dtype=bool)
    weight = 1.0 / (_slate_size(log, slate_size) * log.propensity)
    return _position_based(log, target, theta, explored, weight)


def position_based_slate(
    log: SlotLog, target: Target, theta, slate_size: Optional[np.ndarray] = None
) -> Estimate:
    """화면 전체 + 아이템 단위 매칭 + 위치 편향 보정(클릭 모델 기반 추정).

    결정론 칸의 아이템은 확률 1로 화면에 나오므로 가중치 1, 탐색 칸의 아이템은 replay_position_based와 같은
    가중치를 준다. 후보 집합 안의 모든 아이템이 어느 쪽으로든 화면에 나올 수 있으므로, 타깃 화면이 후보
    집합 안에 있으면 지지가 완전하다. 대신 결정론 칸의 위치는 무작위가 아니다: 그 칸의 기여는 위치
    편향 모델(클릭 = θ[위치] x 아이템 매력)과 θ가 맞을 때만 옳다."""
    explored = np.asarray(log.explored, dtype=bool)
    weight = np.where(explored, 1.0 / (_slate_size(log, slate_size) * log.propensity), 1.0)
    return _position_based(log, target, theta, np.ones(len(log), dtype=bool), weight)


@dataclass(frozen=True)
class PositionCtr:
    clicks: np.ndarray
    slots: np.ndarray

    @property
    def ctr(self) -> np.ndarray:
        with np.errstate(divide="ignore", invalid="ignore"):
            return np.where(self.slots > 0, self.clicks / np.maximum(self.slots, 1), 0.0)


def position_ctr(log: SlotLog, n_positions: Optional[int] = None) -> PositionCtr:
    """탐색 칸의 위치별 클릭 수와 칸 수. 탐색 칸은 위치와 아이템이 독립이라 ctr의 비가 위치 편향의 비다."""
    explored = np.asarray(log.explored, dtype=bool)
    pos = log.position[explored]
    n = int(n_positions if n_positions is not None else (pos.max() + 1 if len(pos) else 0))
    return PositionCtr(
        clicks=np.bincount(pos, weights=log.reward[explored], minlength=n)[:n],
        slots=np.bincount(pos, minlength=n)[:n].astype(np.float64),
    )


def power_law_theta(n_positions: int, eta: float) -> np.ndarray:
    return (np.arange(n_positions) + 1.0) ** (-float(eta))


def fit_position_bias_eta(counts: PositionCtr, eta_grid: Optional[np.ndarray] = None) -> float:
    """θ_p ∝ (p+1)^(-η)를 위치별 (클릭 수, 칸 수)에 포아송 프로파일 가능도로 맞춘다(격자 탐색).

    η를 고정하면 척도의 최우추정이 닫힌 식(Σ클릭 / Σ 칸 수·θ_p)이라 η 하나만 훑으면 된다."""
    grid = np.linspace(0.0, 3.0, 3001) if eta_grid is None else np.asarray(eta_grid, dtype=np.float64)
    clicks, slots = counts.clicks, counts.slots
    if clicks.sum() <= 0:
        return float("nan")
    log_rank = np.log(np.arange(len(clicks)) + 1.0)
    theta = np.exp(-np.outer(grid, log_rank))  # (격자, 위치)
    exposure = theta @ slots
    scale = clicks.sum() / exposure
    loglik = clicks.sum() * np.log(scale) - grid * (clicks @ log_rank) - scale * exposure
    return float(grid[int(np.argmax(loglik))])
