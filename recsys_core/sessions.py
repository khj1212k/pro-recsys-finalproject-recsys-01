"""세션 나누기: 같은 유저의 연속한 이벤트 사이가 gap_s초를 넘으면 새 세션이다.

EB-NeRD는 세션 ID를 데이터가 준다. 서비스 로그에는 세션 ID가 없으므로 클릭 시각으로 나눈다.
서빙 어댑터와, 같은 로그에서 피처를 다시 계산하는 오프라인 경로가 이 함수 하나를 쓴다.
"""
from __future__ import annotations

import numpy as np

from .events import EventIndex

SESSION_GAP_S = 30 * 60


def sessionize(user: np.ndarray, time: np.ndarray, gap_s: int = SESSION_GAP_S) -> np.ndarray:
    """이벤트마다 세션 번호(0부터)를 붙여 입력 순서로 돌려준다.

    유저가 바뀌거나 직전 이벤트와의 간격이 gap_s보다 크면 새 세션이다(간격이 정확히 gap_s면 같은 세션).
    번호는 (유저, 시각) 순으로 늘어나므로 한 유저 안에서는 늦은 세션일수록 번호가 크다.
    """
    user = np.asarray(user, dtype=np.int64)
    time = np.asarray(time, dtype=np.int64)
    if len(user) != len(time):
        raise ValueError("user/time 길이가 다릅니다")
    if len(user) == 0:
        return np.zeros(0, dtype=np.int64)
    order = np.lexsort((time, user))
    u, t = user[order], time[order]
    new = np.ones(len(u), dtype=bool)
    new[1:] = (u[1:] != u[:-1]) | ((t[1:] - t[:-1]) > int(gap_s))
    out = np.empty(len(u), dtype=np.int64)
    out[order] = np.cumsum(new) - 1
    return out


def request_sessions(ev_user: np.ndarray, ev_time: np.ndarray, ev_session: np.ndarray,
                     req_user: np.ndarray, req_time: np.ndarray, gap_s: int = SESSION_GAP_S) -> np.ndarray:
    """요청이 속한 세션 번호. 그 유저의 요청 시각보다 엄격히 이전인 마지막 이벤트가 gap_s 안에 있으면
    그 이벤트의 세션이고, 아니면(이벤트가 없거나 간격이 gap_s보다 크면) -1이다(새 세션: 세션 이벤트 없음)."""
    req_user = np.asarray(req_user, dtype=np.int64)
    req_time = np.asarray(req_time, dtype=np.int64)
    out = np.full(len(req_user), -1, dtype=np.int64)
    if len(ev_user) == 0 or len(req_user) == 0:
        return out
    index = EventIndex(ev_user, ev_time, ev_session)
    lo, hi = index.bounds(req_user, 0, req_time)
    ok = hi > lo
    last = hi[ok] - 1
    within = (req_time[ok] - index.time[last]) <= int(gap_s)
    rows = np.flatnonzero(ok)[within]
    out[rows] = index.item[last[within]]
    return out
