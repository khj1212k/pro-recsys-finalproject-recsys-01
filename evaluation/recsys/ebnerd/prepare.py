"""EB-NeRD 원시 로그 -> recsys_core 입력(카탈로그, 이벤트 인덱스, 요청) 변환.

로그 구성 원칙(point-in-time + split 대칭):
- 유저 로그(히스토리/단기/카테고리): split S의 history.parquet(행동 창 이전 21일 읽기)
  + split S 행동 창의 클릭. train 요청이 validation history(=train 주간 읽기 기록)를
  보면 행동 창 안 밀도가 split마다 달라지므로 split별로 따로 만든다.
- 세션 로그: split S 행동 창의 클릭(세션 키).
- 아이템 인기도(클릭/노출): train+validation 행동 로그 전체. history 읽기 기록은
  행동 창 안 클릭보다 4배가량 촘촘해서 섞으면 창 경계에서 인기도 척도가 바뀐다.
모든 조회는 recsys_core가 [.., t) 반열린 구간으로 자르므로 미래 이벤트가 들어 있어도 안전하다.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from recsys_core import DAY, HOUR, EventIndex, FeatureConfig, FeatureContext, ItemCatalog, Requests

from .loaders import Impressions, click_events, inview_events, load_articles, load_history_events, load_impressions


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build_catalog(dataset_dir: Path, emb_dir: Optional[Path] = None, emb_name: str = "bge_m3_tsb512",
                  fake_dim: Optional[int] = None, seed: int = 0) -> tuple[ItemCatalog, dict]:
    arts = load_articles(dataset_dir)
    cats, cat_codes = np.unique(arts["category"].to_numpy(), return_inverse=True)
    n = len(arts)
    info: dict = {"n_articles": n, "n_categories": int(len(cats))}
    if fake_dim:
        rng = np.random.default_rng(seed)
        emb = rng.standard_normal((n, fake_dim)).astype(np.float32)
        info["embeddings"] = f"FAKE random normal dim={fake_dim} seed={seed} (개발용, 결과 해석 금지)"
    else:
        emb_dir = Path(emb_dir)
        ids = np.load(emb_dir / "article_ids.npy")
        vecs = np.load(emb_dir / f"{emb_name}.f16.npy").astype(np.float32)
        meta = json.loads((emb_dir / f"{emb_name}.meta.json").read_text())
        pos = pd.Index(ids).get_indexer(arts["article_id"].to_numpy())
        emb = np.zeros((n, vecs.shape[1]), dtype=np.float32)
        emb[pos >= 0] = vecs[pos[pos >= 0]]
        info.update({
            "embeddings": emb_name, "embeddings_sha256": meta.get("embeddings_sha256"),
            "embeddings_meta": {k: meta.get(k) for k in ("model", "model_snapshot", "max_length_tokens",
                                                         "text_template", "device", "compute_fp16")},
            "missing_embeddings": int((pos < 0).sum()),
        })
    norms = np.linalg.norm(emb, axis=1, keepdims=True)
    np.divide(emb, norms, out=emb, where=norms > 0)
    catalog = ItemCatalog(ids=arts["article_id"].to_numpy(), emb=emb, pub_time=arts["published_ts"].to_numpy(),
                          category=cat_codes, n_categories=len(cats))
    return catalog, info


def static_top_categories(user_ids: np.ndarray, items: np.ndarray, catalog: ItemCatalog,
                          top: int = 3) -> tuple[np.ndarray, np.ndarray]:
    """팀 '온보딩 선호 카테고리'의 대체물: 행동 창 이전 히스토리에서 가장 많이 읽은 카테고리 top개.
    창이 시작되기 전 스냅샷이라 이후 갱신되지 않는다(팀의 정적 온보딩 설정과 같은 성질)."""
    ok = items >= 0
    users, inv = np.unique(user_ids[ok], return_inverse=True)
    counts = np.zeros((len(users), catalog.n_categories), dtype=np.int64)
    np.add.at(counts, (inv, catalog.category[items[ok]]), 1)
    order = np.argsort(-counts, axis=1, kind="stable")[:, :top]
    matrix = np.zeros_like(counts, dtype=bool)
    rows = np.repeat(np.arange(len(users)), top)
    top_cols = order.ravel()
    matrix[rows, top_cols] = counts[rows, top_cols] > 0
    return users, matrix


@dataclass
class Bench:
    catalog: ItemCatalog
    imps: dict[str, Impressions]
    ctx: dict[str, FeatureContext]
    catalog_info: dict
    user_items: dict[str, np.ndarray] = field(default_factory=dict)
    data_info: dict = field(default_factory=dict)


def load_bench(dataset_dir: Path, emb_dir: Optional[Path] = None, fake_dim: Optional[int] = None,
               config: Optional[FeatureConfig] = None) -> Bench:
    dataset_dir = Path(dataset_dir)
    catalog, cinfo = build_catalog(dataset_dir, emb_dir, fake_dim=fake_dim)
    imps = {s: load_impressions(dataset_dir, s) for s in ("train", "validation")}
    clicks = {s: click_events(imps[s]) for s in imps}
    all_clicks = pd.concat(clicks.values(), ignore_index=True)
    all_views = pd.concat([inview_events(imps[s]) for s in imps], ignore_index=True)
    ci = catalog.index_of(all_clicks["article_id"])
    vi = catalog.index_of(all_views["article_id"])
    item_clicks = EventIndex(ci[ci >= 0], all_clicks["time"].to_numpy()[ci >= 0], ci[ci >= 0])
    item_inviews = EventIndex(vi[vi >= 0], all_views["time"].to_numpy()[vi >= 0], vi[vi >= 0])
    cfg = config or FeatureConfig()
    ctx, user_items, info = {}, {}, {"files": {}}
    for s in imps:
        hist = load_history_events(dataset_dir, s)
        hi = catalog.index_of(hist["article_id"])
        c = clicks[s]
        cidx = catalog.index_of(c["article_id"])
        users = np.concatenate([hist["user_id"].to_numpy(), c["user_id"].to_numpy()])
        times = np.concatenate([hist["time"].to_numpy(), c["time"].to_numpy()])
        items = np.concatenate([hi, cidx])
        keep = items >= 0
        user_log = EventIndex(users[keep], times[keep], items[keep], dedupe=True)
        session_log = EventIndex(c["session_id"].to_numpy()[cidx >= 0], c["time"].to_numpy()[cidx >= 0],
                                 cidx[cidx >= 0])
        ctx[s] = FeatureContext(
            catalog=catalog, user_log=user_log, session_log=session_log, item_clicks=item_clicks,
            item_inviews=item_inviews,
            static_categories=static_top_categories(hist["user_id"].to_numpy(), hi, catalog),
            config=cfg,
        )
        user_items[s] = np.unique((user_log.key << 32) | user_log.item)
        info[s] = {
            "impressions": int(len(imps[s])), "users": int(len(np.unique(imps[s].user_id))),
            "history_events": int(len(hist)), "history_events_unknown_article": int((hi < 0).sum()),
            "window_clicks": int(len(c)), "user_log_events_dedup": int(len(user_log)),
            "time_min": int(imps[s].time.min()), "time_max": int(imps[s].time.max()),
        }
        for f in ("behaviors.parquet", "history.parquet"):
            info["files"][f"{s}/{f}"] = sha256_file(dataset_dir / s / f)
    info["files"]["articles.parquet"] = sha256_file(dataset_dir / "articles.parquet")
    return Bench(catalog=catalog, imps=imps, ctx=ctx, catalog_info=cinfo, user_items=user_items, data_info=info)


def floor_hour(t: int) -> int:
    return int(t) // HOUR * HOUR


def protocol_windows(bench: Bench, pop_window_h: float = 48) -> dict[str, tuple[int, int]]:
    """학습(fit)/early-stop(es)/평가(test) 구간. fit은 train 창 시작 후 pop_window_h 이후부터:
    그 전에는 trailing 인기도 창이 행동 로그 시작 이전으로 잘려 피처 분포가 달라진다."""
    tr, va = bench.imps["train"], bench.imps["validation"]
    train_start = floor_hour(tr.time.min())
    valid_start = floor_hour(va.time.min())
    return {
        "fit": (train_start + int(pop_window_h * HOUR), valid_start - DAY),
        "es": (valid_start - DAY, valid_start),
        "test": (valid_start, int(va.time.max()) + 1),
    }


def impressions_in(imp: Impressions, window: tuple[int, int]) -> np.ndarray:
    return np.flatnonzero((imp.time >= window[0]) & (imp.time < window[1]))


@dataclass
class RankTask:
    """요청 + 후보별 라벨 + 그룹 메타. p1(노출 재정렬)/p2(전체 풀)/랜덤 네거티브 공통."""
    req: Requests
    labels: np.ndarray
    group_user: np.ndarray
    imp_index: np.ndarray
    n_pos_total: Optional[np.ndarray] = None
    extra: dict = field(default_factory=dict)


def p1_task(bench: Bench, split: str, idx: np.ndarray, profile_cutoff: Optional[np.ndarray] = None) -> RankTask:
    imp = bench.imps[split].subset(idx)
    items = bench.catalog.index_of(imp.inview_article)
    if np.any(items < 0):
        raise ValueError("노출 후보 중 카탈로그에 없는 기사가 있습니다")
    req = Requests(user=imp.user_id, time=imp.time, cand_ptr=imp.inview_ptr, cand_item=items,
                   session=imp.session_id, profile_cutoff=profile_cutoff)
    return RankTask(req=req, labels=imp.inview_clicked.copy(), group_user=imp.user_id, imp_index=np.asarray(idx),
                    extra={"age": imp.age, "gender": imp.gender})


def seen_mask(bench: Bench, split: str, req: Requests) -> np.ndarray:
    """후보 중 유저가 profile_cutoff 이전에 이미 읽은/클릭한 아이템 표시."""
    ctx = bench.ctx[split]
    lo, hi = ctx.user_log.bounds(req.user, 0, req.profile_cutoff)
    from recsys_core import expand_ranges
    rows, pos = expand_ranges(lo, hi)
    seen_keys = np.unique(rows * np.int64(len(bench.catalog)) + ctx.user_log.item[pos])
    pair_keys = req.pair_req * np.int64(len(bench.catalog)) + req.cand_item
    return np.isin(pair_keys, seen_keys)


def filter_candidates(task: RankTask, keep: np.ndarray) -> RankTask:
    req = task.req
    counts = np.bincount(req.pair_req[keep], minlength=req.n)
    ptr = np.concatenate([[0], np.cumsum(counts)])
    new_req = Requests(user=req.user, time=req.time, cand_ptr=ptr, cand_item=req.cand_item[keep],
                       session=req.session, profile_cutoff=req.profile_cutoff)
    return RankTask(req=new_req, labels=task.labels[keep], group_user=task.group_user, imp_index=task.imp_index,
                    n_pos_total=task.n_pos_total, extra=task.extra)


def p2_task(bench: Bench, split: str, idx: np.ndarray, window_h: float = 48,
            exclude_seen: bool = True) -> RankTask:
    """전체 코퍼스 후보: 요청 시각 t 기준 [t-window_h, t]에 발행된 모든 기사(이미 읽은 것 제외)."""
    imp = bench.imps[split].subset(idx)
    cat = bench.catalog
    order = np.argsort(cat.pub_time, kind="stable")
    pub_sorted = cat.pub_time[order]
    lo = np.searchsorted(pub_sorted, imp.time - int(window_h * HOUR), side="left")
    hi = np.searchsorted(pub_sorted, imp.time, side="right")
    from recsys_core import expand_ranges
    rows, pos = expand_ranges(lo, hi)
    items = order[pos]
    ptr = np.concatenate([[0], np.cumsum(hi - lo)])
    req = Requests(user=imp.user_id, time=imp.time, cand_ptr=ptr, cand_item=items, session=imp.session_id)
    clicked_idx = cat.index_of(imp.clicked_article)
    ck_rows = np.repeat(np.arange(len(imp)), np.diff(imp.clicked_ptr))
    pos_keys = np.unique(ck_rows * np.int64(len(cat)) + clicked_idx)
    n_pos_total = np.bincount(pos_keys // len(cat), minlength=len(imp)).astype(np.float64)
    labels = np.isin(rows * np.int64(len(cat)) + items, pos_keys)
    task = RankTask(req=req, labels=labels, group_user=imp.user_id, imp_index=np.asarray(idx),
                    n_pos_total=n_pos_total, extra={"age": imp.age, "gender": imp.gender})
    pool_pos = np.bincount(rows[labels], minlength=len(imp))
    task.extra["pos_in_pool_before_seen_filter"] = pool_pos
    if exclude_seen:
        seen = seen_mask(bench, split, req)
        task.extra["seen_fraction_of_pool"] = float(seen.mean()) if len(seen) else 0.0
        task.extra["positives_removed_as_seen"] = int((labels & seen).sum())
        task = filter_candidates(task, ~seen)
        task.extra["pos_in_pool_before_seen_filter"] = pool_pos
    return task


def random_negative_task(bench: Bench, split: str, idx: np.ndarray, rng: np.random.Generator,
                         n_neg: int = 5, pool_days: float = 7) -> RankTask:
    """팀 방식 학습 데이터: 클릭 1건 + 발행 pool_days일 이내 기사 중 무작위 n_neg건.

    팀 코드는 클릭 시각 이전에 발행된 뉴스 전체에서 뽑는다(팀 DB는 뉴스레터 보관 기간이
    짧다). EB-NeRD 카탈로그는 수년치 기사를 담고 있어 그대로 쓰면 발행 경과 시간 하나로
    네거티브가 자명하게 갈리므로 pool_days(기본 7일)로 제한한 것이 유일한 변경점이다.
    팀 코드(lgbm_dataset.py)처럼 유저가 로그 전체에서 한 번이라도 읽은 기사는 네거티브에서
    뺀다(팀 코드는 미래 클릭까지 제외 집합에 넣는다 - 학습 데이터 구성상의 누수지만 평가
    쪽 누수는 아니므로 팀 방식 재현을 위해 그대로 둔다).
    """
    imp = bench.imps[split].subset(idx)
    cat = bench.catalog
    ck_rows = np.repeat(np.arange(len(imp)), np.diff(imp.clicked_ptr))
    pos_items = cat.index_of(imp.clicked_article)
    ok = pos_items >= 0
    ck_rows, pos_items = ck_rows[ok], pos_items[ok]
    users = imp.user_id[ck_rows]
    times = imp.time[ck_rows]
    order = np.argsort(cat.pub_time, kind="stable")
    pub_sorted = cat.pub_time[order]
    lo = np.searchsorted(pub_sorted, times - int(pool_days * DAY), side="left")
    hi = np.searchsorted(pub_sorted, times, side="right")
    seen = bench.user_items[split]
    negs = np.full((len(users), n_neg), -1, dtype=np.int64)
    todo = np.repeat((hi > lo)[:, None], n_neg, axis=1)
    for _ in range(20):
        r, c = np.nonzero(todo)
        if len(r) == 0:
            break
        cand = order[lo[r] + rng.integers(0, 1 << 30, size=len(r)) % (hi[r] - lo[r])]
        bad = np.isin((users[r] << 32) | cand, seen) | (cand == pos_items[r])
        negs[r[~bad], c[~bad]] = cand[~bad]
        todo[r[~bad], c[~bad]] = False
    cand_item = np.concatenate([pos_items[:, None], negs], axis=1)
    valid = cand_item >= 0
    counts = valid.sum(axis=1)
    ptr = np.concatenate([[0], np.cumsum(counts)])
    labels = np.zeros_like(cand_item, dtype=bool)
    labels[:, 0] = True
    req = Requests(user=users, time=times, cand_ptr=ptr, cand_item=cand_item[valid],
                   session=imp.session_id[ck_rows])
    return RankTask(req=req, labels=labels[valid], group_user=users, imp_index=np.asarray(idx)[ck_rows],
                    extra={"age": imp.age[ck_rows], "gender": imp.gender[ck_rows],
                           "unfilled_negatives": int((~valid).sum())})


def _seen_keys_before(bench: Bench, split: str, users: np.ndarray, cutoff: np.ndarray) -> np.ndarray:
    """요청 행 r마다 cutoff 이전 유저 로그 아이템을 (r * n_items + item) 정렬 키로."""
    from recsys_core import expand_ranges
    ctx = bench.ctx[split]
    lo, hi = ctx.user_log.bounds(users, 0, cutoff)
    rows, pos = expand_ranges(lo, hi)
    return np.unique(rows * np.int64(len(bench.catalog)) + ctx.user_log.item[pos])


def pool_negative_task(bench: Bench, split: str, idx: np.ndarray, rng: np.random.Generator,
                       n_neg: int = 20, window_h: float = 48, include_inview: bool = False) -> RankTask:
    """서빙 분포(P2: 최근 window_h 발행 전체 풀)에 맞춘 학습 데이터.

    노출마다 클릭 아이템 + [t-window_h, t] 풀에서 무작위 n_neg개(요청 시점까지 읽은 것과 이번
    클릭은 제외). include_inview면 노출 목록의 비클릭 아이템도 네거티브로 함께 넣는다. 팀
    방식과 달리 제외 집합은 t 이전 로그만 쓴다(평가 시 seen 필터와 같은 정의).
    """
    imp = bench.imps[split].subset(idx)
    cat = bench.catalog
    n_items = np.int64(len(cat))
    n = len(imp)
    seen = _seen_keys_before(bench, split, imp.user_id, imp.time)
    ck_rows = np.repeat(np.arange(n), np.diff(imp.clicked_ptr))
    ck_items = cat.index_of(imp.clicked_article)
    ok = ck_items >= 0
    pos_keys = np.unique(ck_rows[ok] * n_items + ck_items[ok])

    order = np.argsort(cat.pub_time, kind="stable")
    pub_sorted = cat.pub_time[order]
    lo = np.searchsorted(pub_sorted, imp.time - int(window_h * HOUR), side="left")
    hi = np.searchsorted(pub_sorted, imp.time, side="right")
    draws = n_neg * 3
    r = np.repeat(np.arange(n, dtype=np.int64), draws)
    span = np.repeat(hi - lo, draws)
    has = span > 0
    cand = np.full(len(r), -1, dtype=np.int64)
    cand[has] = order[np.repeat(lo, draws)[has] + rng.integers(0, 1 << 30, size=int(has.sum())) % span[has]]
    keys = r * n_items + cand
    good = has & ~np.isin(keys, seen) & ~np.isin(keys, pos_keys)
    iv_keys = None
    if include_inview:
        iv_rows = np.repeat(np.arange(n, dtype=np.int64), np.diff(imp.inview_ptr))
        iv_keys = iv_rows * n_items + cat.index_of(imp.inview_article)
        good &= ~np.isin(keys, iv_keys)
    keys = keys[good]
    _, first = np.unique(keys, return_index=True)
    keys = keys[np.sort(first)]
    rows = keys // n_items
    rank = np.arange(len(rows)) - np.searchsorted(rows, rows, side="left")
    neg_keys = keys[rank < n_neg]

    parts = [pos_keys, neg_keys]
    label_parts = [np.ones(len(pos_keys), bool), np.zeros(len(neg_keys), bool)]
    if include_inview:
        iv_neg = np.unique(iv_keys[~imp.inview_clicked])
        iv_neg = iv_neg[~np.isin(iv_neg, pos_keys)]
        parts.append(iv_neg)
        label_parts.append(np.zeros(len(iv_neg), bool))
    all_keys = np.concatenate(parts)
    all_labels = np.concatenate(label_parts)
    o = np.argsort(all_keys // n_items, kind="stable")
    all_keys, all_labels = all_keys[o], all_labels[o]
    counts = np.bincount(all_keys // n_items, minlength=n)
    req = Requests(user=imp.user_id, time=imp.time, cand_ptr=np.concatenate([[0], np.cumsum(counts)]),
                   cand_item=all_keys % n_items, session=imp.session_id)
    return RankTask(req=req, labels=all_labels, group_user=imp.user_id, imp_index=np.asarray(idx),
                    extra={"age": imp.age, "gender": imp.gender})
