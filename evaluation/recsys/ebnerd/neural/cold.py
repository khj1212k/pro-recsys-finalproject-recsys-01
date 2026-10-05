"""콜드 조건 정의의 단일 구현: 인기도 raw 0 강제와 유저 히스토리 절단 (ADR 0013 A2 사전 등록 A2.3).

모델은 학습한 그대로 두고 평가 입력만 바꾼다.
- 인기도 0: raw 피처 단계에서 `pop_*` 열을 0으로 둔다. 표준화나 랭크 변환보다 앞이다. 표준화된 공간에서 0을 넣으면
  "평균 인기도"가 되어 다른 조건이 되기 때문이다.
- 절단 k: 요청 시각(profile_cutoff)보다 엄격히 이전인 이벤트 중 최근 k건만 남긴다. `user_log`와 `session_log`를 둘 다
  자른다(sess_*는 session_log에서 계산되므로 user_log만 자르면 세션 정보가 샌다). 후보·라벨·seen 필터와 온보딩 대체물
  (`static_categories`)은 건드리지 않는다.

요청마다 cutoff가 달라 절단 결과를 유저 키 인덱스 하나로 표현할 수 없다. 그래서 요청 번호를 키로 하는 로그를 새로 만들고,
요청의 user·session 키를 요청 번호로 바꾼 `(ctx', req')`를 돌려준다. 이 쌍을 `recsys_core.compute_features`에 그대로
넣으면 히스토리에서 나오는 모든 스칼라 피처가 절단 로그 기준으로 다시 계산된다. 시퀀스 모델도 `ctx'.user_log`에서 같은
이벤트를 읽으면 두 학습기가 같은 입력을 받는다.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np
import pandas as pd

from recsys_core import EventIndex, FeatureContext, Requests, expand_ranges

POP_RAW_COLUMNS = ("pop_clicks_6h", "pop_clicks_24h", "pop_clicks_48h", "pop_inviews_24h", "pop_ctr_24h")
COLD_TRUNCATE_KS = (0, 1, 3, 5)


@dataclass(frozen=True)
class ColdCondition:
    name: str
    pop_zero: bool = False
    truncate_k: Optional[int] = None


def cold_conditions() -> tuple[ColdCondition, ...]:
    """E15 콜드 게이트의 5개 조건. v1.2 사슬(E2)은 여기에 k=10과 절단 없음을 더해 곡선을 낸다."""
    return (ColdCondition("pop0", pop_zero=True),) + tuple(
        ColdCondition(f"k{k}", truncate_k=k) for k in COLD_TRUNCATE_KS)


def pop_mask_raw(feats: pd.DataFrame, value: float = 0.0, rows: Optional[np.ndarray] = None,
                 extra_columns: Sequence[str] = ()) -> pd.DataFrame:
    """인기도 raw 열을 value로 덮은 사본. rows(bool, 행 수)가 있으면 그 행만 덮는다.

    extra_columns: 인기도에서 파생된 추가 열(예: pop_ctr_shrunk_24h). 프레임에 없는 열은 건너뛴다.
    """
    out = feats.copy()
    cols = [c for c in tuple(POP_RAW_COLUMNS) + tuple(extra_columns) if c in out.columns]
    for c in cols:
        col = out[c].to_numpy(dtype=np.float32, copy=True)
        if rows is None:
            col[:] = value
        else:
            col[np.asarray(rows, dtype=bool)] = value
        out[c] = col
    return out


def last_k_bounds(index: EventIndex, key: np.ndarray, cutoff: np.ndarray, k: int) -> tuple[np.ndarray, np.ndarray]:
    """각 요청에서 cutoff보다 엄격히 이전인 이벤트 중 최근 k건의 위치 [lo, hi)."""
    if k < 0:
        raise ValueError("k는 0 이상이어야 합니다")
    lo, hi = index.bounds(key, 0, cutoff)
    return np.maximum(lo, hi - k), hi


def _request_keyed(index: EventIndex, key: np.ndarray, cutoff: np.ndarray, k: int) -> EventIndex:
    lo, hi = last_k_bounds(index, key, cutoff, k)
    rows, pos = expand_ranges(lo, hi)
    return EventIndex(rows, index.time[pos], index.item[pos])


def truncate_logs(ctx: FeatureContext, req: Requests, k: Optional[int]) -> tuple[FeatureContext, Requests]:
    """유저 로그와 세션 로그를 요청마다 최근 k건으로 자른 (ctx', req'). k=None이면 입력을 그대로 돌려준다."""
    if k is None:
        return ctx, req
    n = req.n
    ids = np.arange(n, dtype=np.int64)
    user_log = _request_keyed(ctx.user_log, req.user, req.profile_cutoff, k)
    session_log, session = None, None
    if ctx.session_log is not None and req.session is not None:
        session_log = _request_keyed(ctx.session_log, req.session, req.profile_cutoff, k)
        session = ids
    static = None
    if ctx.static_categories is not None:
        keys, matrix = ctx.static_categories
        keys = np.asarray(keys, dtype=np.int64)
        matrix = np.asarray(matrix, dtype=bool)
        if len(keys) == 0:
            static = (ids, np.zeros((n, ctx.catalog.n_categories), dtype=bool))
        else:
            pos = np.clip(np.searchsorted(keys, req.user), 0, len(keys) - 1)
            has = keys[pos] == req.user
            static = (ids, matrix[pos] & has[:, None])
    new_ctx = dataclasses.replace(ctx, user_log=user_log, session_log=session_log, static_categories=static)
    new_req = Requests(user=ids, time=req.time, cand_ptr=req.cand_ptr, cand_item=req.cand_item, session=session,
                       profile_cutoff=req.profile_cutoff)
    return new_ctx, new_req
