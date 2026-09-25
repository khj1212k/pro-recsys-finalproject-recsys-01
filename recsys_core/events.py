"""(키, 시각) 정렬 이벤트 인덱스.

유저 클릭 로그, 세션 클릭 로그, 아이템별 클릭/노출 로그를 전부 같은 자료구조로 다룬다.
모든 조회는 반열린 구간 [t_lo, t_hi)이라 "t 시점 피처는 t보다 엄격히 이전 이벤트만 쓴다"는
point-in-time 규칙이 인덱스 수준에서 강제된다.
"""
from __future__ import annotations

import numpy as np

_REL_MAX = (1 << 32) - 1


class EventIndex:
    """key(>=0 정수)별로 시간순 정렬된 (time, item) 이벤트 배열.

    time은 epoch 초(int64). 조회는 (key << 32 | time - t0) 결합키에 대한
    np.searchsorted 두 번이라 요청 수백만 건도 벡터 연산으로 처리된다.
    """

    def __init__(self, key, time, item, dedupe: bool = False):
        key = np.asarray(key, dtype=np.int64)
        time = np.asarray(time, dtype=np.int64)
        item = np.asarray(item, dtype=np.int64)
        if not (len(key) == len(time) == len(item)):
            raise ValueError("key/time/item 길이가 다릅니다")
        if len(key) and (key.min() < 0 or key.max() > np.iinfo(np.int32).max):
            raise ValueError("key는 0 이상 2^31 미만이어야 합니다")
        order = np.lexsort((item, time, key))
        key, time, item = key[order], time[order], item[order]
        if dedupe and len(key):
            keep = np.ones(len(key), dtype=bool)
            keep[1:] = (np.diff(key) != 0) | (np.diff(time) != 0) | (np.diff(item) != 0)
            key, time, item = key[keep], time[keep], item[keep]
        self.key = key
        self.time = time
        self.item = item
        self._t0 = int(time.min()) if len(time) else 0
        if len(time) and int(time.max()) - self._t0 > _REL_MAX:
            raise ValueError("시간 범위가 2^32초를 넘습니다")
        self._comb = self._combine(key, time)

    def __len__(self) -> int:
        return len(self.key)

    def _combine(self, key: np.ndarray, time: np.ndarray) -> np.ndarray:
        # t0보다 이른 조회 시각은 0으로 잘라도 의미가 같다(그 키의 첫 이벤트부터 포함/제외).
        rel = np.clip(np.asarray(time, dtype=np.int64) - self._t0, 0, _REL_MAX)
        return (np.asarray(key, dtype=np.int64) << 32) | rel

    def bounds(self, key, t_lo, t_hi) -> tuple[np.ndarray, np.ndarray]:
        """각 요청의 [t_lo, t_hi) 구간 이벤트 위치 [lo, hi). key<0이면 빈 구간."""
        key = np.asarray(key, dtype=np.int64)
        t_lo = np.broadcast_to(np.asarray(t_lo, dtype=np.int64), key.shape)
        t_hi = np.broadcast_to(np.asarray(t_hi, dtype=np.int64), key.shape)
        valid = key >= 0
        safe_key = np.where(valid, key, 0)
        lo = np.searchsorted(self._comb, self._combine(safe_key, t_lo), side="left")
        hi = np.searchsorted(self._comb, self._combine(safe_key, t_hi), side="left")
        hi = np.maximum(hi, lo)
        lo = np.where(valid, lo, 0)
        hi = np.where(valid, hi, 0)
        return lo, hi

    def count(self, key, t_lo, t_hi) -> np.ndarray:
        lo, hi = self.bounds(key, t_lo, t_hi)
        return hi - lo

    def last_time_before(self, key, t) -> np.ndarray:
        """t보다 엄격히 이전의 마지막 이벤트 시각. 없으면 -1."""
        key = np.asarray(key, dtype=np.int64)
        out = np.full(len(key), -1, dtype=np.int64)
        lo, hi = self.bounds(key, 0, t)
        ok = hi > lo
        out[ok] = self.time[hi[ok] - 1]
        return out


def expand_ranges(lo: np.ndarray, hi: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """[lo_i, hi_i) 구간들을 (요청 번호, 이벤트 위치) 평탄 배열로 펼친다."""
    counts = (hi - lo).astype(np.int64)
    total = int(counts.sum())
    rows = np.repeat(np.arange(len(lo), dtype=np.int64), counts)
    starts = np.cumsum(counts) - counts
    pos = np.arange(total, dtype=np.int64) - np.repeat(starts, counts) + np.repeat(lo.astype(np.int64), counts)
    return rows, pos
