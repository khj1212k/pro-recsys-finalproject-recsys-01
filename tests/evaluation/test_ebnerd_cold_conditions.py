"""콜드 조건 정의(neural/cold.py)의 불변식: 절단은 미래를 보지 않고 최근 k건만 남기며, 인기도 마스크는 raw 0이다.

EB-NeRD 없이 도는 합성 로그 테스트다(ADR 0013 A2 사전 등록 A2.3).
"""
import numpy as np
import pandas as pd
import pytest

from evaluation.recsys.ebnerd.neural.cold import (
    COLD_TRUNCATE_KS,
    POP_RAW_COLUMNS,
    cold_conditions,
    pop_mask_raw,
    truncate_logs,
)
from recsys_core import DAY, HOUR, EventIndex, FeatureContext, ItemCatalog, Requests, compute_features

T = 1_700_000_000
GROUPS = ("recency", "history", "team_category", "category", "popularity", "short_term")
USER_COLUMNS = ["hist_cos", "hist_len", "cat_share", "short_cos", "short_len", "sess_cos", "sess_len",
                "hours_since_last_event"]


def _catalog(n=8, dim=6, seed=0):
    rng = np.random.default_rng(seed)
    emb = rng.standard_normal((n, dim)).astype(np.float32)
    emb /= np.linalg.norm(emb, axis=1, keepdims=True)
    return ItemCatalog(ids=np.arange(100, 100 + n), emb=emb, pub_time=np.full(n, T - 5 * HOUR),
                       category=np.arange(n) % 3, n_categories=3)


def _index(events):
    events = list(events)
    if not events:
        return EventIndex([], [], [])
    k, t, i = zip(*events)
    return EventIndex(k, t, i)


# 유저 7의 이벤트(시각, 아이템): 과거 5건 + 요청 시각 T 이후 1건(미래)
USER_EVENTS = [(7, T - 5 * DAY, 0), (7, T - 3 * DAY, 1), (7, T - 30 * HOUR, 2), (7, T - 2 * HOUR, 3),
               (7, T - 1 * HOUR, 4), (7, T + 1 * HOUR, 5)]
# 세션 55(유저 7의 현재 세션): 최근 3건 + 미래 1건
SESSION_EVENTS = [(55, T - 30 * HOUR, 2), (55, T - 2 * HOUR, 3), (55, T - 1 * HOUR, 4), (55, T + 1 * HOUR, 5)]


def _ctx(user_events=USER_EVENTS, session_events=SESSION_EVENTS):
    clicks = [(i, T - h * HOUR, i) for i in range(8) for h in (1, 3, 30)]
    static = (np.array([7, 9]), np.array([[True, False, True], [False, True, False]]))
    return FeatureContext(catalog=_catalog(), user_log=_index(user_events), session_log=_index(session_events),
                          item_clicks=_index(clicks), item_inviews=_index(clicks + clicks), static_categories=static)


def _req(users=(7,), times=(T,), sessions=(55,), n_cand=4, cutoffs=None):
    n = len(users)
    return Requests(user=list(users), time=list(times), cand_ptr=np.arange(0, n * n_cand + 1, n_cand),
                    cand_item=np.tile(np.arange(n_cand) + 4, n), session=list(sessions),
                    profile_cutoff=None if cutoffs is None else list(cutoffs))


def _events_of(index, key):
    lo, hi = index.bounds(np.array([key]), 0, np.array([np.iinfo(np.int64).max // 4]))
    return list(zip(index.time[lo[0]:hi[0]].tolist(), index.item[lo[0]:hi[0]].tolist()))


def test_truncation_keeps_the_most_recent_k_events_strictly_before_the_cutoff():
    ctx, req = _ctx(), _req()
    tctx, treq = truncate_logs(ctx, req, 2)
    kept = _events_of(tctx.user_log, int(treq.user[0]))
    assert kept == [(T - 2 * HOUR, 3), (T - 1 * HOUR, 4)]
    assert all(t < T for t, _ in kept)  # 미래 이벤트(T+1h)는 어떤 k에서도 들어오지 않는다
    for k in (0, 1, 3, 5, 10):
        tctx, treq = truncate_logs(ctx, req, k)
        ev = _events_of(tctx.user_log, int(treq.user[0]))
        assert len(ev) == min(k, 5) and all(t < T for t, _ in ev)


def test_session_log_is_truncated_with_the_same_k():
    tctx, treq = truncate_logs(_ctx(), _req(), 1)
    assert _events_of(tctx.session_log, int(treq.session[0])) == [(T - 1 * HOUR, 4)]
    f = compute_features(tctx, treq, groups=GROUPS)
    assert f["sess_len"].tolist() == [1.0] * 4 and f["hist_len"].tolist() == [1.0] * 4


@pytest.mark.parametrize("k", [1, 2, 3])
def test_truncated_features_equal_features_of_a_user_who_only_has_those_events(k):
    """절단 로그로 계산한 유저 피처 전부가, 처음부터 그 k건만 가진 유저의 피처와 같아야 한다."""
    ctx, req = _ctx(), _req()
    tctx, treq = truncate_logs(ctx, req, k)
    got = compute_features(tctx, treq, groups=GROUPS)
    past_user = [e for e in USER_EVENTS if e[1] < T][-k:]
    past_sess = [e for e in SESSION_EVENTS if e[1] < T][-k:]
    want = compute_features(_ctx(past_user, past_sess), req, groups=GROUPS)
    pd.testing.assert_frame_equal(got, want, check_exact=False, rtol=1e-6, atol=1e-7)


def test_k_none_and_k_beyond_history_reproduce_the_untruncated_matrix():
    ctx, req = _ctx(), _req()
    full = compute_features(ctx, req, groups=GROUPS)
    same_ctx, same_req = truncate_logs(ctx, req, None)
    assert same_ctx is ctx and same_req is req
    tctx, treq = truncate_logs(ctx, req, 50)
    pd.testing.assert_frame_equal(compute_features(tctx, treq, groups=GROUPS), full, check_exact=False,
                                  rtol=1e-6, atol=1e-7)


def test_k0_empties_user_state_but_keeps_item_and_static_features():
    ctx, req = _ctx(), _req()
    full = compute_features(ctx, req, groups=GROUPS)
    tctx, treq = truncate_logs(ctx, req, 0)
    f = compute_features(tctx, treq, groups=GROUPS)
    for c in ("hist_cos", "hist_len", "cat_share", "short_cos", "short_len", "sess_cos", "sess_len"):
        assert np.all(f[c].to_numpy() == 0.0), c
    assert f["hours_since_last_event"].isna().all()
    untouched = [c for c in full.columns if c not in USER_COLUMNS]
    # 온보딩 대체물(static_categories)·신선도·인기도는 이벤트로 세지 않으므로 그대로다.
    pd.testing.assert_frame_equal(f[untouched], full[untouched])
    assert full["user_ncat"].iloc[0] == 2.0


def test_each_request_is_truncated_at_its_own_cutoff_even_for_the_same_user():
    """같은 유저의 이른 요청이 늦은 요청 시점의 이벤트를 보면 안 된다(요청별 절단)."""
    ctx = _ctx()
    req = _req(users=(7, 7, 9), times=(T - 10 * HOUR, T, T), sessions=(55, 55, 77))
    tctx, treq = truncate_logs(ctx, req, 1)
    assert _events_of(tctx.user_log, 0) == [(T - 30 * HOUR, 2)]
    assert _events_of(tctx.user_log, 1) == [(T - 1 * HOUR, 4)]
    assert _events_of(tctx.user_log, 2) == []           # 유저 9는 이벤트가 없다
    assert _events_of(tctx.session_log, 0) == [(T - 30 * HOUR, 2)]
    f = compute_features(tctx, treq, groups=GROUPS)
    assert f["hist_len"].tolist() == [1.0] * 8 + [0.0] * 4
    # 유저 9의 온보딩 대체 카테고리는 요청 키가 바뀌어도 따라간다.
    assert f["user_ncat"].tolist() == [2.0] * 8 + [1.0] * 4


def test_profile_cutoff_not_request_time_defines_before():
    ctx = _ctx()
    req = _req(cutoffs=(T - 90 * 60,))  # T-1.5h: T-1h 이벤트는 cutoff 이후
    tctx, treq = truncate_logs(ctx, req, 1)
    assert _events_of(tctx.user_log, 0) == [(T - 2 * HOUR, 3)]


def test_truncation_does_not_touch_candidates_or_the_original_context():
    ctx, req = _ctx(), _req()
    n_before = len(ctx.user_log)
    tctx, treq = truncate_logs(ctx, req, 1)
    assert np.array_equal(treq.cand_ptr, req.cand_ptr) and np.array_equal(treq.cand_item, req.cand_item)
    assert np.array_equal(treq.time, req.time) and np.array_equal(treq.profile_cutoff, req.profile_cutoff)
    assert len(ctx.user_log) == n_before and tctx.item_clicks is ctx.item_clicks


def test_pop_mask_raw_zeroes_exactly_the_popularity_columns():
    ctx, req = _ctx(), _req()
    f = compute_features(ctx, req, groups=GROUPS)
    assert (f[list(POP_RAW_COLUMNS)].to_numpy() > 0).any()
    before = f.copy()
    m = pop_mask_raw(f)
    assert np.all(m[list(POP_RAW_COLUMNS)].to_numpy() == 0.0)
    others = [c for c in f.columns if c not in POP_RAW_COLUMNS]
    pd.testing.assert_frame_equal(m[others], f[others])
    pd.testing.assert_frame_equal(f, before)  # 입력은 바꾸지 않는다


def test_pop_mask_raw_can_mask_selected_rows_with_nan_and_extra_columns():
    f = pd.DataFrame({c: np.arange(1.0, 5.0, dtype=np.float32) for c in POP_RAW_COLUMNS})
    f["pop_ctr_shrunk_24h"] = np.float32(0.3)
    f["hist_cos"] = np.float32(0.5)
    rows = np.array([True, False, True, False])
    m = pop_mask_raw(f, value=np.nan, rows=rows, extra_columns=("pop_ctr_shrunk_24h",))
    assert m.loc[rows, list(POP_RAW_COLUMNS) + ["pop_ctr_shrunk_24h"]].isna().all().all()
    assert not m.loc[~rows].isna().any().any()
    assert m["hist_cos"].tolist() == [0.5] * 4


def test_cold_conditions_are_pop0_and_the_four_truncation_levels():
    conds = cold_conditions()
    assert [c.name for c in conds] == ["pop0", "k0", "k1", "k3", "k5"]
    assert COLD_TRUNCATE_KS == (0, 1, 3, 5)
    assert conds[0].pop_zero and conds[0].truncate_k is None
    assert [c.truncate_k for c in conds[1:]] == [0, 1, 3, 5] and not any(c.pop_zero for c in conds[1:])
