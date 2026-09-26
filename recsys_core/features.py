"""(요청, 후보 아이템) 쌍의 point-in-time 피처를 벡터 연산으로 계산한다 - DB/LLM 없음.

요청(request)은 "유저 u가 시각 t에 후보 목록을 받는 순간"이다. 후보는 CSR 형태
(cand_ptr, cand_item)로 넘긴다. 유저 상태(히스토리/세션/카테고리 분포)는 profile_cutoff
(기본값 = t)보다 엄격히 이전 이벤트만, 아이템 상태(인기도)는 t보다 엄격히 이전 이벤트만
쓴다. profile_cutoff를 t와 따로 둔 이유는 "일 배치 프로필 vs 실시간 프로필" 같은 재생
실험에서 아이템 쪽 조건은 고정한 채 유저 상태의 신선도만 바꿔 보기 위해서다.

같은 코드를 오프라인 평가(EB-NeRD 하네스)와 이후 요청 시점 추천 API가 공유하도록
numpy/pandas만 쓴다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

import numpy as np
import pandas as pd

from .events import EventIndex, expand_ranges

HOUR = 3600
DAY = 86400


@dataclass
class ItemCatalog:
    ids: np.ndarray
    emb: np.ndarray
    pub_time: np.ndarray
    category: np.ndarray
    n_categories: int

    def __post_init__(self):
        self.ids = np.asarray(self.ids, dtype=np.int64)
        self.emb = np.ascontiguousarray(self.emb, dtype=np.float32)
        self.pub_time = np.asarray(self.pub_time, dtype=np.int64)
        self.category = np.asarray(self.category, dtype=np.int64)
        self._order = np.argsort(self.ids, kind="stable")
        self._sorted_ids = self.ids[self._order]

    def __len__(self) -> int:
        return len(self.ids)

    def index_of(self, external_ids) -> np.ndarray:
        """외부 아이템 id -> 카탈로그 행 번호. 없으면 -1."""
        q = np.asarray(external_ids, dtype=np.int64)
        pos = np.searchsorted(self._sorted_ids, q)
        pos = np.clip(pos, 0, max(len(self._sorted_ids) - 1, 0))
        found = (len(self._sorted_ids) > 0) & (self._sorted_ids[pos] == q)
        return np.where(found, self._order[pos], -1)


@dataclass
class Requests:
    user: np.ndarray
    time: np.ndarray
    cand_ptr: np.ndarray
    cand_item: np.ndarray
    session: Optional[np.ndarray] = None
    profile_cutoff: Optional[np.ndarray] = None

    def __post_init__(self):
        self.user = np.asarray(self.user, dtype=np.int64)
        self.time = np.asarray(self.time, dtype=np.int64)
        self.cand_ptr = np.asarray(self.cand_ptr, dtype=np.int64)
        self.cand_item = np.asarray(self.cand_item, dtype=np.int64)
        if self.session is not None:
            self.session = np.asarray(self.session, dtype=np.int64)
        if self.profile_cutoff is None:
            self.profile_cutoff = self.time
        self.profile_cutoff = np.asarray(self.profile_cutoff, dtype=np.int64)
        if len(self.cand_ptr) != len(self.user) + 1 or self.cand_ptr[-1] != len(self.cand_item):
            raise ValueError("cand_ptr는 길이 n+1의 CSR 오프셋이어야 합니다")
        if np.any(self.profile_cutoff > self.time):
            raise ValueError("profile_cutoff가 요청 시각보다 미래일 수 없습니다")

    @property
    def n(self) -> int:
        return len(self.user)

    @property
    def n_candidates(self) -> np.ndarray:
        return np.diff(self.cand_ptr)

    @property
    def pair_req(self) -> np.ndarray:
        return np.repeat(np.arange(self.n, dtype=np.int64), self.n_candidates)


@dataclass
class FeatureConfig:
    half_life_days: float = 7.0
    # 팀 FeatureEngineer(recommend_engine)의 감쇠식을 그대로 재현할 때만 쓴다:
    # 경과 일수를 내림하고(timedelta.days) 가중치 하한 min_weight를 둔다.
    floor_days: bool = False
    min_weight: float = 0.0
    short_window_h: float = 24.0
    pop_windows_h: Sequence[float] = (6, 24, 48)
    ctr_window_h: float = 24.0
    request_chunk: int = 8192
    event_chunk: int = 16384
    pair_chunk: int = 32768


@dataclass
class FeatureContext:
    catalog: ItemCatalog
    user_log: EventIndex
    session_log: Optional[EventIndex] = None
    item_clicks: Optional[EventIndex] = None
    item_inviews: Optional[EventIndex] = None
    # 팀 방식의 "온보딩 선호 카테고리" 대체물: (정렬된 유저 키, [유저, 카테고리] bool 행렬)
    static_categories: Optional[tuple] = None
    config: FeatureConfig = field(default_factory=FeatureConfig)


def feature_groups(config: FeatureConfig | None = None) -> dict[str, list[str]]:
    cfg = config or FeatureConfig()
    pops = [f"pop_clicks_{int(w)}h" for w in cfg.pop_windows_h]
    return {
        "recency": ["hours_since_pub", "is_fresh_24h", "is_fresh_7d"],
        "history": ["hist_cos", "hist_len"],
        "team_category": ["news_category", "cat_match_count", "is_cat_match", "user_ncat"],
        "category": ["cat_share"],
        "popularity": pops + [f"pop_inviews_{int(cfg.ctr_window_h)}h", f"pop_ctr_{int(cfg.ctr_window_h)}h"],
        "short_term": ["short_cos", "short_len", "sess_cos", "sess_len", "hours_since_last_event"],
    }


def _decay_weights(age_s: np.ndarray, cfg: FeatureConfig) -> np.ndarray:
    age_days = np.floor_divide(age_s, DAY) if cfg.floor_days else age_s / DAY
    w = np.power(0.5, age_days / cfg.half_life_days)
    if cfg.min_weight > 0:
        w = np.maximum(w, cfg.min_weight)
    return w.astype(np.float32)


def _request_chunks(counts: np.ndarray, max_events: int) -> Iterable[tuple[int, int]]:
    cum = np.cumsum(counts)
    n = len(counts)
    start = 0
    while start < n:
        base = cum[start - 1] if start > 0 else 0
        end = int(np.searchsorted(cum, base + max_events, side="right"))
        end = max(end, start + 1)
        yield start, min(end, n)
        start = end


def window_vectors(index: EventIndex, emb: np.ndarray, key, t_lo, t_hi, *,
                   ref_time=None, config: FeatureConfig | None = None,
                   decay: bool = False) -> tuple[np.ndarray, np.ndarray]:
    """각 요청의 [t_lo, t_hi) 이벤트 아이템 임베딩의 (감쇠) 가중합을 L2 정규화해 반환.

    코사인은 크기에 불변이라 가중 평균 대신 가중합을 정규화해도 결과가 같다.
    이벤트가 없는 요청은 0 벡터(코사인 0)다. 반환: (벡터 [n, d], 이벤트 수 [n]).
    """
    cfg = config or FeatureConfig()
    key = np.asarray(key, dtype=np.int64)
    n, d = len(key), emb.shape[1]
    out = np.zeros((n, d), dtype=np.float32)
    lo, hi = index.bounds(key, t_lo, t_hi)
    counts = hi - lo
    if n == 0 or counts.sum() == 0:
        return out, counts
    ref = None if ref_time is None else np.broadcast_to(np.asarray(ref_time, dtype=np.int64), key.shape)
    for s, e in _request_chunks(counts, cfg.event_chunk):
        c = counts[s:e]
        if c.sum() == 0:
            continue
        rows, pos = expand_ranges(lo[s:e], hi[s:e])
        items = index.item[pos]
        valid = (items >= 0) & (items < len(emb))
        if decay:
            w = _decay_weights(ref[s:e][rows] - index.time[pos], cfg)
        else:
            w = np.ones(len(pos), dtype=np.float32)
        w = np.where(valid, w, 0.0).astype(np.float32)
        x = emb[np.where(valid, items, 0)] * w[:, None]
        nonempty = c > 0
        starts = (np.cumsum(c) - c)[nonempty]
        out[s:e][nonempty] = np.add.reduceat(x, starts, axis=0)
    norms = np.linalg.norm(out, axis=1, keepdims=True)
    np.divide(out, norms, out=out, where=norms > 0)
    return out, counts


def window_category_counts(index: EventIndex, item_category: np.ndarray, n_categories: int,
                           key, t_lo, t_hi, config: FeatureConfig | None = None) -> np.ndarray:
    cfg = config or FeatureConfig()
    key = np.asarray(key, dtype=np.int64)
    n = len(key)
    out = np.zeros((n, n_categories), dtype=np.float32)
    lo, hi = index.bounds(key, t_lo, t_hi)
    counts = hi - lo
    for s, e in _request_chunks(counts, cfg.event_chunk * 8):
        if counts[s:e].sum() == 0:
            continue
        rows, pos = expand_ranges(lo[s:e], hi[s:e])
        items = index.item[pos]
        valid = (items >= 0) & (items < len(item_category))
        cats = item_category[items[valid]]
        flat = np.bincount(rows[valid] * n_categories + cats, minlength=(e - s) * n_categories)
        out[s:e] = flat.reshape(e - s, n_categories)
    return out


def _bounded_chunks(counts: np.ndarray, max_events: int, max_rows: int) -> Iterable[tuple[int, int]]:
    for s, e in _request_chunks(counts, max_events):
        for a in range(s, e, max_rows):
            yield a, min(a + max_rows, e)


def _history_cosine_segments(index: EventIndex, emb: np.ndarray, users: np.ndarray, cutoff: np.ndarray,
                             cand_ptr: np.ndarray, cand_item: np.ndarray,
                             cfg: FeatureConfig) -> tuple[np.ndarray, np.ndarray]:
    """감쇠 가중 히스토리 벡터와 후보의 코사인을 유저별 누적합으로 계산한다.

    지수 감쇠 w = 2^-((cutoff - t)/h)는 2^-(cutoff/h) * 2^(t/h)로 나뉘고 코사인은 크기에
    불변이라, 요청마다 전체 히스토리를 다시 더하는 대신(비용 O(요청 x 히스토리 x d))
    유저의 요청을 cutoff 순으로 세워 직전 요청 이후 새로 생긴 이벤트 구간만 더해 누적하면
    (비용 O((이벤트 + 요청) x d)) 같은 방향을 얻는다. 기준 시각(anchor)은 유저의 마지막
    cutoff로 둬 가중치가 1 이하로 유지되게 한다. 내림/하한이 있는 팀 감쇠식은 이렇게
    분해되지 않으므로 호출부가 기존 경로를 쓴다.
    """
    n, d = len(users), emb.shape[1]
    hist_cos = np.zeros(len(cand_item), dtype=np.float32)
    hist_len = np.zeros(n, dtype=np.float32)
    if n == 0:
        return hist_cos, hist_len
    order = np.lexsort((cutoff, users))
    k_s, c_s = users[order], cutoff[order]
    lo, hi = index.bounds(k_s, 0, c_s)
    hist_len[order] = hi - lo
    new_user = np.ones(n, dtype=bool)
    new_user[1:] = k_s[1:] != k_s[:-1]
    prev_hi = np.concatenate([[0], hi[:-1]])
    seg_lo = np.where(new_user, lo, np.minimum(prev_hi, hi))
    group = np.cumsum(new_user) - 1
    last_of_group = np.concatenate([np.flatnonzero(new_user)[1:] - 1, [n - 1]])
    anchor = c_s[last_of_group][group]
    h = cfg.half_life_days * DAY
    carry = np.zeros(d, dtype=np.float64)
    for s, e in _bounded_chunks(hi - seg_lo, cfg.event_chunk, cfg.request_chunk):
        m = e - s
        seg_counts = hi[s:e] - seg_lo[s:e]
        seg = np.zeros((m, d), dtype=np.float64)
        if seg_counts.sum() > 0:
            rows, pos = expand_ranges(seg_lo[s:e], hi[s:e])
            w = np.exp2((index.time[pos] - anchor[s:e][rows]) / h).astype(np.float32)
            x = emb[index.item[pos]] * w[:, None]
            nonempty = seg_counts > 0
            starts = (np.cumsum(seg_counts) - seg_counts)[nonempty]
            seg[nonempty] = np.add.reduceat(x, starts, axis=0)
        cs = np.cumsum(seg, axis=0)
        nu = new_user[s:e]
        gstart = np.maximum.accumulate(np.where(nu, np.arange(m), 0))
        vec = cs - np.where((gstart > 0)[:, None], cs[np.maximum(gstart - 1, 0)], 0.0)
        continuing = gstart == 0 if not nu[0] else np.zeros(m, dtype=bool)
        vec[continuing] += carry
        carry = vec[-1].copy()
        norms = np.linalg.norm(vec, axis=1, keepdims=True)
        vec32 = np.divide(vec, norms, out=np.zeros_like(vec), where=norms > 0).astype(np.float32)
        req_ids = order[s:e]
        prow, ppos = expand_ranges(cand_ptr[req_ids], cand_ptr[req_ids + 1])
        hist_cos[ppos] = _pair_dot(vec32, prow, emb, cand_item[ppos], cfg.pair_chunk)
    return hist_cos, hist_len


def _pair_dot(vecs: np.ndarray, local_rows: np.ndarray, emb: np.ndarray, items: np.ndarray,
              chunk: int) -> np.ndarray:
    out = np.empty(len(items), dtype=np.float32)
    for s in range(0, len(items), chunk):
        e = s + chunk
        out[s:e] = np.einsum("ij,ij->i", vecs[local_rows[s:e]], emb[items[s:e]])
    return out


def compute_features(ctx: FeatureContext, req: Requests,
                     groups: Sequence[str] = ("recency", "history", "category", "popularity", "short_term"),
                     ) -> pd.DataFrame:
    """요청 x 후보 쌍마다 한 행인 피처 DataFrame (행 순서 = cand_item 순서)."""
    cfg = ctx.config
    cat = ctx.catalog
    names = feature_groups(cfg)
    unknown = set(groups) - set(names)
    if unknown:
        raise ValueError(f"알 수 없는 피처 그룹: {sorted(unknown)}")
    items = req.cand_item
    if np.any(items < 0) or np.any(items >= len(cat)):
        raise ValueError("cand_item에 카탈로그 밖 인덱스가 있습니다")
    pair_req = req.pair_req
    t_pair = req.time[pair_req]
    cols: dict[str, np.ndarray] = {}

    if "recency" in groups:
        hours = np.maximum(0.0, (t_pair - cat.pub_time[items]) / HOUR)
        cols["hours_since_pub"] = hours.astype(np.float32)
        cols["is_fresh_24h"] = (hours <= 24).astype(np.float32)
        cols["is_fresh_7d"] = (hours <= 168).astype(np.float32)

    if "popularity" in groups:
        if ctx.item_clicks is None or ctx.item_inviews is None:
            raise ValueError("popularity 그룹에는 item_clicks/item_inviews 인덱스가 필요합니다")
        for w in cfg.pop_windows_h:
            cols[f"pop_clicks_{int(w)}h"] = ctx.item_clicks.count(items, t_pair - int(w * HOUR), t_pair).astype(np.float32)
        cw = int(cfg.ctr_window_h * HOUR)
        clicks = ctx.item_clicks.count(items, t_pair - cw, t_pair).astype(np.float32)
        inviews = ctx.item_inviews.count(items, t_pair - cw, t_pair).astype(np.float32)
        cols[f"pop_inviews_{int(cfg.ctr_window_h)}h"] = inviews
        cols[f"pop_ctr_{int(cfg.ctr_window_h)}h"] = clicks / np.maximum(inviews, 1.0)

    if "team_category" in groups:
        cols["news_category"] = cat.category[items].astype(np.float32)
        if ctx.static_categories is None:
            raise ValueError("team_category 그룹에는 static_categories가 필요합니다")
        keys, matrix = ctx.static_categories
        keys = np.asarray(keys, dtype=np.int64)
        if len(keys) == 0:
            has = np.zeros(req.n, dtype=bool)
            pos = np.zeros(req.n, dtype=np.int64)
            matrix = np.zeros((1, cat.n_categories), dtype=bool)
        else:
            pos = np.clip(np.searchsorted(keys, req.user), 0, len(keys) - 1)
            has = keys[pos] == req.user
        user_ncat = np.where(has, matrix[pos].sum(axis=1), 0).astype(np.float32)
        match = has[pair_req] & matrix[pos[pair_req], cat.category[items]]
        cols["cat_match_count"] = match.astype(np.float32)
        cols["is_cat_match"] = match.astype(np.float32)
        cols["user_ncat"] = user_ncat[pair_req]

    fast_history = "history" in groups and not cfg.floor_days and cfg.min_weight <= 0
    if fast_history:
        hc, hl = _history_cosine_segments(ctx.user_log, cat.emb, req.user, req.profile_cutoff, req.cand_ptr,
                                          items, cfg)
        cols["hist_cos"] = hc
        cols["hist_len"] = hl[pair_req]
    loop_groups = [g for g in ("history", "category", "short_term") if g in groups
                   and not (g == "history" and fast_history)]
    if loop_groups:
        n_pairs = len(items)
        for g in loop_groups:
            for name in names[g]:
                cols[name] = np.zeros(n_pairs, dtype=np.float32)
        cutoff = req.profile_cutoff
        for a in range(0, req.n, cfg.request_chunk):
            b = min(a + cfg.request_chunk, req.n)
            pa, pb = req.cand_ptr[a], req.cand_ptr[b]
            if pa == pb:
                continue
            local = pair_req[pa:pb] - a
            it = items[pa:pb]
            users = req.user[a:b]
            cut = cutoff[a:b]
            if "history" in loop_groups:
                v, cnt = window_vectors(ctx.user_log, cat.emb, users, 0, cut, ref_time=cut,
                                        config=cfg, decay=True)
                cols["hist_cos"][pa:pb] = _pair_dot(v, local, cat.emb, it, cfg.pair_chunk)
                cols["hist_len"][pa:pb] = cnt[local]
            if "category" in loop_groups:
                cc = window_category_counts(ctx.user_log, cat.category, cat.n_categories, users, 0, cut, cfg)
                share = cc / np.maximum(cc.sum(axis=1, keepdims=True), 1.0)
                cols["cat_share"][pa:pb] = share[local, cat.category[it]]
            if "short_term" in loop_groups:
                v, cnt = window_vectors(ctx.user_log, cat.emb, users, cut - int(cfg.short_window_h * HOUR), cut,
                                        config=cfg)
                cols["short_cos"][pa:pb] = _pair_dot(v, local, cat.emb, it, cfg.pair_chunk)
                cols["short_len"][pa:pb] = cnt[local]
                if ctx.session_log is not None and req.session is not None:
                    v, cnt = window_vectors(ctx.session_log, cat.emb, req.session[a:b], 0, cut, config=cfg)
                    cols["sess_cos"][pa:pb] = _pair_dot(v, local, cat.emb, it, cfg.pair_chunk)
                    cols["sess_len"][pa:pb] = cnt[local]
                last = ctx.user_log.last_time_before(users, cut)
                t_req = req.time[a:b]
                gap = np.where(last >= 0, (t_req - last) / HOUR, np.nan).astype(np.float32)
                cols["hours_since_last_event"][pa:pb] = gap[local]

    ordered = [n for g in names for n in names[g] if g in groups]
    return pd.DataFrame({n: cols[n] for n in ordered})
