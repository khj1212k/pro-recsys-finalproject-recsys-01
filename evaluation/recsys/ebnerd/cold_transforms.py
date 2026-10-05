"""콜드 regime 사슬(v1.2)의 조건 변환: 학습 마스킹, 유저 서브샘플, 풀 축소, 랭크 정규화, 축소 CTR, 릴리스 양자화.

정의는 ADR 0013 "A2 사전 등록" A2.3에 고정돼 있고, 표본·시드 값은 preregistration/cold-v1.2.yaml에서 호출부가 넘긴다.
모든 함수는 입력을 바꾸지 않고 사본이나 마스크를 돌려주며, 같은 인자에는 같은 결과를 낸다.
히스토리 절단과 인기도 0 강제는 E15와 공유하므로 `neural/cold.py`에 있다.
"""
from __future__ import annotations

import dataclasses
from typing import Optional, Sequence

import numpy as np
import pandas as pd

from recsys_core import DAY, HOUR, EventIndex, FeatureContext, ItemCatalog, Requests, expand_ranges, group_ids

from .loaders import Impressions, click_events, inview_events
from .neural.cold import pop_mask_raw
from .prepare import Bench, RankTask, filter_candidates, seen_mask

SHRUNK_COLUMN = "pop_ctr_shrunk_24h"


# --- 학습 시 인기도 마스킹 (E1) ---------------------------------------------------------------

def mask_training_popularity(feats: pd.DataFrame, req: Requests, p: float, rng: np.random.Generator,
                             value: float, extra_columns: Sequence[str] = ()) -> tuple[pd.DataFrame, np.ndarray]:
    """요청 단위로 확률 p로 골라 그 요청의 모든 후보에서 인기도 열을 value로 가린다.

    콜드 서비스에서는 한 요청의 후보가 전부 인기도 0이므로 단위는 행이 아니라 요청이다.
    반환: (가린 사본, 요청별 마스크). 난수는 요청 수만큼만 쓰므로 같은 rng 상태면 value가 달라도 같은 요청이 가려진다.
    """
    request_mask = rng.random(req.n) < p
    return pop_mask_raw(feats, value=value, rows=request_mask[req.pair_req], extra_columns=extra_columns), request_mask


# --- 유저 서브샘플 (E1, E6, E7) ---------------------------------------------------------------

def behaviour_users(bench: Bench) -> np.ndarray:
    """train ∪ validation 행동 로그에 나오는 유저 id(정렬)."""
    return np.unique(np.concatenate([imp.user_id for imp in bench.imps.values()]))


def subsample_users(universe: np.ndarray, fraction: float, seed: int) -> np.ndarray:
    """고정 순열의 앞 ceil(fraction x N)명. 같은 seed면 작은 비율이 큰 비율의 부분집합이다."""
    universe = np.unique(np.asarray(universe, dtype=np.int64))
    perm = np.random.default_rng(seed).permutation(len(universe))
    m = int(np.ceil(fraction * len(universe)))
    return np.sort(universe[perm[:m]])


def subsample_item_logs(imps: dict[str, Impressions], catalog: ItemCatalog,
                        users: np.ndarray) -> tuple[EventIndex, EventIndex]:
    """주어진 유저들의 행동(클릭·노출)만으로 만든 아이템 인기도 인덱스. load_bench와 같은 구성이다."""
    clicks, views = [], []
    for imp in imps.values():
        sub = imp.subset(np.flatnonzero(np.isin(imp.user_id, users)))
        clicks.append(click_events(sub)[["time", "article_id"]])
        views.append(inview_events(sub))
    out = []
    for frames in (clicks, views):
        ev = pd.concat(frames, ignore_index=True)
        item = catalog.index_of(ev["article_id"])
        ok = item >= 0
        out.append(EventIndex(item[ok], ev["time"].to_numpy()[ok], item[ok]))
    return out[0], out[1]


def subsample_request_indices(imp: Impressions, window: tuple[int, int], users: np.ndarray,
                              cap: Optional[int], seed: int) -> np.ndarray:
    """window 안에서 주어진 유저가 낸 요청의 인덱스(정렬). cap을 넘으면 고정 seed로 cap개를 뽑는다."""
    idx = np.flatnonzero((imp.time >= window[0]) & (imp.time < window[1]) & np.isin(imp.user_id, users))
    if cap and len(idx) > cap:
        idx = np.sort(np.random.default_rng(seed).choice(idx, size=cap, replace=False))
    return idx


# --- 풀 축소 (E1) -----------------------------------------------------------------------------

def pool_shrink_keep(labels: np.ndarray, ptr: np.ndarray, n: int, seed: int) -> np.ndarray:
    """요청당 후보를 n개로 줄이는 마스크: 풀 안 정답은 전부 남기고 나머지는 후보별 난수 순으로 채운다.

    같은 seed면 난수가 같아 n이 작은 마스크가 큰 마스크의 부분집합이다. 풀이 n 이하인 요청은 그대로다.
    """
    labels = np.asarray(labels, dtype=bool)
    ptr = np.asarray(ptr, dtype=np.int64)
    key = np.where(labels, -1.0, np.random.default_rng(seed).random(len(labels)))
    g = group_ids(ptr)
    order = np.lexsort((key, g))
    rank = np.empty(len(labels), dtype=np.int64)
    rank[order] = np.arange(len(labels)) - ptr[g[order]]
    return (rank < n) | labels


# --- 요청 내 랭크 정규화 (E3) -----------------------------------------------------------------

def average_rank01(values: np.ndarray, ptr: np.ndarray) -> np.ndarray:
    """그룹 안 오름차순 평균 순위를 0~1로 편 값. 동점은 평균 순위, 유효 값이 하나거나 전부 동점이면 0.5, NaN은 NaN."""
    v = np.asarray(values, dtype=np.float64)
    ptr = np.asarray(ptr, dtype=np.int64)
    out = np.full(len(v), np.nan)
    ok = ~np.isnan(v)
    if not ok.any():
        return out
    gi, vi = group_ids(ptr)[ok], v[ok]
    order = np.lexsort((vi, gi))
    gs, vs = gi[order], vi[order]
    n = len(vs)
    pos = np.arange(n)
    new_group = np.ones(n, dtype=bool)
    new_group[1:] = gs[1:] != gs[:-1]
    group_start = np.maximum.accumulate(np.where(new_group, pos, 0))
    new_block = new_group.copy()
    new_block[1:] |= vs[1:] != vs[:-1]
    starts = np.flatnonzero(new_block)
    ends = np.concatenate([starts[1:], [n]]) - 1
    block = np.cumsum(new_block) - 1
    avg = (starts[block] + ends[block]) / 2.0 - group_start
    count = np.bincount(gs, minlength=len(ptr) - 1)[gs]
    ranked = np.where(count > 1, avg / np.maximum(count - 1, 1), 0.5)
    res = np.empty(n)
    res[order] = ranked
    out[ok] = res
    return out


def rank_normalize(feats: pd.DataFrame, ptr: np.ndarray, columns: Sequence[str]) -> pd.DataFrame:
    """columns의 열을 요청 안 평균 순위(0~1)로 바꾼 사본. 프레임에 없는 열은 건너뛴다."""
    out = feats.copy()
    for c in columns:
        if c in out.columns:
            out[c] = average_rank01(out[c].to_numpy(), ptr).astype(np.float32)
    return out


# --- 축소 CTR (E6) ----------------------------------------------------------------------------

def global_ctr(item_clicks: EventIndex, item_inviews: EventIndex, t: np.ndarray, window_h: float = 24.0) -> np.ndarray:
    """각 시각 t의 [t-window, t) 전역 CTR = 전체 클릭 수 / 전체 노출 수. 노출이 없으면 0."""
    t = np.asarray(t, dtype=np.int64)
    w = int(window_h * HOUR)

    def count(index: EventIndex) -> np.ndarray:
        times = np.sort(index.time)
        return (np.searchsorted(times, t, side="left") - np.searchsorted(times, t - w, side="left")).astype(np.float64)

    clicks, views = count(item_clicks), count(item_inviews)
    return np.where(views > 0, clicks / np.maximum(views, 1.0), 0.0)


def add_shrunk_ctr(feats: pd.DataFrame, req: Requests, ctx: FeatureContext, alpha: float,
                   column: str = SHRUNK_COLUMN) -> pd.DataFrame:
    """pop_ctr_shrunk_24h = (clicks_24h + alpha x p0) / (inviews_24h + alpha), p0 = 요청 시각의 24h 전역 CTR.

    클릭·노출 수는 이미 계산된 raw 열(pop_clicks_24h, pop_inviews_24h)에서, p0는 같은 ctx의 아이템 로그에서 나온다.
    그래서 서브샘플 조건에서는 사전값도 서브샘플 로그의 것이 된다.
    """
    if alpha <= 0:
        raise ValueError("alpha는 양수여야 합니다")
    p0 = global_ctr(ctx.item_clicks, ctx.item_inviews, req.time, ctx.config.ctr_window_h)[req.pair_req]
    clicks = feats["pop_clicks_24h"].to_numpy(np.float64)
    views = feats["pop_inviews_24h"].to_numpy(np.float64)
    out = feats.copy()
    out[column] = ((clicks + alpha * p0) / (views + alpha)).astype(np.float32)
    return out


# --- 일일 배치 릴리스 양자화 (E4) -------------------------------------------------------------

def release_times(pub_time: np.ndarray, release_hour: int = 7) -> np.ndarray:
    """발행 시각 이후 처음 오는 일일 릴리스 시각(올림). 내림하면 아직 발행되지 않은 기사가 후보가 된다."""
    pub = np.asarray(pub_time, dtype=np.int64)
    off = release_hour * HOUR
    return -((-(pub - off)) // DAY) * DAY + off


def latest_release(t: np.ndarray, release_hour: int = 7) -> np.ndarray:
    """시각 t 이전(같음 포함) 가장 최근의 릴리스 시각(내림)."""
    t = np.asarray(t, dtype=np.int64)
    off = release_hour * HOUR
    return (t - off) // DAY * DAY + off


def p3_context(ctx: FeatureContext, release_hour: int = 7) -> FeatureContext:
    """릴리스 양자화 세계의 피처 컨텍스트: 발행 시각 = 릴리스 시각, 인기도 = 릴리스 이후 이벤트만."""
    cat = ctx.catalog
    rel = release_times(cat.pub_time, release_hour)
    quantized = ItemCatalog(ids=cat.ids, emb=cat.emb, pub_time=rel, category=cat.category,
                            n_categories=cat.n_categories)

    def after_release(index: Optional[EventIndex]) -> Optional[EventIndex]:
        if index is None:
            return None
        keep = index.time >= rel[index.key]
        return EventIndex(index.key[keep], index.time[keep], index.item[keep])

    return dataclasses.replace(ctx, catalog=quantized, item_clicks=after_release(ctx.item_clicks),
                               item_inviews=after_release(ctx.item_inviews))


def p3_task(bench: Bench, split: str, idx: np.ndarray, release_hour: int = 7, batches: int = 2,
            exclude_seen: bool = True) -> RankTask:
    """일일 배치 후보: 요청 시각의 가장 최근 릴리스와 그 앞 (batches-1)개 릴리스에 나온 기사 - 이미 읽은 것.

    라벨·n_pos_total은 p2_task와 같은 정의다(풀 밖 정답은 n_pos_total에만 남아 벌점이 된다).
    extra["release_age_h"]: 후보별 릴리스 후 경과 시간.
    """
    imp = bench.imps[split].subset(idx)
    cat = bench.catalog
    rel = release_times(cat.pub_time, release_hour)
    order = np.argsort(rel, kind="stable")
    rel_sorted = rel[order]
    current = latest_release(imp.time, release_hour)
    lo = np.searchsorted(rel_sorted, current - (batches - 1) * DAY, side="left")
    hi = np.searchsorted(rel_sorted, current, side="right")
    rows, pos = expand_ranges(lo, hi)
    items = order[pos]
    req = Requests(user=imp.user_id, time=imp.time, cand_ptr=np.concatenate([[0], np.cumsum(hi - lo)]),
                   cand_item=items, session=imp.session_id)
    n_items = np.int64(len(cat))
    ck_rows = np.repeat(np.arange(len(imp)), np.diff(imp.clicked_ptr))
    pos_keys = np.unique(ck_rows * n_items + cat.index_of(imp.clicked_article))
    task = RankTask(req=req, labels=np.isin(rows * n_items + items, pos_keys), group_user=imp.user_id,
                    imp_index=np.asarray(idx),
                    n_pos_total=np.bincount(pos_keys // n_items, minlength=len(imp)).astype(np.float64),
                    extra={"age": imp.age, "gender": imp.gender})
    if exclude_seen:
        task = filter_candidates(task, ~seen_mask(bench, split, req))
    r = task.req
    task.extra["release_age_h"] = (r.time[r.pair_req] - rel[r.cand_item]) / HOUR
    return task


def release_age_bucket(task: RankTask, edges_h: Sequence[float]) -> np.ndarray:
    """요청별 버킷 번호: 풀 안 정답 중 릴리스 후 경과 시간이 가장 짧은 것 기준. 풀 안 정답이 없으면 -1.

    edges_h = [0, 2, 6, 12, 24]이면 버킷은 [0,2) [2,6) [6,12) [12,24) [24,inf)의 0~4다.
    """
    age = np.asarray(task.extra["release_age_h"], dtype=np.float64)
    youngest = np.full(task.req.n, np.inf)
    lab = np.asarray(task.labels, dtype=bool)
    np.minimum.at(youngest, task.req.pair_req[lab], age[lab])
    bucket = np.searchsorted(np.asarray(edges_h, dtype=np.float64), youngest, side="right") - 1
    return np.where(np.isfinite(youngest), bucket, -1)
