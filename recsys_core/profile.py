"""장기 프로필의 증분 상태: 클릭이 올 때마다 한 번 갱신하고, 읽을 때는 감쇠를 다시 계산하지 않는다.

정의(= features._history_cosine_segments와 같은 벡터). 요청 시각 t의 히스토리 벡터는
    v(t) = sum_i 2^(-(t - t_i)/h) * e_i          (h = 반감기, e_i = 클릭한 아이템의 단위 벡터)
이고, 어떤 기준 시각 a에 대해 v(t) = 2^(-(t - a)/h) * S(a), S(a) = sum_i 2^(-(a - t_i)/h) * e_i 로 나뉜다.
코사인은 양수 배에 불변이므로 cos(v(t), x) = cos(S(a), x)다. 그래서 상태로 S(a)와 a만 들고 있으면 된다.

갱신(이벤트 (t_c, e)): a' = max(a, t_c),
    S' = S * 2^(-(a' - a)/h) + e * 2^(-(a' - t_c)/h)
늦게 도착한 이벤트(t_c < a)도 같은 식으로 정확히 들어간다. 누적은 float64다.

카테고리 수는 감쇠 없는 단순 누적이고, 합이 항상 hist_len과 같다(카테고리가 없는 아이템은 0번에 센다).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, Optional, Sequence, Tuple

import numpy as np

DAY = 86400
HALF_LIFE_DAYS = 7.0
# 카테고리 매핑이 없는 아이템을 세는 자리. 실제 카테고리 ID는 1 이상이다.
NO_CATEGORY = 0


def unit_rows(emb: np.ndarray) -> np.ndarray:
    """행마다 L2 정규화한 float32 행렬. 0 벡터는 그대로 둔다.

    하네스 카탈로그(evaluation/recsys/ebnerd/prepare.build_catalog)와 같은 연산이다: float32로 norm을 구해
    float32로 나눈다. 서빙 어댑터·증분 상태·로그 재계산이 전부 이 함수를 거쳐야 같은 단위 벡터를 본다.
    """
    emb = np.array(emb, dtype=np.float32, ndmin=2)
    norms = np.linalg.norm(emb, axis=1, keepdims=True)
    np.divide(emb, norms, out=emb, where=norms > 0)
    return emb


@dataclass
class HistState:
    hist_sum: Optional[np.ndarray] = None   # float64 [d]. 이벤트가 없으면 None
    anchor_s: Optional[int] = None          # 반영된 이벤트 중 가장 늦은 시각(epoch 초) = 감쇠 기준 시각
    hist_len: int = 0
    cat_counts: Dict[int, int] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return self.hist_len == 0 or self.hist_sum is None

    def direction(self) -> Optional[np.ndarray]:
        """히스토리 벡터의 방향(float32 단위 벡터). 이벤트가 없거나 합이 0이면 None."""
        if self.empty:
            return None
        n = float(np.linalg.norm(self.hist_sum))
        if n == 0.0:
            return None
        return (self.hist_sum / n).astype(np.float32)


def _decay(dt_s: float, half_life_days: float) -> float:
    return float(np.exp2(-float(dt_s) / (half_life_days * DAY)))


def apply_event(state: HistState, t_s: int, emb: np.ndarray, category: int = NO_CATEGORY,
                half_life_days: float = HALF_LIFE_DAYS) -> HistState:
    """이벤트 하나(시각 t_s초, 아이템 임베딩, 그 아이템의 카테고리)를 반영한 새 상태. 입력 상태는 바꾸지 않는다."""
    t_s = int(t_s)
    e = unit_rows(emb)[0].astype(np.float64)
    counts = dict(state.cat_counts)
    counts[int(category)] = counts.get(int(category), 0) + 1
    if state.empty:
        return HistState(hist_sum=e, anchor_s=t_s, hist_len=1, cat_counts=counts)
    if e.shape != state.hist_sum.shape:
        raise ValueError(f"임베딩 차원 {e.shape} != 상태 차원 {state.hist_sum.shape}")
    anchor = max(int(state.anchor_s), t_s)
    hist_sum = (np.asarray(state.hist_sum, dtype=np.float64) * _decay(anchor - int(state.anchor_s), half_life_days)
                + e * _decay(anchor - t_s, half_life_days))
    return HistState(hist_sum=hist_sum, anchor_s=anchor, hist_len=state.hist_len + 1, cat_counts=counts)


def rebuild(events: Iterable[Tuple[int, np.ndarray, int]],
            half_life_days: float = HALF_LIFE_DAYS) -> HistState:
    """이벤트 전체에서 정의식으로 한 번에 계산한 상태(증분 갱신을 쓰지 않는다).

    events: (시각 초, 임베딩, 카테고리)의 나열. 순서는 상관없다. 재구축 잡과, 증분 상태가 정의와 같은지
    보는 테스트의 기준값이 이 함수다.
    """
    events = list(events)
    if not events:
        return HistState()
    times = np.asarray([int(t) for t, _, _ in events], dtype=np.int64)
    emb = unit_rows(np.stack([np.asarray(e, dtype=np.float32) for _, e, _ in events])).astype(np.float64)
    anchor = int(times.max())
    w = np.exp2(-(anchor - times).astype(np.float64) / (half_life_days * DAY))
    counts: Dict[int, int] = {}
    for _, _, c in events:
        counts[int(c)] = counts.get(int(c), 0) + 1
    return HistState(hist_sum=(emb * w[:, None]).sum(axis=0), anchor_s=anchor, hist_len=len(events),
                     cat_counts=counts)


def counts_row(cat_counts: Dict[int, int], n_categories: int) -> np.ndarray:
    """{카테고리: 수} -> 길이 n_categories의 정수 배열."""
    row = np.zeros(int(n_categories), dtype=np.int64)
    for c, n in cat_counts.items():
        if not 0 <= int(c) < n_categories:
            raise ValueError(f"카테고리 {c}가 범위 [0, {n_categories}) 밖입니다")
        row[int(c)] = int(n)
    return row


def same_state(a: HistState, b: HistState, cos_tol: float = 1e-9) -> bool:
    """두 상태가 같은 프로필인지: 이벤트 수·기준 시각·카테고리 수가 같고 방향이 cos_tol 안에서 같다."""
    if a.hist_len != b.hist_len or a.cat_counts != b.cat_counts:
        return False
    if a.empty or b.empty:
        return a.empty and b.empty
    if int(a.anchor_s) != int(b.anchor_s):
        return False
    na, nb = float(np.linalg.norm(a.hist_sum)), float(np.linalg.norm(b.hist_sum))
    if na == 0.0 or nb == 0.0:
        return na == nb
    if abs(na - nb) > cos_tol * max(na, nb):
        return False
    return 1.0 - float(np.dot(a.hist_sum, b.hist_sum) / (na * nb)) <= cos_tol


__all__: Sequence[str] = ("DAY", "HALF_LIFE_DAYS", "NO_CATEGORY", "HistState", "apply_event", "counts_row",
                          "rebuild", "same_state", "unit_rows")
