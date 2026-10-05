"""콜드 regime 조건 변환의 불변식 (ADR 0013 A2 사전 등록 A2.3). 합성 데이터로 돌고 EB-NeRD가 필요 없다."""
import numpy as np
import pandas as pd
import pytest

from evaluation.recsys.ebnerd.cold_transforms import (
    SHRUNK_COLUMN,
    add_shrunk_ctr,
    average_rank01,
    behaviour_users,
    global_ctr,
    latest_release,
    mask_training_popularity,
    p3_context,
    p3_task,
    pool_shrink_keep,
    rank_normalize,
    release_age_bucket,
    release_times,
    subsample_item_logs,
    subsample_request_indices,
    subsample_users,
)
from evaluation.recsys.ebnerd.neural.cold import POP_RAW_COLUMNS
from evaluation.recsys.ebnerd.prepare import impressions_in, p2_task, protocol_windows
from recsys_core import DAY, HOUR, EventIndex, Requests, compute_features, expand_ranges

GROUPS = ("recency", "history", "team_category", "category", "popularity", "short_term")
POP = list(POP_RAW_COLUMNS)


# --- 합성 데이터셋이 실제 로더·프로토콜 창을 통과하는지 -----------------------------------------

def test_synthetic_dataset_goes_through_the_real_loaders_and_protocol_windows(synth_bench):
    W = protocol_windows(synth_bench)
    assert W["fit"][0] < W["fit"][1] == W["es"][0] < W["es"][1] == W["test"][0] < W["test"][1]
    n = {k: len(impressions_in(synth_bench.imps["train" if k != "test" else "validation"], W[k])) for k in W}
    assert min(n.values()) > 100
    task = p2_task(synth_bench, "validation", impressions_in(synth_bench.imps["validation"], W["test"])[:200])
    assert task.req.n_candidates.mean() > 30 and task.labels.any()
    assert synth_bench.catalog_info["missing_embeddings"] == 0


# --- 학습 시 인기도 마스킹 ---------------------------------------------------------------------

def _pop_frame(n_req=200, per=5, seed=0):
    rng = np.random.default_rng(seed)
    n = n_req * per
    f = pd.DataFrame({c: rng.random(n).astype(np.float32) + 0.5 for c in POP})
    f["hist_cos"] = rng.random(n).astype(np.float32)
    req = Requests(user=np.arange(n_req), time=np.zeros(n_req, np.int64), cand_ptr=np.arange(0, n + 1, per),
                   cand_item=np.zeros(n, np.int64))
    return f, req


@pytest.mark.parametrize("value", [0.0, np.nan])
def test_training_mask_hides_popularity_for_whole_requests_with_the_exact_value(value):
    f, req = _pop_frame()
    masked, req_mask = mask_training_popularity(f, req, p=0.5, rng=np.random.default_rng(3000), value=value)
    rows = req_mask[req.pair_req]
    got = masked.loc[rows, POP].to_numpy()
    assert np.isnan(got).all() if np.isnan(value) else np.all(got == 0.0)
    pd.testing.assert_frame_equal(masked.loc[~rows], f.loc[~rows])        # 안 가린 요청은 그대로
    assert masked["hist_cos"].equals(f["hist_cos"])                       # 인기도 밖의 열은 그대로
    assert 0.35 < req_mask.mean() < 0.65
    # 요청 단위: 한 요청 안에서 일부 후보만 가려지는 일이 없다
    per_req = rows.reshape(req.n, -1)
    assert np.all(per_req.all(axis=1) | ~per_req.any(axis=1))


def test_zero_and_nan_variants_mask_the_same_requests_for_the_same_seed():
    f, req = _pop_frame()
    _, m0 = mask_training_popularity(f, req, 0.5, np.random.default_rng(3001), 0.0)
    _, mn = mask_training_popularity(f, req, 0.5, np.random.default_rng(3001), np.nan)
    _, other = mask_training_popularity(f, req, 0.5, np.random.default_rng(3002), 0.0)
    assert np.array_equal(m0, mn) and not np.array_equal(m0, other)


def test_fit_then_es_draws_come_from_one_stream():
    """fit → es 순서로 같은 rng에서 뽑으므로, es 마스크는 fit 요청 수에 따라 달라지는 이어진 난수다."""
    f, req = _pop_frame(n_req=50)
    rng = np.random.default_rng(3000)
    _, fit_mask = mask_training_popularity(f, req, 0.5, rng, 0.0)
    _, es_mask = mask_training_popularity(f, req, 0.5, rng, 0.0)
    stream = np.random.default_rng(3000).random(100) < 0.5
    assert np.array_equal(fit_mask, stream[:50]) and np.array_equal(es_mask, stream[50:])


# --- 유저 서브샘플 -----------------------------------------------------------------------------

def test_subsample_users_is_nested_and_sized_by_ceiling(synth_bench):
    universe = behaviour_users(synth_bench)
    u1, u5, u20 = (subsample_users(universe, f, seed=11) for f in (0.01, 0.05, 0.20))
    assert len(u1) == int(np.ceil(0.01 * len(universe))) and len(u20) == int(np.ceil(0.20 * len(universe)))
    assert set(u1) <= set(u5) <= set(u20) <= set(universe.tolist())
    assert np.array_equal(u5, subsample_users(universe, 0.05, seed=11))
    assert not np.array_equal(u20, subsample_users(universe, 0.20, seed=12))


def test_subsample_popularity_counts_only_subsample_users_events(synth_bench):
    universe = behaviour_users(synth_bench)
    users = subsample_users(universe, 0.2, seed=11)
    clicks, views = subsample_item_logs(synth_bench.imps, synth_bench.catalog, users)
    want_clicks = want_views = 0
    per_item = np.zeros(len(synth_bench.catalog), dtype=np.int64)
    for imp in synth_bench.imps.values():
        own = np.isin(imp.user_id, users)
        ck_rows = np.repeat(np.arange(len(imp)), np.diff(imp.clicked_ptr))
        iv_rows = np.repeat(np.arange(len(imp)), np.diff(imp.inview_ptr))
        want_clicks += int(own[ck_rows].sum())
        want_views += int(own[iv_rows].sum())
        np.add.at(per_item, synth_bench.catalog.index_of(imp.clicked_article[own[ck_rows]]), 1)
    assert len(clicks) == want_clicks and len(views) == want_views
    assert 0 < want_clicks < len(synth_bench.ctx["train"].item_clicks)  # 전체 로그보다 적다
    far = np.iinfo(np.int64).max // 4
    items = np.arange(len(synth_bench.catalog))
    assert np.array_equal(clicks.count(items, 0, far), per_item)


def test_subsample_feature_matrix_only_changes_popularity_columns(synth_bench):
    import dataclasses
    W = protocol_windows(synth_bench)
    users = subsample_users(behaviour_users(synth_bench), 0.2, seed=11)
    idx = subsample_request_indices(synth_bench.imps["validation"], W["test"], users, cap=None, seed=12)
    task = p2_task(synth_bench, "validation", idx)
    ctx = synth_bench.ctx["validation"]
    clicks, views = subsample_item_logs(synth_bench.imps, synth_bench.catalog, users)
    full = compute_features(ctx, task.req, groups=GROUPS)
    sub = compute_features(dataclasses.replace(ctx, item_clicks=clicks, item_inviews=views), task.req, groups=GROUPS)
    others = [c for c in full.columns if c not in POP]
    pd.testing.assert_frame_equal(sub[others], full[others])
    assert np.all(sub["pop_clicks_48h"].to_numpy() <= full["pop_clicks_48h"].to_numpy())
    assert sub["pop_clicks_48h"].sum() < full["pop_clicks_48h"].sum()


def test_subsample_requests_belong_to_subsample_users_and_respect_window_and_cap(synth_bench):
    W = protocol_windows(synth_bench)
    imp = synth_bench.imps["validation"]
    users = subsample_users(behaviour_users(synth_bench), 0.2, seed=11)
    idx = subsample_request_indices(imp, W["test"], users, cap=None, seed=12)
    assert len(idx) > 0 and np.isin(imp.user_id[idx], users).all()
    assert np.all((imp.time[idx] >= W["test"][0]) & (imp.time[idx] < W["test"][1]))
    capped = subsample_request_indices(imp, W["test"], users, cap=10, seed=12)
    assert len(capped) == 10 and set(capped) <= set(idx) and np.all(np.diff(capped) > 0)
    assert np.array_equal(capped, subsample_request_indices(imp, W["test"], users, cap=10, seed=12))


# --- 풀 축소 -----------------------------------------------------------------------------------

def _pool(sizes=(100, 55, 30, 70), n_pos=(1, 2, 1, 0), seed=0):
    rng = np.random.default_rng(seed)
    ptr = np.concatenate([[0], np.cumsum(sizes)])
    labels = np.zeros(ptr[-1], dtype=bool)
    for g, k in enumerate(n_pos):
        labels[rng.choice(np.arange(ptr[g], ptr[g + 1]), size=k, replace=False)] = True
    return labels, ptr


def test_pool_shrink_keeps_every_positive_and_caps_the_pool():
    labels, ptr = _pool()
    for n in (40, 60, 90):
        keep = pool_shrink_keep(labels, ptr, n, seed=13)
        assert keep[labels].all()
        sizes = np.add.reduceat(keep.astype(int), ptr[:-1])
        assert sizes.tolist() == [min(s, n) for s in np.diff(ptr)]


def test_pool_shrink_is_nested_and_deterministic():
    labels, ptr = _pool()
    k40, k60, k90 = (pool_shrink_keep(labels, ptr, n, seed=13) for n in (40, 60, 90))
    assert np.all(k40 <= k60) and np.all(k60 <= k90)
    assert np.array_equal(k60, pool_shrink_keep(labels, ptr, 60, seed=13))
    assert not np.array_equal(k60, pool_shrink_keep(labels, ptr, 60, seed=14))


def test_pool_shrink_keeps_all_positives_even_when_they_exceed_n():
    labels = np.array([True, True, True, False, False])
    keep = pool_shrink_keep(labels, np.array([0, 5]), 2, seed=0)
    assert keep.tolist() == [True, True, True, False, False]


# --- 요청 내 랭크 정규화 -----------------------------------------------------------------------

def test_average_rank01_handles_ties_nan_and_degenerate_groups():
    values = np.array([3.0, 1.0, 2.0, 1.0,      # 동점 한 쌍: 1,1 -> 평균 순위 0.5 -> 0.5/3
                       5.0, 5.0, 5.0,           # 전부 동점 -> 0.5
                       7.0,                     # 후보 하나 -> 0.5
                       np.nan, 2.0, 4.0])       # NaN은 순위에서 빼고 NaN으로 남긴다
    ptr = np.array([0, 4, 7, 8, 11])
    got = average_rank01(values, ptr)
    want = [1.0, 0.5 / 3, 2 / 3, 0.5 / 3, 0.5, 0.5, 0.5, 0.5, np.nan, 0.0, 1.0]
    np.testing.assert_allclose(got, want, equal_nan=True)


def test_rank_normalize_is_within_request_and_touches_only_listed_columns():
    ptr = np.array([0, 3, 6])
    f = pd.DataFrame({"a": [1.0, 5.0, 3.0, 10.0, 30.0, 20.0], "b": [9.0, 8.0, 7.0, 1.0, 2.0, 3.0],
                      "keep": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]}, dtype=np.float32)
    out = rank_normalize(f, ptr, ["a", "b", "not_there"])
    assert out["a"].tolist() == [0.0, 1.0, 0.5, 0.0, 1.0, 0.5]
    assert out["b"].tolist() == [1.0, 0.5, 0.0, 0.0, 0.5, 1.0]
    assert out["keep"].equals(f["keep"]) and f["a"].tolist()[0] == 1.0
    # 다른 요청의 값을 바꿔도 이 요청의 순위는 그대로다(요청 안에서만 계산)
    g = f.copy()
    g.loc[3:, "a"] = [1e9, -1e9, 0.0]
    assert rank_normalize(g, ptr, ["a"])["a"].tolist()[:3] == [0.0, 1.0, 0.5]
    # 단조 변환에 불변: 척도가 달라도 같은 표현
    h = f.copy()
    h["a"] = np.log1p(h["a"]) * 1000
    assert rank_normalize(h, ptr, ["a"])["a"].tolist() == out["a"].tolist()


# --- 축소 CTR ----------------------------------------------------------------------------------

T = 1_700_000_000


def _ev(pairs):
    k, t = zip(*pairs) if pairs else ((), ())
    return EventIndex(k, t, k)


def test_global_ctr_uses_only_the_half_open_trailing_window():
    clicks = _ev([(0, T - 25 * HOUR), (0, T - 23 * HOUR), (1, T - 1), (1, T)])
    views = _ev([(0, T - 25 * HOUR)] + [(0, T - 23 * HOUR)] * 4 + [(1, T - 1)] * 4 + [(1, T)] * 10)
    # [T-24h, T): 클릭 2건(T-23h, T-1), 노출 8건. T 시각의 이벤트와 24h보다 오래된 이벤트는 빠진다.
    assert global_ctr(clicks, views, np.array([T]))[0] == pytest.approx(2 / 8)
    assert global_ctr(clicks, views, np.array([T - 30 * HOUR]))[0] == 0.0  # 노출 0 -> 0


def test_shrunk_ctr_matches_the_registered_formula(synth_bench):
    W = protocol_windows(synth_bench)
    idx = impressions_in(synth_bench.imps["validation"], W["test"])[:50]
    task = p2_task(synth_bench, "validation", idx)
    ctx = synth_bench.ctx["validation"]
    f = compute_features(ctx, task.req, groups=GROUPS)
    out = add_shrunk_ctr(f, task.req, ctx, alpha=20)
    p0 = global_ctr(ctx.item_clicks, ctx.item_inviews, task.req.time)[task.req.pair_req]
    want = (f["pop_clicks_24h"] + 20 * p0) / (f["pop_inviews_24h"] + 20)
    np.testing.assert_allclose(out[SHRUNK_COLUMN], want, rtol=1e-6)
    assert SHRUNK_COLUMN not in f.columns
    # 노출이 0인 후보는 사전값 p0 그대로, 노출이 많을수록 원 CTR에 가까워진다
    zero = f["pop_inviews_24h"].to_numpy() == 0
    assert zero.any() and np.allclose(out[SHRUNK_COLUMN].to_numpy()[zero], p0[zero], rtol=1e-6)
    big = f["pop_inviews_24h"].to_numpy() > 200
    if big.any():
        raw = f["pop_ctr_24h"].to_numpy()[big]
        assert np.all(np.abs(out[SHRUNK_COLUMN].to_numpy()[big] - raw) < np.abs(p0[big] - raw) + 1e-9)


# --- 일일 배치 릴리스 양자화 -------------------------------------------------------------------

def test_release_is_the_first_daily_boundary_at_or_after_publication():
    seven = 7 * HOUR
    pub = np.array([seven, seven + 1, seven + DAY - 1, seven + DAY, 0, 5 * DAY + 3 * HOUR])
    rel = release_times(pub, release_hour=7)
    assert rel.tolist() == [seven, seven + DAY, seven + DAY, seven + DAY, seven, 5 * DAY + seven]
    assert np.all(rel >= pub) and np.all(rel - pub < DAY) and np.all((rel - seven) % DAY == 0)
    t = np.array([seven, seven + 1, seven + DAY - 1, seven + DAY])
    assert latest_release(t, 7).tolist() == [seven, seven, seven, seven + DAY]


def test_p3_pool_contains_only_released_articles_of_the_last_two_batches(synth_bench):
    W = protocol_windows(synth_bench)
    idx = impressions_in(synth_bench.imps["validation"], W["test"])[::7]
    task = p3_task(synth_bench, "validation", idx, release_hour=7, batches=2)
    req = task.req
    cat = synth_bench.catalog
    t = req.time[req.pair_req]
    rel = release_times(cat.pub_time, 7)[req.cand_item]
    current = latest_release(t, 7)
    assert np.all(cat.pub_time[req.cand_item] <= rel) and np.all(rel <= t)  # 릴리스 전(미발행 포함) 기사는 후보가 아니다
    assert np.all((rel == current) | (rel == current - DAY))
    age = task.extra["release_age_h"]
    assert np.all(age >= 0) and np.all(age < 48)
    # 이미 읽은 기사는 빠진다
    log = synth_bench.ctx["validation"].user_log
    lo, hi = log.bounds(req.user, 0, req.time)
    rows, pos = expand_ranges(lo, hi)
    seen = set(zip(rows.tolist(), log.item[pos].tolist()))
    assert not any((r, i) in seen for r, i in zip(req.pair_req.tolist(), req.cand_item.tolist()))
    assert np.all(task.n_pos_total >= np.bincount(req.pair_req[task.labels], minlength=req.n))


def test_p3_context_counts_popularity_only_after_release_and_ages_from_release(synth_bench):
    ctx = synth_bench.ctx["validation"]
    q = p3_context(ctx, release_hour=7)
    rel = release_times(ctx.catalog.pub_time, 7)
    assert np.array_equal(q.catalog.pub_time, rel)
    assert np.all(q.item_clicks.time >= rel[q.item_clicks.key]) and np.all(q.item_inviews.time >= rel[q.item_inviews.key])
    assert 0 < len(q.item_clicks) < len(ctx.item_clicks)   # 릴리스 전 클릭이 실제로 빠졌다
    assert q.user_log is ctx.user_log                       # 유저 히스토리는 그대로
    W = protocol_windows(synth_bench)
    task = p3_task(synth_bench, "validation", impressions_in(synth_bench.imps["validation"], W["test"])[:40])
    f = compute_features(q, task.req, groups=GROUPS)
    np.testing.assert_allclose(f["hours_since_pub"], task.extra["release_age_h"], rtol=1e-5, atol=1e-3)


def test_release_age_bucket_uses_the_youngest_positive_in_the_pool():
    from evaluation.recsys.ebnerd.prepare import RankTask
    req = Requests(user=[1, 2, 3], time=[0, 0, 0], cand_ptr=[0, 3, 6, 8], cand_item=np.zeros(8, np.int64))
    labels = np.array([0, 1, 0, 1, 0, 1, 0, 0], bool)
    age = np.array([1.0, 5.0, 30.0, 30.0, 0.5, 1.9, 3.0, 40.0])
    task = RankTask(req=req, labels=labels, group_user=np.array([1, 2, 3]), imp_index=np.arange(3),
                    extra={"release_age_h": age})
    # 요청 0: 정답 5h -> [2,6) = 1 / 요청 1: 정답 30h, 1.9h 중 젊은 쪽 -> [0,2) = 0 / 요청 2: 풀 안 정답 없음 -> -1
    assert release_age_bucket(task, [0, 2, 6, 12, 24]).tolist() == [1, 0, -1]
    labels2 = np.array([0, 0, 1, 0, 0, 0, 0, 1], bool)
    task2 = RankTask(req=req, labels=labels2, group_user=np.array([1, 2, 3]), imp_index=np.arange(3),
                     extra={"release_age_h": age})
    assert release_age_bucket(task2, [0, 2, 6, 12, 24]).tolist() == [4, -1, 4]
