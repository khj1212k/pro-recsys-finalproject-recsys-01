"""E15 신경망 arm의 입력: 마지막 N 클릭 시퀀스, 시퀀스 스칼라(A+), 스칼라 블록, 콜드 증강 (ADR 0013 A3.3·A3.4).

numpy/pandas만 쓴다(torch 없이 임포트된다). 모든 함수는 point-in-time을 지킨다: 시퀀스는 `EventIndex.bounds(key, 0, cutoff)`의
반열린 구간에서 읽으므로 요청 시각과 같거나 늦은 클릭은 들어오지 않는다.
열 목록·비율·시드 같은 등록 값은 호출부가 preregistration/neural-e15.yaml에서 읽어 넘긴다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

import numpy as np
import pandas as pd

from recsys_core import EventIndex, FeatureContext, Requests, compute_features, expand_ranges

from ..models import ALL_GROUPS
from .cold import truncate_logs

GENDER_TOKENS = 4          # 0 = 결측, 1.. = 값 0, 1, 2 (그 밖의 값은 가장 가까운 칸으로 자른다)


@dataclass
class Sequences:
    """요청마다 마지막 N 클릭. 오른쪽 정렬: 마지막 열이 가장 최근 클릭이고 앞쪽이 패딩이다."""
    items: np.ndarray     # [R, N] int32, 패딩 -1
    mask: np.ndarray      # [R, N] bool
    pos: np.ndarray       # [R, N] int64, 로그 안 이벤트 위치(보조 손실의 네거티브 조회용), 패딩 -1

    @property
    def n_hist(self) -> int:
        return int(self.items.shape[1])

    def subset(self, rows: np.ndarray) -> "Sequences":
        return Sequences(self.items[rows], self.mask[rows], self.pos[rows])


def last_n_clicks(index: EventIndex, key: np.ndarray, cutoff: np.ndarray, n: int) -> Sequences:
    """각 요청에서 cutoff보다 엄격히 이전인 이벤트 중 최근 n건."""
    if n <= 0:
        raise ValueError("n은 1 이상이어야 합니다")
    lo, hi = index.bounds(key, 0, cutoff)
    pos = hi[:, None] - n + np.arange(n, dtype=np.int64)[None, :]
    mask = pos >= lo[:, None]
    if len(index) == 0:
        mask = np.zeros_like(mask)
    safe = np.where(mask, pos, 0)
    items = np.where(mask, index.item[safe] if len(index) else 0, -1).astype(np.int32)
    return Sequences(items=items, mask=mask, pos=np.where(mask, pos, -1))


def truncate_sequences(seq: Sequences, k) -> Sequences:
    """요청마다 최근 k건만 남긴다. k는 스칼라나 요청별 배열이고, 음수는 "그대로"다.

    `truncate_logs`로 자른 로그에서 `last_n_clicks`를 다시 읽은 것과 같은 이벤트가 남는다(테스트가 묶는다).
    """
    n = seq.n_hist
    k = np.broadcast_to(np.asarray(k, dtype=np.int64), (len(seq.items),))
    keep = seq.mask & ((k[:, None] < 0) | (np.arange(n)[None, :] >= n - k[:, None]))
    return Sequences(items=np.where(keep, seq.items, -1).astype(np.int32), mask=keep, pos=np.where(keep, seq.pos, -1))


# --- 콜드 증강 --------------------------------------------------------------------------------

def cold_augment_plan(n_requests: int, fraction: float, ks: Sequence[int], seed: int) -> np.ndarray:
    """요청별 절단 k(-1 = 증강하지 않음). fraction만큼을 비복원으로 고르고 k를 균등 무작위로 정한다."""
    rng = np.random.default_rng(seed)
    m = int(round(fraction * n_requests))
    rows = rng.permutation(n_requests)[:m]
    plan = np.full(n_requests, -1, dtype=np.int64)
    plan[rows] = np.asarray(ks, dtype=np.int64)[rng.integers(0, len(ks), size=m)]
    return plan


def subset_requests(req: Requests, rows: np.ndarray) -> tuple[Requests, np.ndarray]:
    """요청 일부만 담은 Requests와, 그 후보들이 원래 쌍 배열에서 차지하던 위치."""
    rows = np.asarray(rows, dtype=np.int64)
    _, pairs = expand_ranges(req.cand_ptr[rows], req.cand_ptr[rows + 1])
    counts = req.cand_ptr[rows + 1] - req.cand_ptr[rows]
    sub = Requests(user=req.user[rows], time=req.time[rows], cand_ptr=np.concatenate([[0], np.cumsum(counts)]),
                   cand_item=req.cand_item[pairs], session=None if req.session is None else req.session[rows],
                   profile_cutoff=req.profile_cutoff[rows])
    return sub, pairs


def augment_features(ctx: FeatureContext, req: Requests, feats: pd.DataFrame, plan: np.ndarray) -> pd.DataFrame:
    """plan이 고른 요청의 피처 행을 절단 로그로 다시 계산한 값으로 바꾼 사본. 후보·행 순서는 그대로다."""
    cols = {c: feats[c].to_numpy(copy=True) for c in feats.columns}
    for k in np.unique(plan[plan >= 0]):
        rows = np.flatnonzero(plan == k)
        sub, pairs = subset_requests(req, rows)
        ctx_k, req_k = truncate_logs(ctx, sub, int(k))
        part = compute_features(ctx_k, req_k, groups=ALL_GROUPS)
        for c in cols:
            cols[c][pairs] = part[c].to_numpy()
    return pd.DataFrame(cols)


# --- 시퀀스 스칼라 (A+) -----------------------------------------------------------------------

def seq_scalars(seq: Sequences, req: Requests, emb: np.ndarray, columns: Sequence[str], empty_value: float = 0.0,
                pair_budget: int = 16384) -> pd.DataFrame:
    """후보와 마지막 N 클릭의 코사인으로 만든 스칼라: 최대, 상위 3개 평균(있는 만큼), 가장 최근, 후보가 그 안에 있는지.

    emb는 L2 정규화된 벡터라 내적이 코사인이다. 시퀀스가 비면 네 열 모두 empty_value.
    """
    names = list(columns)
    if len(names) != 4:
        raise ValueError("시퀀스 스칼라 열은 [max, top3, last, in_recent] 4개여야 합니다")
    ptr, counts = req.cand_ptr, req.n_candidates
    out = np.full((4, len(req.cand_item)), empty_value, dtype=np.float32)
    n = seq.n_hist
    step = max(1, pair_budget // max(int(counts.max()) if len(counts) else 1, 1))
    for a in range(0, req.n, step):
        b = min(a + step, req.n)
        cmax = int(counts[a:b].max())
        if cmax == 0:
            continue
        rows, pos = expand_ranges(ptr[a:b], ptr[a + 1:b + 1])
        col = pos - ptr[a:b][rows]
        cand_ids = np.full((b - a, cmax), -2, dtype=np.int64)
        cand_ids[rows, col] = req.cand_item[pos]
        c = np.zeros((b - a, cmax, emb.shape[1]), dtype=np.float32)
        c[rows, col] = emb[req.cand_item[pos]]
        m = seq.mask[a:b]
        s = emb[np.where(m, seq.items[a:b], 0)] * m[..., None]
        sims = np.matmul(c, s.transpose(0, 2, 1))                       # [요청, 후보, N]
        masked = np.where(m[:, None, :], sims, -np.inf)
        top = -np.sort(-masked, axis=-1)[..., :3]
        cnt = np.minimum(m.sum(axis=1), top.shape[-1])[:, None]
        top3 = np.where(np.isfinite(top), top, 0.0).sum(axis=-1) / np.maximum(cnt, 1)
        hit = ((cand_ids[:, :, None] == seq.items[a:b][:, None, :]) & m[:, None, :]).any(axis=-1)
        has = m.any(axis=1)[:, None]
        vals = (np.where(has, masked.max(axis=-1), empty_value), np.where(has, top3, empty_value),
                np.where(has, sims[..., n - 1], empty_value), np.where(has, hit.astype(np.float32), empty_value))
        for i, v in enumerate(vals):
            out[i, pos] = v[rows, col]
    return pd.DataFrame({name: out[i] for i, name in enumerate(names)})


# --- 스칼라 블록 x̃ ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ScalarSpec:
    log1p: tuple
    standardize: tuple
    passthrough: tuple
    category: str
    gender: str

    @classmethod
    def from_prereg(cls, block: Mapping) -> "ScalarSpec":
        return cls(tuple(block["log1p_standardize"]), tuple(block["standardize"]), tuple(block["passthrough"]),
                   block["category_embedding"], block["gender_embedding"])

    @property
    def continuous(self) -> tuple:
        return self.log1p + self.standardize + self.passthrough

    def check_covers(self, columns: Sequence[str]) -> None:
        mine = set(self.continuous) | {self.category, self.gender}
        if mine != set(columns) or len(mine) != len(self.continuous) + 2:
            raise ValueError(f"스칼라 블록의 열 정의가 피처 열과 다릅니다: {sorted(mine ^ set(columns))}")


def _transformed(x_raw: np.ndarray, columns: Sequence[str], spec: ScalarSpec) -> np.ndarray:
    idx = {c: i for i, c in enumerate(columns)}
    out = np.empty((len(x_raw), len(spec.continuous)), dtype=np.float64)
    for j, c in enumerate(spec.continuous):
        v = x_raw[:, idx[c]].astype(np.float64)
        out[:, j] = np.log1p(np.maximum(v, 0.0)) if c in spec.log1p else v     # NaN은 NaN으로 남는다
    return out


def standardization_stats(x_raw: np.ndarray, columns: Sequence[str], spec: ScalarSpec) -> dict:
    """fit 행의 변환값에서 평균·표준편차와, NaN이 한 번이라도 나온 열 목록을 낸다(모델과 함께 저장)."""
    spec.check_covers(columns)
    t = _transformed(x_raw, columns, spec)
    scaled = [c not in spec.passthrough for c in spec.continuous]
    mean, std = np.zeros(t.shape[1]), np.ones(t.shape[1])
    for j, on in enumerate(scaled):
        col = t[:, j][~np.isnan(t[:, j])]
        if on and len(col):
            mean[j] = float(col.mean())
            sd = float(col.std())
            std[j] = sd if sd > 1e-6 else 1.0
    missing = [c for i, c in enumerate(columns) if np.isnan(x_raw[:, i]).any()]
    return {"continuous": list(spec.continuous), "mean": mean.tolist(), "std": std.tolist(), "missing": missing,
            "columns": list(columns), "n_rows": int(len(x_raw))}


@dataclass
class ScalarBlock:
    cont: np.ndarray        # [행, 연속 열 + 결측 지표] float32
    category: np.ndarray    # [행] int64
    gender: np.ndarray      # [행] int64 (0 = 결측)


def scalar_block(x_raw: np.ndarray, columns: Sequence[str], spec: ScalarSpec, stats: Mapping,
                 n_categories: int) -> ScalarBlock:
    """raw 피처 행렬 -> 신경망의 스칼라 입력. 통계는 stats(fit에서 계산)의 것을 그대로 쓴다.

    인기도 0 강제 같은 콜드 조건은 raw 행렬에 먼저 적용한 뒤 이 함수를 부른다(표준화된 공간의 0은 다른 조건이다).
    """
    if list(stats["columns"]) != list(columns):
        raise ValueError("통계를 계산한 열 순서와 입력 열 순서가 다릅니다")
    idx = {c: i for i, c in enumerate(columns)}
    t = (_transformed(x_raw, columns, spec) - np.asarray(stats["mean"])) / np.asarray(stats["std"])
    flags = [np.isnan(x_raw[:, idx[c]]).astype(np.float64) for c in stats["missing"]]
    cont = np.column_stack([np.nan_to_num(t, nan=0.0)] + flags).astype(np.float32)
    cat = np.clip(np.nan_to_num(x_raw[:, idx[spec.category]], nan=0.0), 0, max(n_categories - 1, 0)).astype(np.int64)
    g = x_raw[:, idx[spec.gender]]
    gender = np.where(np.isnan(g), 0, 1 + np.clip(np.nan_to_num(g, nan=0.0), 0, GENDER_TOKENS - 2)).astype(np.int64)
    return ScalarBlock(cont=cont, category=cat, gender=gender)


def scalar_width(stats: Mapping) -> int:
    return len(stats["continuous"]) + len(stats["missing"])


def aux_negative_table(index: EventIndex, pub_time: np.ndarray, n_neg: int, window_h: float, seed: int,
                       hour: int = 3600) -> np.ndarray:
    """로그의 이벤트마다 보조 손실용 네거티브 n_neg개: 그 클릭 시각 t' 기준 [t'-window_h, t'] 발행 풀에서 복원 추출.

    풀이 비었거나 정답과 같은 기사가 뽑힌 칸은 -1이다(손실에서 가려진다). 같은 seed면 같은 표이고 epoch마다 다시 뽑지 않는다.
    """
    rng = np.random.default_rng(seed)
    order = np.argsort(pub_time, kind="stable")
    pub_sorted = np.asarray(pub_time)[order]
    lo = np.searchsorted(pub_sorted, index.time - int(window_h * hour), side="left")
    hi = np.searchsorted(pub_sorted, index.time, side="right")
    span = (hi - lo)[:, None]
    draw = rng.integers(0, 1 << 30, size=(len(index), n_neg))
    neg = np.where(span > 0, order[np.minimum(lo[:, None] + draw % np.maximum(span, 1), len(order) - 1)], -1)
    return np.where(neg == index.item[:, None], -1, neg).astype(np.int32)


def keys_sha256(*arrays: Optional[np.ndarray]) -> str:
    """배열들의 내용 해시(리포트의 표본·네거티브 기록용)."""
    import hashlib

    h = hashlib.sha256()
    for a in arrays:
        if a is not None:
            h.update(np.ascontiguousarray(a).tobytes())
    return h.hexdigest()
